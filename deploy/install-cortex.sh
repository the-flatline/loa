#!/usr/bin/env bash
# Install the cortex on the Pi. Run ON the Pi as user flatline, from a
# checkout of the-flatline/loa-ring.
#
# Replaces the old flag-file stack (loa-ctl + v0.2 presence) with the
# cortex: loa-presence + loa-oled + loa-api, all driven by cortex.db.
set -euo pipefail

echo "== stopping the old door (loa-ctl) =="
sudo systemctl stop loa-ctl 2>/dev/null || true
sudo systemctl disable loa-ctl 2>/dev/null || true

echo "== clearing stale flag files =="
rm -f /tmp/loa_alarm /tmp/loa_busy /tmp/loa_scan /tmp/loa_glitch

echo "== installing loa-ring[api] into the Pi venv =="
PY=/home/flatline/venv/bin/python
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" "git+https://github.com/the-flatline/loa-ring.git@main[api]"
else
  "$PY" -m pip install "git+https://github.com/the-flatline/loa-ring.git@main[api]"
fi

echo "== installing services =="
sudo cp deploy/loa-presence.service deploy/loa-oled.service deploy/loa-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now loa-presence loa-oled loa-api

echo "== done. sanity check: =="
curl -sf http://127.0.0.1:8765/health && echo
curl -sf http://127.0.0.1:8765/state | head -c 300 && echo