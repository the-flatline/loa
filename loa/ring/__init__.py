"""ring — THE LEDS, a limb.

  neopixel    the WS2812B over SPI1, the hardware (was loa/ws2812.py)
  encode      code space + dither, the colour encoder (was loa/render.py)
  animations  the voice: pure-math frame sequences
  __main__    the blit loop  (python -m loa.ring, was loa/ring.py)

The ring's BUILDER is not here — the brain owns it, in loa/cortex/ring.py.
"""
