#!/usr/bin/env bash
#
# update.sh — one-shot updater for the OLED dashboard on the Raspberry Pi.
#
# Run it on the Pi, or from the Mac with:  ssh rpi '~/oled-stats/update.sh'
#
# It does the whole deploy in one go:
#   1. git pull the latest app code from this repo,
#   2. sync the venv dependencies (psutil, …) from requirements.txt,
#   3. reinstall the `oleddisplay` library ONLY if its upstream commit moved
#      (pass --lib to force a reinstall regardless),
#   4. reinstall the systemd unit if it changed, and daemon-reload,
#   5. restart the service and print the last log lines.
#
set -euo pipefail

# --- Config (read once, up top) ---------------------------------------------
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="oled-stats"
UNIT_DST="/etc/systemd/system/oled-stats.service"
LIB_REPO="https://github.com/Jacksotnik/rpi5-sh1107-oled-128x128.git"
PIP="$APP_DIR/venv/bin/pip"
PY="$APP_DIR/venv/bin/python"

# --- Args -------------------------------------------------------------------
force_lib=0
case "${1:-}" in
  --lib) force_lib=1 ;;
  "")    force_lib=0 ;;
  *)     echo "usage: $0 [--lib]   (--lib forces a display-library reinstall)" >&2; exit 2 ;;
esac

# --- Guard: the venv must exist ---------------------------------------------
if [[ ! -x "$PIP" ]]; then
  echo "error: venv not found at $APP_DIR/venv — run the fresh-install steps from the README first." >&2
  exit 1
fi

cd "$APP_DIR"

# 1. App code ----------------------------------------------------------------
echo "==> Pulling app repo…"
git pull --ff-only

# 2. Python deps (psutil and anything else in requirements.txt) --------------
echo "==> Syncing venv dependencies…"
"$PIP" install -q -r requirements.txt

# 3. Display library — reinstall only when its upstream commit changed -------
echo "==> Checking display library…"
remote_head="$(git ls-remote "$LIB_REPO" HEAD 2>/dev/null | awk '{print $1}' || true)"

installed_commit=""
direct_url_file="$(find "$APP_DIR/venv" -name direct_url.json -path '*sh1107*' -print -quit 2>/dev/null || true)"
if [[ -n "$direct_url_file" ]]; then
  installed_commit="$("$PY" -c 'import sys, json; print(json.load(open(sys.argv[1])).get("vcs_info", {}).get("commit_id", ""))' "$direct_url_file" 2>/dev/null || true)"
fi

if [[ -z "$remote_head" ]]; then
  echo "    could not reach the library repo — skipping the library check."
elif [[ "$force_lib" == 1 || -z "$installed_commit" || "$installed_commit" != "$remote_head" ]]; then
  from="${installed_commit:0:7}"
  [[ -z "$from" ]] && from="none"
  echo "    updating library: $from -> ${remote_head:0:7}"
  "$PIP" install --upgrade --force-reinstall --no-deps --no-cache-dir "git+$LIB_REPO"
else
  echo "    library already current (${installed_commit:0:7})."
fi

# 4. systemd unit — reinstall only if it changed -----------------------------
echo "==> Checking systemd unit…"
if [[ ! -f "$UNIT_DST" ]] || ! cmp -s "$APP_DIR/oled-stats.service" "$UNIT_DST"; then
  echo "    unit changed — installing and reloading."
  sudo install -m 644 "$APP_DIR/oled-stats.service" "$UNIT_DST"
  sudo systemctl daemon-reload
else
  echo "    unit unchanged."
fi

# 5. Restart and show the tail of the log ------------------------------------
echo "==> Restarting $SERVICE…"
sudo systemctl restart "$SERVICE"
sleep 1
if systemctl is-active --quiet "$SERVICE"; then
  echo "    $SERVICE is active."
else
  echo "    WARNING: $SERVICE is not active — see the log below." >&2
fi

echo "==> Recent logs:"
journalctl -u "$SERVICE" -n 15 --no-pager
echo "==> Done."
