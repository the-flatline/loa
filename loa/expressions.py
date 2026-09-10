"""expressions — what the face says.

A registry of named expressions. Today they render as text/glyphs on the
OLED (the face is 128x64); when the 3.5" screen arrives, expressions keep
their names and get real face drivers — this registry is the contract.

Some expressions carry a ring accent (an event the ring throws once).
"""

EXPRESSIONS = {
    "neutral": {
        "text": "—",
        "ring": None,
        "desc": "no face at all — a flat dash",
    },
    "happy": {
        "text": ":) :)",
        "ring": "scan",
        "desc": "pleased — smile glyphs, one comet lap",
    },
    "think": {
        "text": "? ?",
        "ring": None,
        "desc": "processing — question marks, no ring theatrics",
    },
    "suspicious": {
        "text": ">_>",
        "ring": None,
        "desc": "watching — side-eye glyph, ring stays home",
    },
    "yell": {
        "text": "!!",
        "ring": "glitch",
        "desc": "the yell on the face too — with a corruption stutter",
    },
    "sleep": {
        "text": "z z z",
        "ring": None,
        "desc": "down — sleep glyphs, ring home",
    },
}