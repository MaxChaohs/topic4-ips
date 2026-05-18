#!/usr/bin/env python3
"""
calibrate.py - Path Loss parameter calibration

Pre-contest workflow:
    1. Find a few known-position points (e.g. 4 calibration points)
    2. Run collect_rssi.py at each (--point cal_X)
    3. Feed CSVs to this script to fit A and n
    4. Results written back to config.yaml

Model:
    RSSI = A - 10 * n * log10(d)
    Fit per band: 2.4GHz and 5GHz have different A, n

Usage:
    python3 calibrate.py --calib-file calibration.yaml
    
calibration.yaml describes each calibration point:
    points:
      cal_1: { position: [2.0, 2.0, 1.0], csv_glob: "data/cal_1_*.csv" }
      cal_2: { position: [12.0, 2.0, 1.0], csv_glob: "data/cal_2_*.csv" }
      cal_3: { position: [12.0, 7.0, 1.0], csv_glob: "data/cal_3_*.csv" }
      cal_4: { position: [2.0, 7.0, 1.0], csv_glob: "data/cal_4_*.csv" }
"""
import argparse
import csv
import glob
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml


def load_csv_rssi(csv_paths: list, target_bssids: dict) -> dict:
    """
    Read multiple CSVs, return {ap_key: {band: [rssi, ...]}}
    target_bssids: {bssid_lower: ap_key}
    """
    out = defaultdict(lambda: defaultdict(list))
    for p in csv_paths:
        with open(p) as f:
            reader = csv.DictReader(f)
            for row in reader:
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


def fit_path_loss(distances: list, rssis: list) -> tuple:
    """
    Fit RSSI = A - 10*n*log10(d)
    Linear LS: y = A - 10n * x, where x = log10(d), y = RSSI

    Returns (A, n, rmse)
    """
    if len(distances) < 2:
        return None, None, None

    x = np.log10(np.array(distances))
    y = np.array(rssis)

    # numpy polyfit: highest power first
    coef = np.polyfit(x, y, 1)
    b, a = coef
    A = a
    n = -b / 10.0

    y_pred = a + b * x
    rmse = float(np.sqrt(np.mean((y - y_pred) ** 2)))

    return float(A), float(n), rmse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--calib-file", required=True,
                    help="calibration points definition (YAML)")
    ap.add_argument("--write", action="store_true",
                    help="write fitted results back to config.yaml")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    with open(args.calib_file) as f:
        calib = yaml.safe_load(f)

    # Build bssid -> ap_key map
    target_bssids = {}
    ap_positions = {}
    for ap_key, info in cfg["infrastructure"].items():
        bssid = info["bssid"]
        if bssid and bssid != "XX:XX:XX:XX:XX:XX":
            target_bssids[bssid.lower()] = ap_key
            ap_positions[ap_key] = np.array(info["position"], dtype=float)

    print(f"[*] Identified {len(target_bssids)} APs, "
          f"{len(calib['points'])} calibration points\n")

    # per_ap[ap_key][band] = [(distance, rssi_median), ...]
    per_ap = defaultdict(lambda: defaultdict(list))

    for cal_name, cal_info in calib["points"].items():
        cal_pos = np.array(cal_info["position"], dtype=float)
        csv_files = glob.glob(cal_info["csv_glob"])
        if not csv_files:
            print(f"[!] {cal_name}: no CSV found ({cal_info['csv_glob']})")
            continue

        rssi_data = load_csv_rssi(csv_files, target_bssids)

        print(f"[{cal_name}] pos={cal_pos.tolist()}, "
              f"CSV files={len(csv_files)}")

        for ap_key, ap_pos in ap_positions.items():
            d = float(np.linalg.norm(cal_pos - ap_pos))
            for band, samples in rssi_data.get(ap_key, {}).items():
                if len(samples) < 3:
                    continue
                rssi_med = float(np.median(samples))
                per_ap[ap_key][band].append((d, rssi_med, len(samples)))
                print(f"    {ap_key:<10} band={band:<3} d={d:5.2f}m "
                      f"rssi={rssi_med:6.1f} (n={len(samples)})")

    # Fit
    print("\n" + "=" * 70)
    print("Path Loss Fit Results")
    print("=" * 70)
    print(f"{'AP':<10} {'Band':<5} {'A (1m RSSI)':>12} {'n':>6} {'RMSE':>6} {'pts':>4}")
    print("-" * 70)

    results = defaultdict(dict)
    for ap_key in sorted(per_ap):
        for band in sorted(per_ap[ap_key]):
            points = per_ap[ap_key][band]
            if len(points) < 2:
                print(f"{ap_key:<10} {band:<5} not enough points ({len(points)})")
                continue
            distances = [p[0] for p in points]
            rssis = [p[1] for p in points]
            A, n, rmse = fit_path_loss(distances, rssis)
            print(f"{ap_key:<10} {band:<5} {A:>12.2f} {n:>6.2f} "
                  f"{rmse:>6.2f} {len(points):>4}")
            results[ap_key][f"A_{band}"] = round(A, 2)
            results[ap_key][f"n_{band}"] = round(n, 2)
            results[ap_key][f"rmse_{band}"] = round(rmse, 2)

    # Health check
    print("\n--- Calibration Health Check ---")
    for ap_key, params in results.items():
        warnings = []
        for band in ["2_4", "5"]:
            n_val = params.get(f"n_{band}")
            rmse_val = params.get(f"rmse_{band}")
            if n_val is not None:
                if n_val < 1.5 or n_val > 5.0:
                    warnings.append(f"band {band} n={n_val} abnormal (typical 2-4)")
                if rmse_val and rmse_val > 6.0:
                    warnings.append(f"band {band} RMSE={rmse_val} too large, poor fit")
        if warnings:
            print(f"  [{ap_key}]")
            for w in warnings:
                print(f"    [WARN] {w}")

    # Write back
    if args.write:
        for ap_key, params in results.items():
            if ap_key in cfg["path_loss"]["per_ap"]:
                cfg["path_loss"]["per_ap"][ap_key].update({
                    "A_2_4": params.get("A_2_4"),
                    "n_2_4": params.get("n_2_4"),
                    "A_5": params.get("A_5"),
                    "n_5": params.get("n_5"),
                })

        with open(args.config, "w") as f:
            yaml.dump(cfg, f, sort_keys=False, allow_unicode=True)
        print(f"\n[+] Wrote back to {args.config}")
    else:
        print(f"\n[*] Preview mode (no write). Add --write to commit.")


if __name__ == "__main__":
    main()