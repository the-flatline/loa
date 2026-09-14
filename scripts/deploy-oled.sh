#!/usr/bin/env bash
# Build the Rust panel on the BUILD HOST and install it on the body.
#
# Run from dixie, from the repo root. The Pi never needs a toolchain: this
# cross-compiles for aarch64-unknown-linux-gnu and ships one static binary.
#
#   scripts/deploy-oled.sh            build + install + restart
#   scripts/deploy-oled.sh --build    build only
set -euo pipefail

BODY="${LOA_BODY:-loa}"
TARGET=aarch64-unknown-linux-gnu
BIN="rust/target/${TARGET}/release/loa-oled"

echo "== building loa-oled for ${TARGET} =="
(cd rust && cargo build --release --target "${TARGET}" -p loa-oled)

if [ "${1:-}" = "--build" ]; then
    echo "built: ${BIN}"
    exit 0
fi

echo "== installing on ${BODY} =="
scp -q "${BIN}" "${BODY}:/tmp/loa-oled.new"
scp -q deploy/loa-oled.service "${BODY}:/tmp/loa-oled.service"
ssh "${BODY}" '
set -e
sudo -n install -m 0755 /tmp/loa-oled.new /usr/local/bin/loa-oled
sudo -n cp /tmp/loa-oled.service /etc/systemd/system/loa-oled.service
sudo -n systemctl daemon-reload
# Exactly one owner of SPI0. The Python face daemon stays installed as the
# rollback and must not be running beside this one.
sudo -n systemctl disable --now loa-oled 2>/dev/null || true
sudo -n systemctl enable --now loa-oled
sleep 3
systemctl is-active loa-oled
'

echo "== what it is doing (its own counters, 6s) =="
sleep 6
ssh "${BODY}" 'sudo -n journalctl -u loa-oled -n 3 --no-pager | tail -2'
