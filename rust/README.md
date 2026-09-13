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
    cargo build --release --target aarch64-unknown-linux-gnu   # for the body

Cross-compiling needs `protoc`, `gcc-aarch64-linux-gnu` and the rustup target;
all three are installed on dixie. The body needs nothing: no libzmq (the ZMQ
client is pure Rust), no Python, no toolchain.

## Checking it

    ./target/release/loa-feed tcp://192.168.1.200:5556 4
