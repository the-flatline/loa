"""oled — THE GLASS, a limb.

A pure display: it subscribes to the `ripperdoc` topic and blits the 1024
bytes the cortex rendered, and it pushes nothing up. It may reach its
DRIVER and the geometry and the topic, and nothing else — enforced in
tests/test_display_boundary.py.

  driver     the SH1106 over SPI0 (was loa/panel.py)
  __main__   the blit loop  (python -m loa.oled, was loa/oled.py)
"""
