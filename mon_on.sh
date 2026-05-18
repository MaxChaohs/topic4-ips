#!/bin/bash
# mon_on.sh - switch wlan1 to monitor mode
# Usage: sudo ./mon_on.sh [channel]
set -e

IFACE=wlan1
CH=$1

echo "[*] Switching $IFACE to monitor mode..."
ip link set $IFACE down
iw dev $IFACE set type monitor
ip link set $IFACE up

if [ -n "$CH" ]; then
    iw dev $IFACE set channel $CH
    echo "[+] $IFACE set to channel $CH"
fi

echo ""
iw dev $IFACE info
