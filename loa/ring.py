"""Ring — hardware layer. WS2812B over SPI0, Raspberry Pi 5 safe.

Why SPI and not rpi_ws281x: the PyPI rpi_ws281x 5.0.0 wheel ships no compiled
binary for the RP1 chipset ("Hardware revision is not supported"), and the
Adafruit path needs Blinka. SPI is the Pi-5-safe, dependency-light route:
GPIO10 (MOSI) at 3.2MHz, four SPI bits per WS2812 bit.
"""
ONE  = bytes([0b1110])   # WS2812 "1"  ~937ns high, 1.25us total
ZERO = bytes([0b1000])   # WS2812 "0"  ~937ns low,  1.25us total


def _ws_byte(v: int) -> bytes:
    out = bytearray()
    for bit in (7, 6, 5, 4, 3, 2, 1, 0):
        out += ONE if (v >> bit) & 1 else ZERO
    return bytes(out)


def parse_color(value):
    """Normalize a color to an (r,g,b) tuple.

    Accepts:
      "#00FF00" / "#0F0"      hex string
      "0x00FF00"              hex string with prefix
      0x00FF00                int
      (0, 255, 0)             tuple or list of three ints

    Raises ValueError on anything else.
    """
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("#"):
            s = s[1:]
        elif s.startswith("0x"):
            s = s[2:]
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            raise ValueError(f"bad hex color: {value!r}")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    if isinstance(value, (tuple, list)):
        if len(value) != 3:
            raise ValueError(f"color must be (r,g,b): {value!r}")
        return (int(value[0]), int(value[1]), int(value[2]))
    if isinstance(value, int):
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    raise ValueError(f"unsupported color: {value!r}")


def grb(r: int, g: int, b: int) -> bytes:
    """Encode one pixel in WS2812 order (green, red, blue)."""
    return _ws_byte(g) + _ws_byte(r) + _ws_byte(b)


class Ring:
    """A WS2812B ring on SPI0.

    Args:
        num: number of LEDs (loa's ring is 24).
        bus, device: SPI bus/device (default 0,0 = /dev/spidev0.0).
        speed: SPI clock in Hz. 3.2MHz gives four SPI bits per WS2812 bit.
    """

    def __init__(self, num: int = 24, bus: int | None = None,
                 device: int | None = None, speed: int = 3_200_000):
        import spidev              # lazy: animations stay importable anywhere
        from . import config
        cfg = config.load()        # loa.conf is the as-built truth
        self.num = num
        self.spi = spidev.SpiDev()
        self.spi.open(
            int(cfg.get("ring_bus", bus if bus is not None else 0)),
            int(cfg.get("ring_device", device if device is not None else 0)),
        )
        self.spi.max_speed_hz = int(cfg.get("ring_speed", speed))
        self.spi.mode = 0b00

    def show(self, frame) -> None:
        """Render one frame: an iterable of colors, len == self.num.

        Each color may be a tuple (r,g,b) or a hex string like "#00FF00".
        """
        buf = bytearray()
        for color in frame:
            r, g, b = parse_color(color)
            buf += grb(int(r), int(g), int(b))
        buf += b'\x00' * 24            # latch: 60us low
        self.spi.writebytes2(list(buf))
        self._publish(frame)

    def _publish(self, frame) -> None:
        """Publish the exact display values to /dev/shm/loa-ring.bin (72B).

        The retained topic of the loa frame bus — RAM-backed (tmpfs), zero
        flash writes. presence publishes, the bench TUI (and any future
        subscriber) reads the last value. Ephemeral by nature: it republishes
        the moment the daemon runs. Config/cortex.db stay on disk; a live
        mirror does not.

        Opens fresh every frame like the OLED topic: a deleted or cleaned
        file is recreated on the next publish instead of writing to a stale
        fd that no longer exists in the directory.
        """
        try:
            raw = bytearray()
            for color in frame:
                r, g, b = parse_color(color)
                raw += bytes((int(r), int(g), int(b)))
            with open("/dev/shm/loa-ring.bin", "wb") as f:
                f.write(raw)
        except Exception:
            pass

    def fill(self, rgb) -> None:
        """Set every LED to one color — tuple, hex string, or int."""
        self.show([rgb] * self.num)

    def off(self) -> None:
        self.fill((0, 0, 0))

    def close(self) -> None:
        self.spi.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()