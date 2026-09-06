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

    def __init__(self, num: int = 24, bus: int = 0, device: int = 0,
                 speed: int = 3_200_000):
        import spidev              # lazy: animations stay importable anywhere
        self.num = num
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed
        self.spi.mode = 0b00

    def show(self, frame) -> None:
        """Render one frame: an iterable of (r,g,b) tuples, len == self.num."""
        buf = bytearray()
        for (r, g, b) in frame:
            buf += grb(int(g), int(r), int(b))
        buf += b'\x00' * 24            # latch: 60us low
        self.spi.writebytes2(list(buf))

    def fill(self, rgb) -> None:
        """Set every LED to one (r,g,b)."""
        self.show([rgb] * self.num)

    def off(self) -> None:
        self.fill((0, 0, 0))

    def close(self) -> None:
        self.spi.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()