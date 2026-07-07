#!/usr/bin/env bash
# store-ap-setup.sh — turn this Pi into a standalone kiosk inventory appliance.
#   eth0  = uplink to the store's router (internet for the Pi itself)
#   wlan0 = the Pi's OWN WiFi that staff phones join to use the app
#
# KIOSK MODE (Option A): the AP serves the app only. Phones that join it
# auto-open StowTrace (captive-portal popup) and do NOT route internet through
# the Pi. Fast, reliable auto-open, no store WiFi password ever needed.
#
# Usage (over SSH):  sudo bash store-ap-setup.sh "SSID" "wifi-password"
#   defaults to: stowtrace / stowtrace
# Idempotent — rerun any time to change SSID/password.
set -euo pipefail

SSID="${1:-stowtrace}"
PASS="${2:-stowtrace}"
if [[ ${#PASS} -lt 8 ]]; then
  echo "WiFi password must be at least 8 characters (got '${PASS}')" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then echo "run with sudo" >&2; exit 1; fi

AP_IP="192.168.4.1"

echo "==> WiFi regulatory domain (required for AP mode)"
raspi-config nonint do_wifi_country US 2>/dev/null || iw reg set US || true
rfkill unblock wifi || true
nmcli radio wifi on || true

echo "==> Creating/refreshing the access point (kiosk mode)"
nmcli connection delete store-ap >/dev/null 2>&1 || true
nmcli connection add type wifi ifname wlan0 con-name store-ap \
      autoconnect yes ssid "$SSID" >/dev/null
nmcli connection modify store-ap \
      802-11-wireless.mode ap \
      802-11-wireless.band bg \
      802-11-wireless.powersave 2 \
      ipv4.method shared \
      ipv4.addresses ${AP_IP}/24 \
      wifi-sec.key-mgmt wpa-psk \
      wifi-sec.psk "$PASS"

# ---- Captive portal: resolve EVERY domain to the Pi on the AP subnet -------
# NetworkManager's 'shared' mode runs a dnsmasq instance. We add config so any
# DNS query from an AP client returns the Pi's IP. Then the phone's connectivity
# check hits our nginx, which returns the captive-portal redirect to the app.
echo "==> Captive-portal DNS (auto-open the app on connect)"
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/stowtrace-portal.conf <<EOF
# Kiosk captive portal: every lookup on the AP resolves to the Pi.
address=/#/${AP_IP}
# Friendly names for the app
address=/st.local/${AP_IP}
address=/stowtrace.local/${AP_IP}
address=/st.store/${AP_IP}
# Hand the Pi out as the DNS server to AP clients
dhcp-option=6,${AP_IP}
EOF

echo "==> Bringing the network up"
systemctl restart NetworkManager
sleep 3
nmcli connection up store-ap >/dev/null || true

echo
echo "================================================================"
echo "  StowTrace kiosk AP is live"
echo "================================================================"
echo "    SSID:      $SSID"
echo "    Password:  $PASS"
echo "    App:       auto-opens on connect (or http://st.local/)"
echo "    Uplink:    eth0 (keep it plugged into the store router)"
echo
echo "  A phone joining '$SSID' should auto-pop StowTrace."
echo "  If not, open any http:// site and it redirects to the app."
echo "  'Add to Home Screen' once = tap-an-icon app forever."
echo
echo "  Change SSID/password later: rerun this, or via the app's WiFi tab."
echo
