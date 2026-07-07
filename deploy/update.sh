#!/usr/bin/env bash
# Update StowTrace to the latest version.
# Uses the persistent git clone at /opt/stowtrace/src/.
# Idempotent â€” safe to run when no update is available.
set -euo pipefail

INSTALL_DIR="/opt/stowtrace"
SRC_DIR="$INSTALL_DIR/src"
WEB_DIR="/var/www/html"
SERVICE_USER="$(stat -c '%U' "$INSTALL_DIR/stowtrace_backend.py" 2>/dev/null || echo "$SUDO_USER")"
REPO_BRANCH="${STOWTRACE_REPO_BRANCH:-main}"

if [ "$EUID" -ne 0 ]; then
  echo "Run with sudo: sudo bash $INSTALL_DIR/update.sh"
  exit 1
fi

# If no persistent source dir exists, fall back to fetching to /tmp
if [ ! -d "$SRC_DIR/.git" ]; then
  echo "==> No persistent source dir at $SRC_DIR â€” falling back to fresh clone"
  SRC_DIR="/tmp/stowtrace-update-$$"
  REPO_URL="${STOWTRACE_REPO_URL:-https://github.com/kenny8282/stowtrace.git}"
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$SRC_DIR"
  CLEANUP=1
else
  echo "==> Pulling latest from origin/$REPO_BRANCH"
  cd "$SRC_DIR"
  sudo -u "$SERVICE_USER" git fetch --quiet origin
  sudo -u "$SERVICE_USER" git checkout --quiet "$REPO_BRANCH"
  sudo -u "$SERVICE_USER" git reset --hard --quiet "origin/$REPO_BRANCH"
  CLEANUP=0
fi

echo "==> Updating backend"
cp "$SRC_DIR/app/backend/stowtrace_backend.py" "$INSTALL_DIR/"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/stowtrace_backend.py"

echo "==> Updating frontend"
cp "$SRC_DIR/app/frontend/index.html"           "$WEB_DIR/index.html"
cp "$SRC_DIR/app/frontend/forge/index.html"     "$WEB_DIR/forge/index.html"
cp "$SRC_DIR/app/frontend/inventory/index.html" "$WEB_DIR/inventory/index.html"
if [ -f "$SRC_DIR/app/frontend/wifi/index.html" ]; then
  mkdir -p "$WEB_DIR/wifi"
  cp "$SRC_DIR/app/frontend/wifi/index.html"    "$WEB_DIR/wifi/index.html"
fi
chown -R www-data:www-data "$WEB_DIR"

echo "==> Refreshing nginx config"
cp "$SRC_DIR/deploy/etc/st-nginx.conf" /etc/nginx/sites-available/stowtrace
nginx -t >/dev/null 2>&1 && systemctl reload nginx

echo "==> Refreshing systemd service"
sed -e "s/^User=.*/User=$SERVICE_USER/" \
    -e "s/^Group=.*/Group=$SERVICE_USER/" \
    "$SRC_DIR/deploy/etc/st-backend.service" \
    > /etc/systemd/system/st-backend.service
systemctl daemon-reload

echo "==> Refreshing update.sh itself"
cp "$SRC_DIR/deploy/update.sh" "$INSTALL_DIR/update.sh"
chmod +x "$INSTALL_DIR/update.sh"

echo "==> Refreshing WiFi sudoers"
if [ -f "$SRC_DIR/deploy/etc/st-wifi-sudoers.template" ]; then
  awk -v u="$SERVICE_USER" '{ gsub(/%s/, u); print }' \
    "$SRC_DIR/deploy/etc/st-wifi-sudoers.template" \
    > /etc/sudoers.d/stowtrace-wifi
  chmod 0440 /etc/sudoers.d/stowtrace-wifi
  if ! visudo -c -q -f /etc/sudoers.d/stowtrace-wifi 2>/dev/null; then
    rm -f /etc/sudoers.d/stowtrace-wifi
  fi
fi

echo "==> Refreshing update-runner sudoers"
cat > /etc/sudoers.d/stowtrace-update <<EOF
$SERVICE_USER ALL=(ALL) NOPASSWD: /bin/bash $INSTALL_DIR/update.sh
$SERVICE_USER ALL=(ALL) NOPASSWD: $INSTALL_DIR/update.sh
$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemd-run --unit=st-update-runner --collect --no-block /bin/bash $INSTALL_DIR/update.sh
EOF
chmod 0440 /etc/sudoers.d/stowtrace-update
if ! visudo -c -q -f /etc/sudoers.d/stowtrace-update 2>/dev/null; then
  rm -f /etc/sudoers.d/stowtrace-update
fi

echo "==> Refreshing reboot sudoers"
cat > /etc/sudoers.d/stowtrace-reboot <<EOF
$SERVICE_USER ALL=(ALL) NOPASSWD: /sbin/reboot
$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemd-run --unit=st-reboot-runner --collect --no-block --on-active=5 /sbin/reboot
EOF
chmod 0440 /etc/sudoers.d/stowtrace-reboot
if ! visudo -c -q -f /etc/sudoers.d/stowtrace-reboot 2>/dev/null; then
  rm -f /etc/sudoers.d/stowtrace-reboot
fi

echo "==> Clearing update cache"
# The cached update-check state is now stale; remove it so the next
# check returns fresh data.
rm -f /var/lib/stowtrace/update-cache.json || true

echo "==> Restarting service"
systemctl restart st-backend
sleep 2

if [ "$CLEANUP" = "1" ]; then
  rm -rf "$SRC_DIR"
fi

if systemctl is-active --quiet st-backend; then
  echo
  echo "âœ“ Update complete. Service is running."
else
  echo
  echo "! Service didn't come back up. Check:"
  echo "  sudo journalctl -u st-backend -n 30 --no-pager"
  exit 1
fi
