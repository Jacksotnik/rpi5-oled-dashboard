#!/usr/bin/env bash
#
# install.sh — first-time setup on the Raspberry Pi. Run once, after cloning:
#   git clone https://github.com/Jacksotnik/rpi5-oled-dashboard.git ~/oled-stats
#   ~/oled-stats/install.sh
#
# Creates the venv, installs dependencies, installs and enables the systemd unit,
# then starts the service. Safe to re-run.
#
set -euo pipefail

# --- Config (read once, up top) ---------------------------------------------
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="oled-stats"
UNIT_SRC="$APP_DIR/oled-stats.service"
UNIT_DST="/etc/systemd/system/oled-stats.service"
PY="$APP_DIR/venv/bin/python"
PIP="$APP_DIR/venv/bin/pip"

cd "$APP_DIR"

# 1. venv --------------------------------------------------------------------
echo "==> Creating venv…"
if [[ -x "$PY" ]]; then
  echo "    venv already present — reusing it."
else
  python3 -m venv --system-site-packages venv
fi

# 2. Dependencies ------------------------------------------------------------
echo "==> Installing dependencies…"
"$PIP" install -q -r requirements.txt

# 3. systemd unit ------------------------------------------------------------
echo "==> Installing systemd unit…"
sudo install -m 644 "$UNIT_SRC" "$UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

# 4. Start and show the tail of the log --------------------------------------
echo "==> Starting $SERVICE…"
sudo systemctl restart "$SERVICE"
sleep 1
if systemctl is-active --quiet "$SERVICE"; then
  echo "    $SERVICE is active."
else
  echo "    WARNING: $SERVICE is not active — see the log below." >&2
fi

echo "==> Recent logs:"
journalctl -u "$SERVICE" -n 15 --no-pager
echo "==> Done. Later updates: ./update.sh"
