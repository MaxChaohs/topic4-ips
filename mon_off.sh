#!/bin/bash
# mon_off.sh - restore wlan1 to managed mode and reconnect
# Usage: sudo ./mon_off.sh
set -e

IFACE=wlan1

echo "[*] Restoring $IFACE to managed mode..."
ip link set $IFACE down
iw dev $IFACE set type managed
ip link set $IFACE up

echo "[*] Reconnecting..."
if wpa_cli -i $IFACE reconnect >/dev/null 2>&1; then
    echo "[+] Triggered reconnect via wpa_cli"
elif systemctl is-active wpa_supplicant >/dev/null 2>&1; then
    systemctl restart wpa_supplicant
    echo "[+] Restarted wpa_supplicant"
fi

echo "[*] Waiting for network..."
for i in $(seq 1 15); do
    sleep 1
    if ip -4 addr show $IFACE | grep -q "inet "; then
        echo "[+] $IFACE got IP:"
        ip -4 addr show $IFACE | grep "inet "
        exit 0
    fi
    echo -n "."
done

echo ""
echo "[!] $IFACE has no IP yet. Try: sudo dhclient $IFACE"
