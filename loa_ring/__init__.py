"""loa_ring — the voice of loa.

Hardware layer (Ring): drives a WS2812B ring over SPI (GPIO10 MOSI).
Animation layer (animations): pure math, no hardware — returns frames as
lists of LED_COUNT (r,g,b) tuples, so they can be rendered, simulated, or
tested anywhere.
"""
from .ring import Ring
from . import animations
from . import control
from . import presence

__all__ = ["Ring", "animations", "control", "presence"]
__version__ = "0.2.0"