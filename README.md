# loa — the loa control stack

The **cortex** of the loa outpost (a Raspberry Pi 5): ring driver, face
(OLED) driver, a feelings vocabulary, expressions, and an HTTP controller
the brain (dixie) talks to. Theme: **PHOSPHOR**.

## Hardware (as built 2026-09-07)

- Raspberry Pi 5
- 24-LED WS2812B ring on **SPI1, MOSI = P38/GPIO20** (`/dev/spidev1.0`) —
  verified by pinctrl; this Pi's spi1-1cs overlay has MOSI on P38, OPPOSITE
  of the old notes. Never wire a data-out device to P38's MISO.
- SH1106 1.3" OLED (the face) on **SPI0 CE0** (CLK 11 / MOSI 10 / RES 24 /
  DC 25 / CS 8), 7-pin SPI mode, 500kHz.

Driven over SPI because the PyPI `rpi_ws281x` wheel has no RP1/Pi-5 binary
and the Adafruit path needs Blinka. SPI is Pi-5-safe and dependency-light.

`loa.conf` (~) holds the as-built bus assignments (`ring_bus`, `ring_device`,
`ring_speed`, `oled_bus`, `oled_device`, `oled_offset`, `cortex_db`); env
vars (`LOA_RING_BUS`, …) override. The library reads it at open time, so the
repo stays aligned with the bench.

## Architecture

Three processes, one nervous system:

```
brain (dixie) ──HTTP──> loa-cortex (FastAPI, :8765) ──> cortex.db (SQLite)
                                        ▲                  │
                        ring daemon (loa-ring) ───────────┘  poll every frame
                        face daemon (loa-oled)    ──────────┘  poll every frame
```

- **cortex.db** — `~/.loa/cortex.db`. One `state` row (what the ring should
  do, what the face should show, current mood/expression) + an append-only
  `events` log (every mood, expression, ring command, daemon boot). The
  daemons poll state each frame; the API writes it. History is the memory.
- **loa-ring** — owns the ring (SPI1). Sustained states
  (home/busy/alarm) + one-shot events (scan/glitch), priority
  alarm > busy > event > home.
- **loa-motion / loa-sonar / loa-weather** — own the inputs (GPIO + i2c), one
  process per sense so a hung sensor cannot deafen the others. v1: PIR on GPIO17, debounced
  rising edge, cooldown; motion fires a scan event + logs `sense` history.
- **loa-oled** — owns the face (SPI0). Modes: `scope` (the flatline —
  default), `ecg`, `ripple`, `noise`, `text` (marquee), `ripperdoc`,
  `off`. `dim` drops
  panel contrast (asleep). `ripperdoc` is the bench board: [PIR] outline
  box that goes solid while the pin is high, plus trigger count / age — for
  tuning the senses.
- **loa-cortex** — the door. Pure intent, no hardware: runs anywhere.

## API

The surface is **ONE route**: `POST /api`. Commands go IN over HTTP; data
comes OUT on the ZeroMQ topics — there is no HTTP read path, by design. There
is no liveness route either: liveness is the unit and the feed, message
arrival. Every verb returns the body its old route returned, so callers do not
change shape.

| Verb | Args | Effect |
|---|---|---|
| `feel` | `{"feeling": "calm"}` | set a mood (ring + face) |
| `express` | `{"expression": "happy"}` or `{"expression":"custom","text":"..."}` | face says something |
| `ring` | `{"state": "scan"}` | direct ring: home/busy/alarm, scan/glitch events |
| `display` | `{"mode": "ecg", "dim": true}` | direct face control |
| `ripperdoc` | `{"on": true}` / `{"page": "pir"}` | bench mode: face becomes live sense status board |
| `vault.health` | — | public seal state of the vault (no token) |
| `vault.append` | `{"entry": "..."}` | sealed write (`X-Vault-Token`) |
| `vault.read` | — | the raw thread, access log first (`X-Vault-Token`) |

Request body: `{"cmd": "<verb>", "args": {...}}`. An unknown verb is a `400`
that lists the valid verbs. The verb list is exactly what the two real clients
issue — ripperdoc on the body and the vault client on dixie. There is no
`ping` verb: a verb with no caller would be a door opened to "have it
available". `tests/test_api_surface.py` asserts the route table is exactly
`{"/api"}`, so the surface cannot grow a door at a time again.

### Feelings (moods)

| Feeling | Ring | Face |
|---|---|---|
| **calm** (default) | home breath | scope — flat line, occasional blip |
| **busy** | amber breath | ecg trace |
| **pleased** | one comet lap | ripple |
| **annoyed** | one glitch stutter | noise burst |
| **alarmed** | full red triple pulse | `!! FLATLINE !!` |
| **asleep** | home, dim | ripple, low contrast |

### Expressions

`neutral`, `happy`, `think`, `suspicious`, `yell`, `sleep`, or `custom`
with free text. Render on the OLED today; the 3.5" face driver keeps these
names when it lands.

## Install (Pi)

```bash
pip install "spidev>=3.5"
pip install "git+https://github.com/the-flatline/loa.git@main[api]"
```

Services (see `deploy/`): `loa-ring.service`, `loa-motion/sonar/weather.service`,
`loa-oled.service`, `loa-cortex.service`. The cortex replaces the old flag-file
door. `loa-cortex` binds 0.0.0.0:8765; ice's firewall is
the gate (dixie -> loa:8765 only), DNS-first via `loa.zendient.com`.

## Use (library)

```python
from loa import Ring, animations

ring = Ring(num=24)              # bus from loa.conf (as built: SPI1)
ring.fill((0, 255, 0))           # tuple, "#00FF00", or 0x00FF00
ring.show(animations.scan_frames()[10])
ring.close()
```

Animations are pure math (no hardware) — render, simulate, or test anywhere.

## License

MIT.