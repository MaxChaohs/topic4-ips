#!/bin/bash
# test_tshark.sh - verify which tshark field carries RSSI
# Usage: sudo ./test_tshark.sh
#
# Precondition: wlan1 is already in monitor mode (run mon_on.sh 6 first)
#
# Result:
#   - If "wlan_radio.signal_dbm" has values -> collect_rssi.py unchanged
#   - If only "radiotap.dbm_antsignal" has values -> .py needs update
set -e

IFACE=wlan1

# Verify monitor mode
TYPE=$(iw dev $IFACE info | grep "type" | awk '{print $2}')
if [ "$TYPE" != "monitor" ]; then
    echo "[!] $IFACE not in monitor mode (current: $TYPE)"
    echo "    Run first: sudo ./mon_on.sh 6"
    exit 1
fi

# Set channel if missing
CH_INFO=$(iw dev $IFACE info | grep "channel" || echo "")
if [ -z "$CH_INFO" ]; then
    echo "[*] Setting channel 6"
    iw dev $IFACE set channel 6
fi

echo "[*] Listening to beacons for 10 seconds, comparing fields..."
echo "    bssid | wlan_radio.signal_dbm | radiotap.dbm_antsignal"
echo "---------------------------------------------------"

OUT=$(tshark -i $IFACE -I -a duration:10 \
  -Y "wlan.fc.type_subtype == 0x08" \
  -T fields \
  -e wlan.bssid \
  -e wlan_radio.signal_dbm \
  -e radiotap.dbm_antsignal \
  -E separator='|' 2>/dev/null)

echo "$OUT" | head -15

if [ -z "$OUT" ]; then
    echo ""
    echo "[!] No beacons received. Possible causes:"
    echo "    1. No AP nearby (unlikely)"
    echo "    2. Monitor mode not set correctly"
    echo "    3. No AP on this channel (try other channels)"
    exit 1
fi

TOTAL=$(echo "$OUT" | wc -l)
COL2_FILLED=$(echo "$OUT" | awk -F'|' '$2 != "" {count++} END {print count+0}')
COL3_FILLED=$(echo "$OUT" | awk -F'|' '$3 != "" {count++} END {print count+0}')

echo ""
echo "----- Stats -----"
echo "Total beacons: $TOTAL"
echo "wlan_radio.signal_dbm filled: $COL2_FILLED"
echo "radiotap.dbm_antsignal filled: $COL3_FILLED"
echo ""

if [ "$COL2_FILLED" -gt 0 ] && [ "$COL2_FILLED" -ge "$COL3_FILLED" ]; then
    echo "[+] Use wlan_radio.signal_dbm (collect_rssi.py unchanged)"
elif [ "$COL3_FILLED" -gt 0 ]; then
    echo "[!] Use radiotap.dbm_antsignal"
    echo "    Need to update collect_rssi.py and scan_channels.py:"
    echo "    Replace 'wlan_radio.signal_dbm' with 'radiotap.dbm_antsignal'"
    echo ""
    echo "    Run this to auto-replace (backup made as .bak):"
    echo "    sed -i.bak 's/wlan_radio\\.signal_dbm/radiotap.dbm_antsignal/g' collect_rssi.py scan_channels.py"
else
    echo "[!] Both fields empty, abnormal"
fi
