"""panel — the face's PANEL. The device, not the picture.

The SH1106 driver: SPI0, the init sequence, the two commands that turn the
glass 180 degrees, and the contrast register. NullDisplay is the same
interface with no hardware, so the bench and the tests exercise the identical
blit path.

Split out of `face.py` (2026-09-13) so the DISPLAY daemon's import graph is
exactly {this driver, the frame geometry, the topic}: `loa-oled` imports
panel.py and physically cannot reach the renderer (`face.py`, `frames.py`) or
the state derivation that lives beside it (`seal_state`, `faults_state`,
`power_status`). While both lived in one file the boundary was a convention —
the display could have imported the state-deriving code tomorrow, worked fine,
and nobody would have noticed. Enforced structurally by
tests/test_display_boundary.py, not by good intentions.

Nothing here DERIVES anything or reads anything. The panel is write-only: it
cannot be read back, so the orientation it was last told is remembered in the
object, exactly as a real panel remembers a register.

Geometry (WIDTH/HEIGHT/PAGES) is re-exported from `geom` — the driver and the
renderer share one definition of the frame, never two.
"""
import time

from . import geom

WIDTH = geom.FACE_WIDTH
HEIGHT = geom.FACE_HEIGHT
PAGES = geom.FACE_PAGES


class NullDisplay:
    """No hardware: frames go nowhere. Used off-Pi (dixie, tests)."""

    def show(self, buf, offset=2):
        pass

    def clear(self):
        pass

    def set_contrast(self, val):
        pass

    def set_flip(self, flipped):
        """No panel here, so the orientation is just remembered — but it IS
        remembered, so the bench can check the setting reaches the display."""
        self._flip = bool(flipped)

    def close(self):
        pass


#: How THIS panel is mounted. Measured, not guessed: with all four combinations
#: of segment remap and COM scan driven on the real glass (2026-09-13), A0/C0 is
#: the one that reads upright and not mirrored. So no compensation is needed —
#: the mount matches how the renderers draw — and `oled_flip = false` is the
#: setting that says so.
DEFAULT_FLIP = False


class SH1106:
    """SH1106 1.3" 128x64 over SPI0. Proven init sequence from oled_canon.

    bus/device default from loa.conf (oled_bus / oled_device), else SPI0 CE0
    (CLK 11 / MOSI 10 / CS 8). DC and RES are plain GPIOs via pinctrl.
    """

    def __init__(self, bus=None, device=None, speed=None, dc=25, res=24,
                 offset=2):
        import subprocess
        import spidev
        from . import config
        cfg = config.load()
        self.dc = dc
        self.res = res
        self.offset = int(cfg.get("oled_offset", offset))
        self.spi = spidev.SpiDev()
        self.spi.open(
            int(cfg.get("oled_bus", bus if bus is not None else 0)),
            int(cfg.get("oled_device", device if device is not None else 0)),
        )
        self.spi.max_speed_hz = int(cfg.get("oled_speed", speed if speed is not None else 500_000))
        self.spi.mode = 0b00
        self._gpio = subprocess.run
        self._init()

    def _pin(self, pin, val):
        self._gpio(["pinctrl", "set", str(pin), "op", "dh" if val else "dl"],
                   capture_output=True)

    def _cmd(self, *b):
        self._pin(self.dc, 0)
        self.spi.xfer2(list(b))

    def _data(self, *b):
        self._pin(self.dc, 1)
        self.spi.xfer2(list(b))

    def _init(self):
        self._pin(self.res, 0)
        time.sleep(0.05)
        self._pin(self.res, 1)
        time.sleep(0.05)
        for c in (0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40,
                  0x8D, 0x14, 0x20, 0x00, 0xDA, 0x12,
                  0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6,
                  0x2E, 0xAF):
            self._cmd(c)
        # The orientation is NOT part of the init anymore: it is a setting,
        # applied through set_flip() so one place decides it. It is applied here
        # with DEFAULT_FLIP so the panel is correct from power-on rather than
        # from whenever the feed happens to arrive — measured on the body
        # 2026-09-13: powering up at A1/C8 and correcting on the first message
        # meant the glass was wrong for as long as the feed took to start.
        self._flip = None
        self.set_flip(DEFAULT_FLIP)

    def show(self, buf, offset=None):
        off = self.offset if offset is None else offset
        for page in range(PAGES):
            self._cmd(0xB0 | page, off & 0x0F, 0x10 | (off >> 4))
            self._data(*buf[page * WIDTH:(page + 1) * WIDTH])

    def set_contrast(self, val):
        self._cmd(0x81, max(0, min(255, int(val))))

    #: The panel's two orientations. 0xA1 (segment remap) + 0xC8 (COM scan
    #: reversed) is how the face is wired today; 0xA0 + 0xC0 is the same face
    #: turned 180 degrees.
    FLIP_ON = (0xA1, 0xC8)
    FLIP_OFF = (0xA0, 0xC0)

    def set_flip(self, flipped):
        """Turn the face 180 degrees — on the PANEL, not in software.

        The SH1106 does it in two commands, so a rotated face costs nothing per
        frame and draws the identical picture; doing it in the renderer would
        cost CPU on every frame including the wash, on a board that has already
        been to its thermal limit for less.

        The panel cannot be read back, so the state is remembered here. Called
        at startup with the setting, so the glass always matches the setting
        rather than whatever the last process left behind.
        """
        flipped = bool(flipped)
        if flipped == getattr(self, "_flip", None):
            return
        for c in (self.FLIP_ON if flipped else self.FLIP_OFF):
            self._cmd(c)
        self._flip = flipped

    def clear(self):
        self.show(bytearray(PAGES * WIDTH))

    def close(self):
        self.spi.close()


def get_display():
    """SH1106 when the hardware is here, NullDisplay anywhere else."""
    try:
        import spidev  # noqa: F401
        return SH1106()
    except Exception:
        return NullDisplay()
