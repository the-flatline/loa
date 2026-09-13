#!/bin/sh
# One-shot smoke check: is the feed answering, and what is on it?
#
# This used to build the PYTHON console's Feed. The console is Rust now, so this
# runs `loa-feed` — the same wire layer the panel, the ring and the console use.
# That is the point of it: one implementation of the feed for every consumer,
# which is what the two-frame/two-languages bug of 2026-09-14 was about.
#
#   scripts/feed_smoke.sh                     the body's feed, 4 seconds
#   scripts/feed_smoke.sh tcp://host:5556 10  another endpoint, longer
set -eu
EP="${1:-tcp://192.168.1.200:5556}"
SECS="${2:-4}"
BIN="$(dirname "$0")/../rust/target/release/loa-feed"
if [ ! -x "$BIN" ]; then
    echo "no loa-feed binary — build it:  cd rust && cargo build --release" >&2
    exit 1
fi
exec "$BIN" "$EP" "$SECS"
