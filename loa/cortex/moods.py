"""moods — the feelings vocabulary.

Each mood is intent: what the ring should do and what the face should show.
The API translates intent into cortex state (SQLite); the daemons poll that
state every frame and own the hardware. The brain just says how it feels.

Ring targets:
  home / busy / alarm = sustained states (the daemon holds them)
  scan / glitch       = one-shot events (daemon fires once, then clears)

The default mood is "calm": the flatline baseline — a flat scope trace with
occasional blips. That is the house look.
"""

from . import face

MOODS = {
    "calm": {
        "ring": "home",
        "oled": {"mode": "scope", "dim": False},
        "desc": "the flatline baseline — flat line, occasional blip",
    },
    "busy": {
        "ring": "busy",
        "oled": {"mode": "ecg", "dim": False},
        "desc": "working — amber breath, heart-trace on the face",
    },
    "pleased": {
        "ring": "scan",
        "oled": {"mode": "ripple", "dim": False},
        "desc": "satisfied — one comet lap, smooth ripple",
    },
    "annoyed": {
        "ring": "glitch",
        "oled": {"mode": "noise", "dim": False},
        "desc": "corrupted — RGB-split stutter, static on the face",
    },
    "alarmed": {
        "ring": "alarm",
        "oled": {"mode": "text", "text": "!! FLATLINE !!", "dim": False},
        "desc": "the yell — full red triple pulse, name on the face",
    },
    "asleep": {
        "ring": "home",
        "oled": {"mode": "ripple", "dim": True},
        "desc": "off but not gone — dim ripple, panel at low contrast",
    },
}


#: A one-shot the ring plays once and forgets. It is a RECORD, not a flag.
ONESHOT_RING = ("scan", "glitch")


def apply_ring(cortex, target):
    """A ring target.

    Sustained states (home/busy/alarm) are STATE: the ring holds them until it
    is told otherwise. One-shots (scan/glitch) are RECORDS: they ride the event
    topic and the ring plays the newest one it has not played. They used to be a
    `pending_event` flag in the state that the ring daemon CLEARED — a daemon
    writing another service's state, which is exactly what this design removes,
    and which silently ate a second one-shot that arrived before the first was
    cleared.
    """
    if target in ONESHOT_RING:
        cortex.log_event("ring", {"state": target})
    else:
        cortex.set_state({"ring_state": target})


def oled_state_for(mood):
    """The face state a mood wants: mode + optional text + dim flag."""
    return dict(MOODS[mood]["oled"])