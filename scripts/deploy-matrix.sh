#!/usr/bin/env bash
# Build loa-matrix on the BUILD HOST and install it on the body.
#
#   scripts/deploy-matrix.sh            build + install
#   scripts/deploy-matrix.sh --build    build only
#
# Standalone for now: no unit, no feed. It is installed so the bench can run
# it over ssh while the panel is on fly leads. When the cortex starts
# publishing a matrix topic this becomes a daemon like loa-ring.
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
