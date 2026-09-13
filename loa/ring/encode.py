"""render — converts animation frames to WS2812 code space.

Pipeline (one pass):

  1. Design curves are authored in the CODE space Divv approved (peak 35).
  2. Dither in code space: per-LED per-channel error accumulators emit
     integer codes, carrying the fractional remainder forward. This
     synthesizes between-code brightness without losing the low end.
  3. No gamma expansion of the approved values — the peak stays 35. (The
     earlier 'gamma' pass expanded 35 -> 125 and blinded the room.)

Perceived-space gamma exists only as a scaling helper for future use; the
ring's non-linearity means steps are perceptually uneven, but dithering in
code space still smooths the fade and preserves the approved brightness.
"""
import math

GAMMA = 2.8          # WS2812-ish response exponent (scaling helper only)


def perceived(code: float) -> float:
    """code 0..255 -> perceived brightness 0..255 (code^gamma)."""
    return 255.0 * (max(0.0, min(255.0, code)) / 255.0) ** GAMMA


def code_from_perceived(p: float) -> float:
    """perceived 0..255 -> code 0..255 (inverse gamma)."""
    return 255.0 * (max(0.0, min(255.0, p)) / 255.0) ** (1.0 / GAMMA)


class DitheredFrame:
    """Per-LED, per-channel error diffusion in CODE space.

    Accumulators reset per instance; create one per state so no fractional
    carryover flashes when the ring changes state.
    """

    def __init__(self, num):
        self.num = num
        self.acc = [[0.0, 0.0, 0.0] for _ in range(num)]

    def render(self, ring, frame):
        """frame: list of (r,g,b) DESIGN-CODE floats. Writes dithered codes."""
        codes = []
        for i, (r, g, b) in enumerate(frame):
            out = []
            for ch, val in enumerate((r, g, b)):
                self.acc[i][ch] += val
                emitted = int(self.acc[i][ch])
                self.acc[i][ch] -= emitted
                out.append(max(0, min(255, emitted)))
            codes.append((out[0], out[1], out[2]))
        ring.show(codes)