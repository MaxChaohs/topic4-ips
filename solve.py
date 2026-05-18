#!/usr/bin/env python3
"""
solve.py - post-contest position solver

Usage:
    python3 solve.py --data-dir data/ --config config.yaml

Workflow:
    1. Enumerate all P{n}_R{r}_*.csv in data/ (14 total)
    2. Solve position for each (point, round)
    3. Ensemble two rounds
    4. Output final 7 coordinates

Method: Weighted Least Squares with 3D bounds
    minimize sum_i  w_i * (||p - p_i|| - d_i)^2
    subject to: x in [0, 14.5], y in [0, 8.5], z in [0, 3]

    where d_i is derived from path loss: d = 10^((A - RSSI) / (10n))
"""
import argparse
import csv
import glob
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml
from scipy.optimize import minimize


# --- Data loading ---

def load_csv(path: str, target_bssids: dict) -> dict:
    """Load single CSV, return {ap_key: {band: [rssi, ...]}}"""
    out = defaultdict(lambda: defaultdict(list))
    with open(path) as f:
        for row in csv.DictReader(f):
            bssid = row["bssid"].lower()
            if bssid not in target_bssids:
                continue
            ap_key = target_bssids[bssid]
            try:
                freq = int(row["frequency"])
                rssi = int(row["rssi"])
                band = "2_4" if freq < 3000 else "5"
                out[ap_key][band].append(rssi)
            except (ValueError, KeyError):
                continue
    return out


def parse_filename(name: str) -> tuple:
    """P1_R1_20251109_123456.csv -> ('P1', 1)"""
    m = re.match(r"(P\d+)_R(\d+)_", name)
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


# --- RSSI -> distance ---

def rssi_to_distance(rssi: float, A: float, n: float) -> float:
    """d = 10^((A - RSSI) / (10n))"""
    return float(10 ** ((A - rssi) / (10 * n)))


def aggregate_rssi(samples: list, method: str = "median",
                   trim_percent: float = 20) -> float:
    """Aggregate multiple RSSI samples into one representative value"""
    if not samples:
        return None
    arr = np.array(samples, dtype=float)
    if method == "median":
        return float(np.median(arr))
    elif method == "mean":
        return float(np.mean(arr))
    elif method == "trimmed_mean":
        lo = np.percentile(arr, trim_percent)
        hi = np.percentile(arr, 100 - trim_percent)
        trimmed = arr[(arr >= lo) & (arr <= hi)]
        return float(np.mean(trimmed)) if len(trimmed) else float(np.median(arr))
    else:
        return float(np.median(arr))


# --- Solver ---

def solve_position(measurements: list, bounds: tuple,
                   initial: np.ndarray = None) -> tuple:
    """
    Weighted Least Squares for 3D position.

    measurements: [(ap_pos: np.array(3,), distance: float, weight: float), ...]
    bounds: ((x_min, x_max), (y_min, y_max), (z_min, z_max))
    initial: starting guess (None -> weighted centroid)

    Returns: (position: np.array(3,), residual: float, success: bool)
    """
    if len(measurements) < 3:
        center = np.array([
            (bounds[0][0] + bounds[0][1]) / 2,
            (bounds[1][0] + bounds[1][1]) / 2,
            (bounds[2][0] + bounds[2][1]) / 2,
        ])
        return center, float("inf"), False

    ap_positions = np.array([m[0] for m in measurements])
    distances = np.array([m[1] for m in measurements])
    weights = np.array([m[2] for m in measurements])

    if initial is None:
        initial = np.average(ap_positions, axis=0, weights=weights)

    def cost(p):
        diffs = np.linalg.norm(ap_positions - p, axis=1) - distances
        return float(np.sum(weights * diffs ** 2))

    # Multi-start to avoid local minima
    best_result = None
    starts = [
        initial,
        np.array([(bounds[0][0] + bounds[0][1]) / 2,
                  (bounds[1][0] + bounds[1][1]) / 2,
                  1.0]),
    ]
    for ap_pos in ap_positions:
        s = ap_pos.copy()
        s[0] = np.clip(s[0], bounds[0][0], bounds[0][1])
        s[1] = np.clip(s[1], bounds[1][0], bounds[1][1])
        s[2] = np.clip(s[2], bounds[2][0], bounds[2][1])
        starts.append(s)

    for s in starts:
        try:
            res = minimize(
                cost, s,
                method="L-BFGS-B",
                bounds=bounds,
                options={"maxiter": 200, "ftol": 1e-9},
            )
            if best_result is None or res.fun < best_result.fun:
                best_result = res
        except Exception:
            continue

    if best_result is None:
        return initial, float("inf"), False

    return best_result.x, float(best_result.fun), best_result.success


# --- Main flow ---

def solve_one_round(csv_path: str, cfg: dict,
                    target_bssids: dict,
                    ap_positions: dict) -> dict:
    """Solve a single CSV (one round for one point)"""
    raw = load_csv(csv_path, target_bssids)
    per_ap = cfg["path_loss"]["per_ap"]
    default = cfg["path_loss"]
    min_samples = cfg["solver"]["min_samples_per_ap"]
    method = cfg["solver"]["outlier_method"]
    trim = cfg["solver"]["trim_percent"]

    measurements = []
    debug_info = []

    for ap_key, ap_pos in ap_positions.items():
        bands = raw.get(ap_key, {})
        for band, samples in bands.items():
            if len(samples) < min_samples:
                continue
            rssi_repr = aggregate_rssi(samples, method, trim)

            ap_params = per_ap.get(ap_key, {})
            A = ap_params.get(f"A_{band}")
            n = ap_params.get(f"n_{band}")
            if A is None or n is None:
                default_key = f"default_{band}ghz" if band == "5" else "default_2_4ghz"
                A = default[default_key]["A"]
                n = default[default_key]["n"]

            d = rssi_to_distance(rssi_repr, A, n)
            d = max(0.5, min(d, 30.0))  # clamp

            # Weight: stronger RSSI (closer AP) gets higher weight
            weight = max(1.0, rssi_repr + 100)  # rssi=-50 -> weight=50
            sample_factor = min(len(samples) / 30.0, 1.0)
            weight *= sample_factor

            measurements.append((ap_pos, d, weight))
            debug_info.append({
                "ap": ap_key, "band": band, "rssi": rssi_repr,
                "n_samples": len(samples), "distance_est": d, "weight": weight
            })

    if not measurements:
        return {"position": None, "residual": float("inf"),
                "success": False, "debug": [], "n_measurements": 0}

    field = cfg["field"]
    bounds = (
        (field["x_min"], field["x_max"]),
        (field["y_min"], field["y_max"]),
        (field["z_min"], field["z_max"]),
    )

    position, residual, success = solve_position(measurements, bounds)

    return {
        "position": position,
        "residual": residual,
        "success": success,
        "debug": debug_info,
        "n_measurements": len(measurements),
    }


def ensemble_two_rounds(r1: dict, r2: dict, method: str = "weighted_mean",
                        divergence_threshold: float = 3.0) -> dict:
    """Combine two rounds"""
    p1 = r1["position"]
    p2 = r2["position"]

    if p1 is None and p2 is None:
        return {"position": None, "method": "both_failed"}
    if p1 is None:
        return {"position": p2, "method": "use_r2_only"}
    if p2 is None:
        return {"position": p1, "method": "use_r1_only"}

    distance = float(np.linalg.norm(p1 - p2))

    if method == "best_residual":
        if r1["residual"] <= r2["residual"]:
            return {"position": p1, "method": "best_residual_r1",
                    "divergence": distance}
        else:
            return {"position": p2, "method": "best_residual_r2",
                    "divergence": distance}

    elif method == "weighted_mean":
        if distance > divergence_threshold:
            if r1["residual"] <= r2["residual"]:
                chosen = p1
                tag = "diverged_pick_r1"
            else:
                chosen = p2
                tag = "diverged_pick_r2"
            return {"position": chosen, "method": tag, "divergence": distance}
        else:
            w1 = 1.0 / max(r1["residual"], 0.01)
            w2 = 1.0 / max(r2["residual"], 0.01)
            avg = (w1 * p1 + w2 * p2) / (w1 + w2)
            return {"position": avg, "method": "weighted_mean",
                    "divergence": distance}

    else:
        avg = (p1 + p2) / 2
        return {"position": avg, "method": "simple_average",
                "divergence": distance}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--output", default="result.csv")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    target_bssids = {}
    ap_positions = {}
    for ap_key, info in cfg["infrastructure"].items():
        bssid = info["bssid"]
        if bssid and bssid != "XX:XX:XX:XX:XX:XX":
            target_bssids[bssid.lower()] = ap_key
            ap_positions[ap_key] = np.array(info["position"], dtype=float)

    if not target_bssids:
        print("[!] No BSSID in config.yaml")
        return

    csv_files = sorted(glob.glob(f"{args.data_dir}/P*_R*_*.csv"))
    grouped = defaultdict(dict)
    for path in csv_files:
        name = Path(path).name
        point, rnd = parse_filename(name)
        if point and rnd:
            existing = grouped[point].get(rnd)
            if existing is None or path > existing:
                grouped[point][rnd] = path

    if not grouped:
        print(f"[!] No P*_R*_*.csv found in {args.data_dir}/")
        return

    print(f"[*] Found {len(grouped)} points, files:\n")
    for point in sorted(grouped):
        for rnd in sorted(grouped[point]):
            print(f"    {point}  R{rnd}: {Path(grouped[point][rnd]).name}")

    final_results = {}
    print("\n" + "=" * 70)
    print("Solver Results")
    print("=" * 70)

    for point in sorted(grouped):
        rounds_data = {}
        for rnd in [1, 2]:
            path = grouped[point].get(rnd)
            if path is None:
                print(f"\n[!] {point} missing R{rnd}")
                rounds_data[rnd] = {"position": None, "residual": float("inf"),
                                    "success": False, "debug": [],
                                    "n_measurements": 0}
                continue
            rounds_data[rnd] = solve_one_round(path, cfg, target_bssids, ap_positions)

        merged = ensemble_two_rounds(
            rounds_data[1], rounds_data[2],
            method=cfg["solver"]["ensemble_method"],
            divergence_threshold=cfg["solver"]["divergence_threshold"],
        )

        final_results[point] = {
            "round1": rounds_data[1],
            "round2": rounds_data[2],
            "final": merged,
        }

        print(f"\n--- {point} ---")
        for rnd in [1, 2]:
            r = rounds_data[rnd]
            if r["position"] is not None:
                p = r["position"]
                print(f"  R{rnd}: ({p[0]:5.2f}, {p[1]:5.2f}, {p[2]:5.2f}) "
                      f"residual={r['residual']:.2f}  "
                      f"meas={r['n_measurements']}")
                if args.verbose:
                    for d in r["debug"]:
                        print(f"       {d['ap']:<10} band={d['band']:<3} "
                              f"rssi={d['rssi']:6.1f} "
                              f"d={d['distance_est']:5.2f}m "
                              f"n={d['n_samples']:>3} w={d['weight']:.1f}")
            else:
                print(f"  R{rnd}: <failed>")

        fp = merged["position"]
        if fp is not None:
            print(f"  => Final: ({fp[0]:5.2f}, {fp[1]:5.2f}, {fp[2]:5.2f})  "
                  f"[{merged['method']}]")

    with open(args.output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["point", "x", "y", "z", "method"])
        for point in sorted(final_results):
            r = final_results[point]["final"]
            if r["position"] is not None:
                p = r["position"]
                writer.writerow([point, f"{p[0]:.3f}", f"{p[1]:.3f}",
                                 f"{p[2]:.3f}", r["method"]])
            else:
                writer.writerow([point, "NaN", "NaN", "NaN", "failed"])

    print(f"\n[+] Final results written to {args.output}")


if __name__ == "__main__":
    main()