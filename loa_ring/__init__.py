"""loa-ring — the loa outpost control stack.

Hardware layers (Ring: WS2812B over SPI; SH1106: the face over SPI0).
Animation layers (pure math, no hardware — render anywhere).
Cortex (cortex): SQLite state + event log — the nervous system.
API (api): the FastAPI door the brain talks to.
Daemons (presence: ring; oled_daemon: face).
"""

__version__ = "0.5.4"

from .ring import Ring
from . import animations
from . import cortex
from . import control
from . import presence
from . import sense
from . import oled
from . import oled_daemon
from . import amiga
from . import moods
from . import expressions
from . import api

__all__ = [
    "Ring", "animations", "cortex", "control", "presence", "sense", "oled",
    "oled_daemon", "amiga", "moods", "expressions", "api",
]