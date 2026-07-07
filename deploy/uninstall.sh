#!/usr/bin/env bash
# Uninstall StowTrace. Preserves data by default; pass --purge to wipe it.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Run with sudo."
  exit 1
fi

PURGE=0
if [ "${1:-}" = "--purge" ]; then PURGE=1; fi

echo "==> Stopping service"
systemctl stop st-backend 2>/dev/null || true
systemctl disable st-backend 2>/dev/null || true
rm -f /etc/systemd/system/st-backend.service
systemctl daemon-reload

echo "==> Removing nginx config"
rm -f /etc/nginx/sites-enabled/stowtrace
rm -f /etc/nginx/sites-available/stowtrace
nginx -t >/dev/null 2>&1 && systemctl reload nginx || true

echo "==> Removing web files"
rm -f /var/www/html/index.html
rm -rf /var/www/html/forge /var/www/html/inventory /var/www/html/wifi

echo "==> Removing WiFi sudoers rule"
rm -f /etc/sudoers.d/stowtrace-wifi

echo "==> Removing app directory"
rm -rf /opt/stowtrace

echo "==> Removing TLS cert"
rm -rf /etc/ssl/stowtrace

echo "==> Removing udev rule"
rm -f /etc/udev/rules.d/50-brother-ptouch.rules
udevadm control --reload-rules

if [ "$PURGE" = "1" ]; then
  echo "==> Purging data (--purge passed)"
  rm -rf /var/lib/stowtrace
else
  echo
  echo "Your inventory data is preserved at:"
  echo "  /var/lib/stowtrace/"
  echo "Run with --purge to delete it too."
fi

echo
echo "âœ“ Uninstalled."
