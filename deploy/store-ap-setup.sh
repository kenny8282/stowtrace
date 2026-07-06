#!/usr/bin/env bash
# store-ap-setup.sh — turn this Pi into a standalone inventory appliance:
#   eth0  = uplink to the store's router (internet for updates/sync)
#   wlan0 = the Pi's OWN WiFi network that staff devices join to use the app
#
# Run once, over SSH:  sudo bash store-ap-setup.sh "SSID-NAME" "wifi-password"
# Idempotent — rerun any time to change the SSID or password.
#
# After it runs, connect a phone to the SSID and open:  http://192.168.4.1/
# (or https://<hostname>.local). The AP survives reboots.
set -euo pipefail

SSID="${1:-}"
PASS="${2:-}"
if [[ -z "$SSID" || -z "$PASS" ]]; then
  echo "usage: sudo bash store-ap-setup.sh \"SSID\" \"password (8+ chars)\"" >&2
  exit 1
fi
if [[ ${#PASS} -lt 8 ]]; then
  echo "WiFi password must be at least 8 characters" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

echo "==> Setting WiFi regulatory domain (required for AP mode)"
raspi-config nonint do_wifi_country US 2>/dev/null || iw reg set US || true

echo "==> Unblocking WiFi radio"
rfkill unblock wifi || true

echo "==> Creating/refreshing the access point connection"
nmcli connection delete store-ap >/dev/null 2>&1 || true
nmcli connection add type wifi ifname wlan0 con-name store-ap \
      autoconnect yes ssid "$SSID" >/dev/null
nmcli connection modify store-ap \
      802-11-wireless.mode ap \
      802-11-wireless.band bg \
      802-11-wireless.powersave 2 \
      ipv4.method shared \
      ipv4.addresses 192.168.4.1/24 \
      wifi-sec.key-mgmt wpa-psk \
      wifi-sec.psk "$PASS"

echo "==> Friendly address: making http://inv.store resolve to this box"
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/stowtrace-inv.conf <<'EOF'
# Devices on the inventory WiFi can reach the app at a memorable name
address=/inv.store/192.168.4.1
address=/inventory.store/192.168.4.1
EOF

echo "==> Bringing the network up"
systemctl restart NetworkManager
sleep 2
nmcli connection up store-ap >/dev/null

echo
echo "✓ Access point is live."
echo "    SSID:     $SSID"
echo "    App URL:  http://inv.store/  (or http://192.168.4.1/)"
echo "    Uplink:   eth0 (leave it plugged into the store router)"
echo
echo "One-time per staff device: open the URL, then 'Add to Home Screen' —"
echo "from then on the inventory is a tap-an-icon app."
echo
echo "Notes:"
echo "  - 'shared' mode NATs AP clients out through eth0, so devices on the"
echo "    inventory WiFi also get internet — keeps phones happily connected."
echo "    To make the AP app-only (no internet for clients), run:"
echo "      sudo nmcli connection modify store-ap ipv4.method manual && sudo nmcli connection up store-ap"
echo "    (then clients must use http://192.168.4.1/ and phones may warn 'no internet')"
echo "  - Change SSID/password any time by rerunning this script."
