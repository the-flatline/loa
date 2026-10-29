# PLAN

What's left, in the order it should be done, and why that order. Written 2026-09-14
at the point where the rename was half-done. Everything below is measured against the
tree as it stands right now, not against what I remember.

---

## The design

Settled. Do not re-open it.

- One module for the cortex. One module for each sensor/device.
- Each device renders its **own** pixels: `loa-oled` paints the face, `loa-ring` paints
  the ring. Neither has any business knowing the other exists.
- **State goes down, frames go up.** The cortex publishes state; the device receives it
  (SUB socket), renders, and pushes the finished frame back up as fast as it can.
- `loa-cortex` is the only thing that talks to the topics. Downstream — ripperdoc, the
  website via the bridge — reads the frames from there. One source.
- Requests come in via ripperdoc.
- Shared things live in `common/`: geometry, state, the condition vocabulary, fonts.
  No pixels in `common/`.

Names: `loa-ring loa-motion loa-sonar loa-weather loa-oled loa-cortex loa-fault`.
There is no `loa-panel`. That was mine and it was wrong.

---

## Where the tree actually is

Python still in the tree: **5,274 lines**. Rust: **3,888 lines**.

Already Rust: `loa-topic` (the wire) · `loa-oled` (the glass, was `loa-panel`) ·
`loa-ring` (the strip) · `ripperdoc` (the console) · `loa-motion` · `loa-sonar` ·
`loa-weather` (built, baro verified, **deliberately not wired**) · `loa-matrix` ·
plus `loa-gpio` and `loa-sense` underneath them.

Still Python, by size: `cortex/face.py` 826 · `cortex/__main__.py` 812 · `topic.py` 566 ·
`fault/__main__.py` 423 · `cortex/state.py` 391 · `vault/__init__.py` 237 ·
`cortex/store.py` 230 · `cortex/amiga.py` 228 · `cortex/ring.py` 196 · `weather/dht.py` 176 ·
`sense.py` 175 · `weather/baro.py` 154 · `cortex/frames.py` 152 · `ring/animations.py` 132 ·
`config.py` 97 · `cortex/moods.py` 73 · `cortex/topaz.py` 68 · `weather/__main__.py` 59 ·
`ring/encode.py` 52 · `cortex/expressions.py` 40 · `geom.py` 21.

---

## The work, in order

### 1. Finish the rename (half-done, uncommitted, unbuilt)

Repo side is done: crate `rust/loa-oled`, module `oled.rs`, type `Oled`, unit
`deploy/loa-oled.service`, `scripts/deploy-oled.sh`, the fault sweep's bus-owner check,
and the references in `rust/README.md`, `tests/`, `deploy/install-cortex.sh`.

DONE 2026-10-29: crate and binary are `loa-oled`, built on the body, installed to
`/usr/local/bin/loa-oled`, unit `loa-oled.service` enabled and active at 40
written/s. `loa-panel` is gone from the body entirely — unit file, symlink and
binary. The fault sweep now reads clean (`faults: 0`), which it could not before:
the September build of `loa-fault` still looked for `loa-panel`, so it reported
PANEL DOWN and called the running oled a BUS CLASH while the panel was fine. The
python package on the body had not been reinstalled since 2026-09-13.

Trap already caught, worth keeping caught: `deploy/install-cortex.sh` retires dead units
by *deleting them*, and `loa-oled` was on that list. Renaming the daemon to `loa-oled`
would have made that script delete the unit the new daemon installs. The list now names
`loa-panel`. Check any other script that iterates unit names before touching this again.

### 2. `common/`

Pull out what two modules must agree about, and nothing else:

- `common/geom.rs` — the two frame sizes (1024 face, 72 ring)
- `common/state.rs` — the state, from `cortex/state.py`
- `common/condition.rs` — alarm > hurts > mute > busy > home, and the ring-dark takeover,
  from `cortex/ring.py`
- `common/fonts/` — `amiga.rs`, `topaz.rs`, and the two fonts currently buried inside
  `face.py` (5x7 and 3x5) which are fonts and should not be in the face

Done when `loa-oled` and `loa-ring` can both read the state and the condition, and neither
one has an import that reaches into the other.

### 3. `loa-oled` takes the face render

Move `face.py` and `frames.py` into the daemon: subscribe to state, render, blit, push the
frame up. `loa-oled` becomes 826 + 152 lines of Python's job in Rust.

This is the one that can hurt quietly: a wrong pixel in a 1024-byte bitmap still looks
like a face, and the Python that drew it correctly is deleted in the same move. The glass
is the check — Divv looks at it every day and will see it.

Render the wash (full-frame exercise every 5 minutes) and the seeded-per-frame noise mode
first. Between them they drive every pixel on the panel, so they are the fastest way to
see a whole page, or a whole font row, come out wrong. Noise is seeded per frame from a
fixed number, which means it is reproducible if it needs to be looked at twice.

### 4. `loa-ring` takes the ring render

`cortex/ring.py` (the tell and the condition vocabulary — the vocabulary itself moves to
`common/`), `ring/animations.py` (comet, breath, the glitch), `ring/encode.py` (the code-space
dither). The WS2812 timing encoding is already Rust and already pinned by
`rust/loa-ring/tests/wire_format.rs` — that test stays.

### 5. The cortex

State, store, publisher loop, and the HTTP door. It stops rendering; it receives frames
from the two devices and puts them on the topics. This is the biggest single piece
(812 + 391 + 230 lines of Python), and it should follow the devices because until they
render, there is nothing for it to publish.

### 6. Weather, the DHT half

`loa-weather` is built and not wired. Wiring it with the DHT half missing would silence
`temp_c` and `hum_pct` on the feed.

The risk is the timestamps. The Python driver took them from libgpiod `Edge.BOTH` events,
which are immune to interpreter jitter; a Rust poll loop is not, by default. Plan is fixed:
tight loop on `Instant::now()` with the frame checksum as referee. If frames validate
repeatedly, the timestamps were good enough. If they don't, go to libgpiod properly —
do not tune the loop.

### 7. The fault sweep

423 lines. Its real coupling is the daemon roster it asserts against, which keeps changing
under it — which is exactly why it produced false OLED DOWN + BUS CLASH verdicts after the
rename. Port it after the devices settle so it is asserting against a roster that has
stopped moving.

### 8. The vault, and the door it hangs off

Last. 237 lines plus the HTTP door. Its write path has a wipe arm: a wrong token destroys
the journal and there is no restore.

The seal derivation must match byte for byte — HMAC, with the literal domain separator
`b"fragment"`. Rename that string and every derived token changes and the tripwire fires.
Compare derived tokens against the Python function over throwaway inputs, token for token,
until they are identical. Never a live request before that.

### 9. Delete the scaffolding

`topic.py`, `sense.py`, `config.py`, `geom.py` don't get ported — they get deleted when
their last Python consumer goes. Then: `pyproject.toml`, the venv, the installed wheel on
the body, and the console scripts.

Done means: no `.py` in the tree, no venv on the body, `import loa` fails everywhere, and
nothing in `/usr/local/bin` or `/etc/systemd/system` starts an interpreter.

---

## Independent of all of the above

- **SPI is still at 4 MHz.** One number, halves blit time. Nothing else in this plan buys
  a frame as cheaply.
- **`loa-relay` is not a git repository.** Loose file plus a unit, and the website depends
  on it. No history, no backup.
- **`rust/loa-render/`** is untracked. A crate shape I invented, and it is wrong — delete it.
  Same for the generator script that sat in `scripts/` and the directory it wrote to. Both
  are already gone from the working tree.

---

## What went wrong, briefly

Not for the record of the work — for whoever reads this next.

The port kept stalling because I answered architecture questions with timing numbers, and
because I carried forward what was in the code instead of what Divv had decided. Two
concrete instances: `loa-panel` (invented 25 minutes before it was built, never asked) and
a render crate split invented three times in one evening. The repository had the right names
and the right boundaries written down. Reading the code is not the same as reading the
decision, and where the two disagree, the decision wins.

The five things to keep: one module per device · shared things in `common/` · state down,
frames up · cortex owns the topics · simplest unix tool for the job.
