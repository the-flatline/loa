#!/bin/sh
# Build the Rust daemons ON THE BODY and install them.
#
# The Pi is a 2.4GHz quad core with 8.8GB free; it does not need to be protected
# from a compiler. Building here means the binary is native, there is no cross
# toolchain to keep in step, and no vendored C library has to be coaxed into a
# foreign target — which is exactly where the cross path fell over on libzmq.
#
#   scripts/deploy-rust.sh                    loa-panel loa-ring
#   scripts/deploy-rust.sh loa-panel          just one
#
# The source is COPIED, not cloned: the repo is private and the body has no key
# for it. target/ is excluded — the body keeps its own object cache, which is
# what makes the second build fast.
set -eu
BODY="${LOA_BODY:-loa}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/home/flatline/loa-rust
CRATES="${*:-loa-panel loa-ring}"

BINS=""
for c in $CRATES; do BINS="$BINS -p $c"; done

echo "== sources -> body =="
# proto/ COMES TOO: loa-topic's build.rs generates from ../../proto/loa.proto,
# so the schema has to land beside rust/ in the same layout or codegen fails on
# a box that has everything except the contract.
tar -C "$SRC" --exclude=target -czf - rust proto \
    | ssh "$BODY" "mkdir -p $DEST && tar -C $DEST -xzf -"

echo "== build on the body: $CRATES =="
# Fail LOUDLY. The first run of this piped cargo through `tail`, so a missing
# protoc scrolled past and the script went on to install a binary that had never
# been built — the install failed and the log looked like a build log. A build
# step whose failure is not the script's failure is not a build step.
if ! ssh "$BODY" "cd $DEST/rust && ~/.cargo/bin/cargo build --release $BINS" \
        > /tmp/deploy-rust-build.log 2>&1; then
    echo "BUILD FAILED on the body — tail of the log:" >&2
    tail -25 /tmp/deploy-rust-build.log >&2
    exit 1
fi
tail -3 /tmp/deploy-rust-build.log

echo "== install =="
for c in $CRATES; do
    case "$c" in
    loa-panel|loa-ring|loa-motion|loa-sonar|loa-weather|loa-fault)
        # The service names are unchanged on purpose: the sweep asserts that
        # loa-panel holds spidev0.0 and loa-ring holds spidev1.0, so a renamed
        # binary would make a healthy body report BUS CLASH against itself.
        ssh "$BODY" "sudo -n install -m 755 $DEST/rust/target/release/$c /usr/local/bin/$c"
        ssh "$BODY" "sudo -n systemctl restart $c"
        echo "  $c installed and restarted"
        ;;
    *)
        echo "  $c built (no service of that name; left in $DEST/rust/target/release)"
        ;;
    esac
done

echo "== what the daemons say now =="
sleep 6
ssh "$BODY" 'sudo -n journalctl -u loa-panel -u loa-ring -n 6 --no-pager | grep -E "written/s|feed error" | tail -4'
