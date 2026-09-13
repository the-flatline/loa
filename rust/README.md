# The Rust layer: the loa, in a language that does not need an interpreter to
# own a hardware bus.
#
# WHY THIS EXISTS. Measured 2026-09-13, on the body and on dixie:
#
#   * the panel write path was 49.0 fps because DC was toggled by a spawned
#     `pinctrl` process, 16 per frame at 1.40 ms each. Holding the line open
#     took the same path to 371.3 fps against a 472.2 fps wire.
#   * the console could not exceed ~30 fps, and Textual was the wall: 21.6 ms
#     to redraw a FIVE-CHARACTER frame, because every update runs the widget
#     tree, a layout pass and a compositor over the whole screen. Python's own
#     share of a frame was 0.3 ms.
#
# So this is not "Python is slow" — it is not slow. It is that the two pieces
# that must hold a deadline were built on a TUI framework and on fork(). Rust
# here buys a static binary per daemon (no venv, no interpreter at boot, nothing
# to drift), a framework that repaints in 0.47 ms instead of 21.6, and one
# language for the two things that own hardware or a frame clock.
#
# THE PYTHON STAYS, for now, and deliberately: the renderers (face, ring, amiga)
# and the cortex's state remain the reference implementation. A second renderer
# is how the glass and the feed come to disagree about what the body looks like,
# and that bug has already been paid for twice. When a renderer moves here, it
# moves with a frame-diff test against the Python one — byte-for-byte, 1024 of
# them — not on the strength of looking right.

## Layout

    Cargo.toml          the workspace
    .cargo/config.toml  the aarch64 linker: build on dixie, install to loa
    loa-topic/          the wire — protobuf bindings + subscriber (SHARED)
    loa-panel/          the OLED daemon: subscribes, blits, no forks
    ripperdoc/          the bench console, Ratatui

## The contract, in one place

The wire is `loa/topic.py`'s, unchanged: every message is two ZMQ frames,
`[topic][Envelope]`, protobuf generated from `proto/loa.proto` by `build.rs` in
`loa-topic`. There is one schema for both languages. `SCHEMA_VERSION` is
duplicated in `loa-topic/src/lib.rs` on purpose — a consumer cannot check a
message's version against itself.

Commands still travel the other way, over HTTP `POST /api`: the topic is for
data, and the door is for verbs. Nothing here reads `/state`.

## Building

    cd rust
    cargo build --release                            # for dixie (x86_64)

For the body, BUILD ON THE BODY. It is a 2.4GHz quad core with 8.8GB free and
does not need to be treated as fragile; the toolchain lives there and the binary
is native.

    # once, on the body
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
    sudo apt-get install -y cmake build-essential
    # then
    scripts/deploy-panel.sh && scripts/deploy-ring.sh

Cross-compiling from dixie also works (`--target aarch64-unknown-linux-gnu`,
needs `protoc`, `gcc-aarch64-linux-gnu`, the rustup target, and cmake for the
vendored libzmq). It is the slower path, not the better one.

`zmq-sys` builds libzmq from source when the box has no system copy, so the
result is one static binary: the body runs no package it did not already have.

## What is still Python, and the order to change that

The boundary is HALF-DRAWN today, and that is a cost, not a resting state. It
already cost once: the publisher (pyzmq/libzmq) and a subscriber (a pure-Rust
ZMTP crate) disagreed about frame reassembly, so a ~1KB face frame counted three
frames instead of two a couple of times an hour. Python on the body now:

  * the cortex — state, the publisher, the HTTP door, the render loop
  * the renderers — face, ring, amiga, frames
  * motion / sonar / weather — the sensor daemons
  * the fault sweep, and the vault

Order, by how much each buys against how much it can break:

  1. THE RENDERERS, with a byte-diff harness FIRST. They are the whole risk: port
     the face to Rust and get one pixel wrong and the glass and the console
     disagree about what the body looks like. Render the same state in both
     languages and compare the 1024 bytes in CI, and do not delete the Python
     renderer until they have agreed for a day. This is the port that REMOVES a
     second implementation rather than adding one.
  2. THE SENSOR DAEMONS (motion, sonar, weather, fault). Small, isolated, one job
     each, each owning its own bus. Cheap, low risk, and it takes the venv off
     the body's critical path.
  3. THE CORTEX (state, store, publisher loop). Follows the renderers.
  4. THE HTTP DOOR and the vault LAST, or never. The vault's write path has a
     wipe arm: its correctness matters more than its speed, and it is not on the
     frame path at all.

The rule this is all in service of: ONE implementation of anything two programs
must agree about. Two renderers may be a port in progress; two sets of framing
rules are a bug waiting for a quiet hour.


## Checking it

    ./target/release/loa-feed tcp://192.168.1.200:5556 4
