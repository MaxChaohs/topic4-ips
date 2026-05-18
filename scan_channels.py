#!/usr/bin/env python3
"""
scan_channels.py - pre-contest channel reconnaissance

Usage:
    sudo python3 scan_channels.py --duration 60
    sudo python3 scan_channels.py --bands 2.4

Precondition:
    wlan1 must be in monitor mode FIRST. Use mon_on.sh or web UI 'Set Monitor'.
    (We do NOT use airmon-ng; that would kill wpa_supplicant and break wlan0/SSH.)

Function:
    1. Use tshark to passively scan for `duration` seconds, hopping channels
    2. Filter SSIDs starting with 'infra_'
    3. Show BSSID / channel / frequency / avg RSSI
    4. Output updated config.yaml.updated (manual rename to apply)
"""
import argparse
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml


def freq_to_channel(freq_mhz: int) -> int:
    if 2412 <= freq_mhz <= 2484:
        if freq_mhz == 2484:
            return 14
        return (freq_mhz - 2407) // 5
    elif 5170 <= freq_mhz <= 5825:
        return (freq_mhz - 5000) // 5
    elif 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    return 0


def check_monitor(iface: str) -> bool:
    result = subprocess.run(
        ["iw", "dev", iface, "info"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    return "type monitor" in result.stdout


def channel_hop(iface: str, channels: list, dwell_sec: float, stop_event):
    while not stop_event.is_set():
        for ch in channels:
            if stop_event.is_set():
                break
            try:
                subprocess.run(
                    ["iw", "dev", iface, "set", "channel", str(ch)],
                    check=False, capture_output=True
                )
            except Exception:
                pass
            time.sleep(dwell_sec)


def scan_beacons(iface: str, duration: int) -> dict:
    print(f"[*] Listening for beacons for {duration} seconds...")

    cmd = [
        "tshark", "-i", iface, "-l",
        "-a", f"duration:{duration}",
        "-Y", "wlan.fc.type_subtype == 0x08",
        "-T", "fields",
        "-e", "wlan.bssid",
        "-e", "wlan.ssid",
        "-e", "wlan_radio.channel",
        "-e", "wlan_radio.frequency",
        "-e", "wlan_radio.signal_dbm",
        "-E", "separator=|",
    ]

    results = defaultdict(lambda: {"ssid": "", "freqs": defaultdict(int),
                                    "channels": defaultdict(int),
                                    "rssi_samples": []})

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        start = time.time()
        line_count = 0
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            bssid, ssid_hex, channel, freq, rssi = parts[:5]
            if not bssid:
                continue

            ssid = ""
            if ssid_hex:
                try:
                    if all(c in "0123456789abcdefABCDEF:" for c in ssid_hex.replace(":", "")):
                        ssid = bytes.fromhex(ssid_hex.replace(":", "")).decode(errors="ignore")
                    else:
                        ssid = ssid_hex
                except Exception:
                    ssid = ssid_hex

            entry = results[bssid]
            if ssid and not entry["ssid"]:
                entry["ssid"] = ssid
            try:
                entry["freqs"][int(freq)] += 1
                entry["channels"][int(channel)] += 1
                entry["rssi_samples"].append(int(rssi))
            except (ValueError, TypeError):
                continue

            line_count += 1
            if line_count % 100 == 0:
                elapsed = time.time() - start
                print(f"    received {line_count} beacons, elapsed {elapsed:.1f}s, "
                      f"unique BSSIDs: {len(results)}")

        proc.wait(timeout=5)
    except KeyboardInterrupt:
        proc.terminate()
        print("\n[!] User interrupted")

    return dict(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlan1",
                    help="monitor interface name (default: wlan1; was wlan1mon when using airmon-ng)")
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--ssid-prefix", default="infra_")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--bands", default="2.4", choices=["2.4", "5", "both"])
    args = ap.parse_args()

    iface = args.iface

    if not check_monitor(iface):
        print(f"[!] {iface} is not in monitor mode")
        print(f"    Run first: sudo bash mon_on.sh   (or click 'Set Monitor' in web UI)")
        sys.exit(1)

    print(f"[*] {iface} is in monitor mode, starting scan")

    import threading
    channels_24 = list(range(1, 12))
    channels_5 = [36, 40, 44, 48, 149, 153, 157, 161, 165]
    if args.bands == "2.4":
        all_channels = channels_24
    elif args.bands == "5":
        all_channels = channels_5
    else:
        all_channels = channels_24 + channels_5
    print(f"[*] Scanning bands: {args.bands}, channels: {all_channels}")

    stop_event = threading.Event()
    hop_thread = threading.Thread(
        target=channel_hop, args=(iface, all_channels, 0.3, stop_event),
        daemon=True
    )
    hop_thread.start()

    results = scan_beacons(iface, args.duration)

    stop_event.set()
    hop_thread.join(timeout=2)

    print("\n" + "=" * 70)
    print(f"Scan done, detected {len(results)} BSSIDs total")
    print("=" * 70)

    targets = {}
    for bssid, info in results.items():
        ssid = info["ssid"]
        if ssid and ssid.startswith(args.ssid_prefix):
            main_freq = max(info["freqs"], key=info["freqs"].get)
            main_ch = max(info["channels"], key=info["channels"].get)
            rssi_samples = info["rssi_samples"]
            avg_rssi = sum(rssi_samples) / len(rssi_samples) if rssi_samples else 0
            targets[ssid] = {
                "bssid": bssid,
                "channel": main_ch,
                "frequency": main_freq,
                "samples": len(rssi_samples),
                "avg_rssi": round(avg_rssi, 1),
            }

    if not targets:
        print(f"\n[!] No AP found with SSID prefix '{args.ssid_prefix}'")
        print(f"\nShowing all detected SSIDs (first 20):")
        for bssid, info in list(results.items())[:20]:
            ssid = info["ssid"] or "<hidden>"
            avg_rssi = sum(info["rssi_samples"]) / max(len(info["rssi_samples"]), 1)
            main_ch = max(info["channels"], key=info["channels"].get) if info["channels"] else 0
            print(f"  {bssid}  ch={main_ch:3}  rssi={avg_rssi:6.1f}  SSID={ssid}")
        sys.exit(1)

    print(f"\nFound {len(targets)} target APs:\n")
    print(f"{'SSID':<12} {'BSSID':<20} {'CH':>4} {'Freq':>6} {'Samples':>8} {'AvgRSSI':>8}")
    print("-" * 70)
    for ssid in sorted(targets):
        t = targets[ssid]
        print(f"{ssid:<12} {t['bssid']:<20} {t['channel']:>4} "
              f"{t['frequency']:>6} {t['samples']:>8} {t['avg_rssi']:>8.1f}")

    used_channels = sorted({t["channel"] for t in targets.values()})
    bands = set()
    for t in targets.values():
        if t["frequency"] < 3000:
            bands.add("2.4G")
        else:
            bands.add("5G")

    print(f"\nChannel distribution: {used_channels}")
    print(f"Bands: {sorted(bands)}")

    if len(used_channels) == 1:
        print("[OK] All 4 APs on same channel, can lock channel for max samples")
    elif len(used_channels) <= 3:
        print(f"[WARN] Spread across {len(used_channels)} channels, need hopping (4-5s dwell)")
    else:
        print(f"[WARN] Channels scattered ({len(used_channels)}), samples/AP will be low in 20s")

    cfg_path = Path(args.config)
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)

        for ssid_key, t in targets.items():
            if ssid_key in cfg["infrastructure"]:
                cfg["infrastructure"][ssid_key]["bssid"] = t["bssid"]
                cfg["infrastructure"][ssid_key]["channel"] = t["channel"]
                cfg["infrastructure"][ssid_key]["frequency"] = t["frequency"]

        cfg["collection"]["channels_to_hop"] = used_channels
        # Also fix interface name if still wlan1mon
        if cfg["collection"].get("interface") == "wlan1mon":
            cfg["collection"]["interface"] = "wlan1"
            cfg["collection"]["monitor_interface"] = "wlan1"
            print(f"\n[*] Also corrected interface name: wlan1mon -> wlan1")

        out_path = cfg_path.with_suffix(".yaml.updated")
        with open(out_path, "w") as f:
            yaml.dump(cfg, f, sort_keys=False, allow_unicode=True)
        print(f"\n[+] Updated config written to: {out_path}")
        print(f"    To apply: mv {out_path} {cfg_path}")
    else:
        print(f"\n[!] {cfg_path} not found, skip config update")


if __name__ == "__main__":
    main()
