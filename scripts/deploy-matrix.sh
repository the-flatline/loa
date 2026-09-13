#!/usr/bin/env bash
# Build loa-matrix on the BUILD HOST and install it on the body.
#
#   scripts/deploy-matrix.sh            build + install binary + unit, restart
#   scripts/deploy-matrix.sh --build    build only
#   scripts/deploy-matrix.sh --no-service   binary only (bench poking)
#
# The panel is the body's ALERT LIGHT, so the unit goes on with it: a light
# that only works when someone ssh's in is not an alert light.
set -euo pipefail

BODY="${LOA_BODY:-loa}"
TARGET=aarch64-unknown-linux-gnu
BIN="rust/target/${TARGET}/release/loa-matrix"

echo "== building loa-matrix for ${TARGET} =="
(cd rust && cargo build --release --target "${TARGET}" -p loa-matrix)

if [ "${1:-}" = "--build" ]; then
    echo "built: ${BIN}"
    exit 0
fi

echo "== installing on ${BODY} =="
scp -q "${BIN}" "${BODY}:/tmp/loa-matrix.new"
ssh "${BODY}" 'sudo -n install -m 0755 /tmp/loa-matrix.new /usr/local/bin/loa-matrix && rm -f /tmp/loa-matrix.new && /usr/local/bin/loa-matrix --help >/dev/null && echo installed: $(ls -l /usr/local/bin/loa-matrix)'

if [ "${1:-}" = "--no-service" ]; then
    exit 0
fi

echo "== unit + restart =="
scp -q deploy/loa-matrix.service "${BODY}:/tmp/loa-matrix.service"
ssh "${BODY}" '
set -e
sudo -n cp /tmp/loa-matrix.service /etc/systemd/system/loa-matrix.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now loa-matrix.service >/dev/null 2>&1 || true
sudo -n systemctl restart loa-matrix.service
sleep 1
systemctl status loa-matrix.service --no-pager | head -6
'
