# loa-ring

The voice of **loa** — a WS2812B ring driver + animation library for the
Raspberry Pi 5 outpost. Theme: **PHOSPHOR**.

## Hardware

- Raspberry Pi 5
- 24-LED WS2812B ring
- Pinout: RED → pin 2 (5V), BLACK → pin 6 (GND), BLUE → pin 19 (GPIO10 / SPI0 MOSI)

Driven over **SPI** (3.2MHz, four SPI bits per WS2812 bit) because the PyPI
`rpi_ws281x` wheel has no RP1/Pi-5 binary and the Adafruit path needs Blinka.

## Install

```bash
pip install spidev
pip install git+https://github.com/the-flatline/loa-ring.git
```

## Use

```python
from loa_ring import Ring, animations

ring = Ring(num=24)
ring.fill((0, 255, 0))        # solid green
ring.show(animations.scan_frames()[10])  # one frame of the comet
ring.close()
```

## The voice — three states

| State | Trigger | Look |
|---|---|---|
| **Home** | default | green breath, cyan drift, scan comet, rare glitch |
| **Busy** | `touch /tmp/loa_busy` | amber breath — working |
| **Alarm** | `touch /tmp/loa_alarm` | full red triple pulse — the yell |

Priority: alarm > busy > home.

## Daemon

```bash
loa-presence
```

or as a systemd service (see `deploy/loa-presence.service`).

## Architecture

- `loa_ring/ring.py` — hardware layer. `Ring` wraps SPI.
- `loa_ring/animations.py` — pure math, no hardware. Every function returns
  a list of frames (each frame = `LED_COUNT` (r,g,b) tuples), so the
  animations can be rendered, simulated, or tested anywhere.
- `loa_ring/presence.py` — daemon entry point wiring the two together.

The animation layer deliberately has **no hardware dependency**: you can
simulate the ring, print frames, or unit-test them without a Pi.

## License

MIT.