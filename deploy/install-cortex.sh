#!/usr/bin/env bash
# Install the cortex on the Pi. Run ON the Pi as user flatline, from a
# checkout of the-flatline/loa.
#
# Replaces the old flag-file stack (loa-ctl + v0.2 presence) with the cortex:
# loa-cortex at the centre, the ring/face daemons around it, and one daemon
# per sense so a failed sensor cannot deafen the others.
set -euo pipefail

echo "== retiring the old flag-file door =="
rm -f /tmp/loa_alarm /tmp/loa_busy /tmp/loa_scan /tmp/loa_glitch

echo "== installing loa[api] into the Pi venv =="
PY=/home/flatline/venv/bin/python
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" "git+https://github.com/the-flatline/loa.git@main[api]"
else
  # #egg= extras form: works on older pip (pre-21.3) that mis-parses @main[api]
  "$PY" -m pip install "git+https://github.com/the-flatline/loa.git@main#egg=loa[api]"
fi

echo "== installing services =="
sudo cp deploy/loa-ring.service deploy/loa-oled.service deploy/loa-cortex.service \
        deploy/loa-motion.service deploy/loa-sonar.service deploy/loa-weather.service \
        deploy/loa-fault.service deploy/loa-fault.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now loa-ring loa-oled loa-cortex \
                            loa-motion loa-sonar loa-weather \
                            loa-fault.timer

echo "== retiring units that no longer exist =="
# loa-record: the CORTEX writes records now (it is the only writer of the
# store). loa-relay: it polled and wrote feed.json — the bridge this design
# forbids. loa-sense: split into loa-motion / loa-sonar / loa-weather.
for dead in loa-presence loa-sense loa-api loa-faults loa-ctl loa-record loa-relay; do
  sudo systemctl disable --now "$dead" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/$dead.service"
done
sudo rm -f /etc/systemd/system/loa-faults.timer
sudo systemctl daemon-reload

echo "== done. sanity check: =="
curl -sf http://127.0.0.1:8765/health && echo
curl -sf http://127.0.0.1:8765/state | head -c 300 && echo
