#!/bin/bash
# verify_all.sh - one-shot field validation pipeline
# Usage: sudo ./verify_all.sh
#
# Steps:
#   1. Environment check (Python packages, tshark, iw)
#   2. Switch to monitor -> test tshark -> restore
#   3. Run scan_channels.py to scan home AP
#
# SSH will not break, but network will be lost during monitor mode.
# Total run time: ~80 seconds

IFACE=wlan1
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
cd "$SCRIPT_DIR"

echo "==========================================================="
echo "  WiFi Positioning System - Full Field Verification"
echo "==========================================================="
echo ""

# ---- Step 1: Environment check ----
echo "> Step 1: Environment check"
echo "---------------------------"

python3 -c "import numpy, scipy, yaml" 2>&1
if [ $? -ne 0 ]; then
    echo "  FAIL: Python packages missing"
    exit 1
fi
echo "  OK: numpy / scipy / yaml"

command -v tshark >/dev/null && echo "  OK: tshark: $(tshark --version | head -1)"
command -v iw >/dev/null && echo "  OK: iw: $(iw --version)"

if ! iw dev $IFACE info >/dev/null 2>&1; then
    echo "  FAIL: Interface $IFACE not found"
    exit 1
fi
echo "  OK: Interface $IFACE exists"

for f in config.yaml scan_channels.py collect_rssi.py calibrate.py solve.py; do
    if [ ! -f "$f" ]; then
        echo "  FAIL: Missing file: $f"
        exit 1
    fi
done
echo "  OK: All Python scripts present"

echo ""

# ---- Step 2: Switch to monitor ----
echo "> Step 2: Switch to monitor mode + channel 6"
echo "---------------------------"
ip link set $IFACE down
iw dev $IFACE set type monitor
ip link set $IFACE up
iw dev $IFACE set channel 6

TYPE=$(iw dev $IFACE info | grep "type" | awk '{print $2}')
if [ "$TYPE" = "monitor" ]; then
    echo "  OK: $IFACE is monitor mode, channel 6"
else
    echo "  FAIL: monitor switch failed (type=$TYPE)"
    exit 1
fi
echo ""

# ---- Step 3: tshark field verification ----
echo "> Step 3: tshark field verification (10s passive scan)"
echo "---------------------------"

OUT=$(tshark -i $IFACE -I -a duration:10 \
  -Y "wlan.fc.type_subtype == 0x08" \
  -T fields \
  -e wlan.bssid \
  -e wlan_radio.signal_dbm \
  -e radiotap.dbm_antsignal \
  -E separator='|' 2>/dev/null)

TOTAL=$(echo "$OUT" | grep -v '^$' | wc -l)
COL2=$(echo "$OUT" | awk -F'|' '$2 != "" {c++} END {print c+0}')
COL3=$(echo "$OUT" | awk -F'|' '$3 != "" {c++} END {print c+0}')

echo "  beacons received: $TOTAL"
echo "  wlan_radio.signal_dbm filled: $COL2"
echo "  radiotap.dbm_antsignal filled: $COL3"

if [ "$TOTAL" -eq 0 ]; then
    echo "  FAIL: No beacons received"
    ip link set $IFACE down
    iw dev $IFACE set type managed
    ip link set $IFACE up
    exit 1
fi

if [ "$COL2" -ge "$COL3" ] && [ "$COL2" -gt 0 ]; then
    FIELD="wlan_radio.signal_dbm"
    echo "  OK: use wlan_radio.signal_dbm (no .py change)"
elif [ "$COL3" -gt 0 ]; then
    FIELD="radiotap.dbm_antsignal"
    echo "  WARN: use radiotap.dbm_antsignal, auto-patching .py..."
    sed -i.bak 's/wlan_radio\.signal_dbm/radiotap.dbm_antsignal/g' \
        collect_rssi.py scan_channels.py
    echo "  OK: backed up to .bak and replaced field name"
fi
echo ""

# ---- Step 4: scan_channels.py against home AP ----
echo "> Step 4: scan_channels.py (30s, prefix MD402)"
echo "---------------------------"
echo "  Note: For contest, change --ssid-prefix to infra_"
echo ""

python3 scan_channels.py --iface $IFACE --duration 30 --ssid-prefix MD402 --no-restore

echo ""

# ---- Step 5: Restore ----
echo "> Step 5: Restore $IFACE to managed + reconnect"
echo "---------------------------"
ip link set $IFACE down
iw dev $IFACE set type managed
ip link set $IFACE up

if wpa_cli -i $IFACE reconnect >/dev/null 2>&1; then
    echo "  [*] Triggered reconnect via wpa_cli"
elif systemctl is-active wpa_supplicant >/dev/null 2>&1; then
    systemctl restart wpa_supplicant
    echo "  [*] Restarted wpa_supplicant"
fi

echo "  [*] Waiting for network..."
for i in $(seq 1 20); do
    sleep 1
    if ip -4 addr show $IFACE | grep -q "inet "; then
        IP=$(ip -4 addr show $IFACE | grep "inet " | awk '{print $2}')
        echo "  OK: $IFACE got IP: $IP"
        break
    fi
    echo -n "."
done

echo ""
echo "==========================================================="
echo "  Verification done"
echo "==========================================================="
