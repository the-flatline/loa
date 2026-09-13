#!/usr/bin/env bash
# Build the Rust ring on the BUILD HOST and install it on the body.
#
#   scripts/deploy-ring.sh            build + install + restart
#   scripts/deploy-ring.sh --build    build only
set -euo pipefail

BODY="${LOA_BODY:-loa}"
TARGET=aarch64-unknown-linux-gnu
BIN="rust/target/${TARGET}/release/loa-ring"

echo "== building loa-ring for ${TARGET} =="
(cd rust && cargo build --release --target "${TARGET}" -p loa-ring)

if [ "${1:-}" = "--build" ]; then
    echo "built: ${BIN}"
    exit 0
fi

# The wire format fails silently on the strip, so the encoder is checked
# against the Python one before anything is installed.
echo "== the WS2812 wire format (against the Python encoder) =="
(cd rust && cargo test --release -p loa-ring 2>&1 | grep -E "^test result")

echo "== installing on ${BODY} =="
scp -q "${BIN}" "${BODY}:/tmp/loa-ring.new"
scp -q deploy/loa-ring.service "${BODY}:/tmp/loa-ring.service"
ssh "${BODY}" '
set -e
sudo -n install -m 0755 /tmp/loa-ring.new /usr/local/bin/loa-ring
sudo -n cp /tmp/loa-ring.service /etc/systemd/system/loa-ring.service
sudo -n systemctl daemon-reload
sudo -n systemctl restart loa-ring
sleep 3
systemctl is-active loa-ring
'

echo "== its own counters, 8s =="
sleep 8
ssh "${BODY}" 'sudo -n journalctl -u loa-ring -n 3 --no-pager | tail -2'
