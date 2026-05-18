#!/usr/bin/env python3
"""
collect_rssi.py - on-site RSSI collection

Usage (run once per (point, round), 14 total):
    sudo python3 collect_rssi.py --point P1 --round 1
    sudo python3 collect_rssi.py --point P1 --round 2
    sudo python3 collect_rssi.py --point P2 --round 1
    ...
    Calibration points:
    sudo python3 collect_rssi.py --point cal_1 --round 1
    sudo python3 collect_rssi.py --point cal_1 --round 2

Output filename format:
    Mission points (P1~P7):  data/P{n}_R{r}.csv          (no timestamp, overwrite)
    Calibration (cal_*):     data/cal_{n}_R{r}_{ts}.csv  (with timestamp for accumulation)

The reason: mission grid in web UI tracks completion by P{n}_R{r}.csv exact match.
Calibration can have multiple samples per round, so keep timestamp to avoid overwrite.

Important:
    - No real-time solving, only raw data collection
    - Solving is done after contest by solve.py
    - Hard stop at 20 seconds, no overrun
"""
import argparse
import csv
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def channel_hop(iface: str, channels: list, dwell_ms: int, stop_event: threading.Event):
    """Background channel hopping. dwell_ms = ms per channel"""
    dwell_sec = dwell_ms / 1000.0
    while not stop_event.is_set():
        for ch in channels:
            if stop_event.is_set():
                return
            subprocess.run(
                ["iw", "dev", iface, "set", "channel", str(ch)],
                check=False, capture_output=True, timeout=2
            )
            slept = 0.0
            while slept < dwell_sec and not stop_event.is_set():
                time.sleep(0.05)
                slept += 0.05


def collect(iface: str, duration: int, target_bssids: set, out_path: Path) -> tuple:
    """
    Listen for `duration` seconds, write CSV.
    Returns: (total_samples, per_ap_count_dict)
    """
    cmd = [
        "tshark",
        "-i", iface,
        "-l",
        "-a", f"duration:{duration}",
        "-Y", "wlan.fc.type_subtype == 0x08",  # beacon
        "-T", "fields",
        "-e", "frame.time_epoch",
        "-e", "wlan.bssid",
        "-e", "wlan_radio.channel",
        "-e", "wlan_radio.frequency",
        "-e", "wlan_radio.signal_dbm",
        "-E", "separator=|",
    ]

    target_bssids_lower = {b.lower() for b in target_bssids}

    written = 0
    per_ap_count = {b: 0 for b in target_bssids_lower}

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "bssid", "channel", "frequency", "rssi"])

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) < 5:
                    continue
                ts, bssid, ch, freq, rssi = parts[:5]
                if not bssid:
                    continue
                bssid_l = bssid.lower()
                if bssid_l not in target_bssids_lower:
                    continue
                try:
                    writer.writerow([ts, bssid_l, ch, freq, rssi])
                    per_ap_count[bssid_l] += 1
                    written += 1
                except Exception:
                    continue
            proc.wait(timeout=5)
        except Exception as e:
            print(f"[!] tshark exception: {e}", file=sys.stderr)
            proc.terminate()

    return written, per_ap_count


def determine_filename(point: str, rnd: int, outdir: Path) -> Path:
    """
    Mission points (P1~P7):  data/P{n}_R{r}.csv (no timestamp)
    Calibration (cal_*):     data/cal_{n}_R{r}_{ts}.csv (with timestamp)
    Other (e.g. test):       same as cal_ (with timestamp)
    """
    if re.fullmatch(r"P[1-7]", point):
        return outdir / f"{point}_R{rnd}.csv"
    else:
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        return outdir / f"{point}_R{rnd}_{ts_str}.csv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--point", required=True,
                    help="point name (P1~P7 for mission, cal_N for calibration)")
    ap.add_argument("--round", type=int, required=True, choices=[1, 2],
                    help="round number (1 or 2)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--duration", type=int, default=None,
                    help="override duration (default reads from config)")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't actually run, just print intent")
    args = ap.parse_args()

    cfg = load_config(args.config)
    duration = args.duration or cfg["collection"]["duration_seconds"]
    iface = cfg["collection"]["interface"]
    channels = cfg["collection"]["channels_to_hop"]
    dwell_ms = cfg["collection"]["channel_hop_dwell_ms"]

    # Collect target BSSIDs
    target_bssids = set()
    for key, info in cfg["infrastructure"].items():
        bssid = info["bssid"]
        if bssid and bssid != "XX:XX:XX:XX:XX:XX":
            target_bssids.add(bssid)

    if not target_bssids:
        print("[!] No BSSIDs in config.yaml, run scan_channels.py first", file=sys.stderr)
        sys.exit(1)

    if not channels:
        print("[!] No channels_to_hop in config.yaml", file=sys.stderr)
        sys.exit(1)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    out_path = determine_filename(args.point, args.round, outdir)

    print(f"================================================")
    print(f"  Point: {args.point}   Round: {args.round}")
    print(f"  Iface: {iface}  Channels: {channels}  Dwell: {dwell_ms}ms")
    print(f"  Target BSSIDs: {len(target_bssids)}")
    print(f"  Output: {out_path}")
    print(f"================================================")

    if args.dry_run:
        return

    # Start channel hopping (only if multiple channels)
    stop_event = threading.Event()
    hop_thread = None
    if len(channels) > 1:
        hop_thread = threading.Thread(
            target=channel_hop,
            args=(iface, channels, dwell_ms, stop_event),
            daemon=True
        )
        hop_thread.start()
    else:
        # Lock single channel
        subprocess.run(
            ["iw", "dev", iface, "set", "channel", str(channels[0])],
            check=False
        )
        print(f"[*] Locked channel {channels[0]}")

    print(f"[*] Collecting for {duration}s... ({time.strftime('%H:%M:%S')})")
    t0 = time.time()
    written, per_ap = collect(iface, duration, target_bssids, out_path)
    elapsed = time.time() - t0

    stop_event.set()
    if hop_thread:
        hop_thread.join(timeout=2)

    print(f"[+] Done (actual elapsed {elapsed:.1f}s)")
    print(f"    Total samples: {written}")
    print(f"    Per-AP counts:")

    # Reverse lookup BSSID -> SSID
    bssid_to_ssid = {}
    for ssid, info in cfg["infrastructure"].items():
        if info["bssid"]:
            bssid_to_ssid[info["bssid"].lower()] = ssid

    for bssid, count in sorted(per_ap.items(), key=lambda x: -x[1]):
        ssid = bssid_to_ssid.get(bssid, "?")
        if count >= 5:
            mark = "OK "
        elif count > 0:
            mark = "LOW"
        else:
            mark = "BAD"
        print(f"      [{mark}] {ssid:<10} {bssid}  {count:>4} samples")

    # Health check
    weak_aps = [k for k, c in per_ap.items() if c < 5]
    if weak_aps:
        print(f"\n[!] WARNING: {len(weak_aps)} AP(s) with < 5 samples, "
              f"consider redoing this round")


if __name__ == "__main__":
    main()
