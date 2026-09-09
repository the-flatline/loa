"""bench — the bench console (loa-bench). Textual TUI + quick CLI.

Two faces, one command:

  loa-bench            — the TUI: Workbench-chrome console with digital twin
                         panes (OLED + ring rendered by the REAL loa_ring
                         renderers), live status, ripperdoc/mood/page keys.
  loa-bench on [page]  — flip ripperdoc on (page: sensors|pir|snr)
  loa-bench off        — back to scope
  loa-bench page <p>   — switch the ripperdoc page
  loa-bench status     — what the face is doing now

Runs ON loa, talks to the cortex API on 127.0.0.1:8765 (LOA_API_BIND/PORT
override) — no listener of its own, no web. You SSH to the box and run it.

Look: Workbench 1.3 — square corners, gadget title bars, the four-colour
palette, a CRT backdrop. No rounded corners, no gradients, no spin.
"""
import json
import os
import sys
import time
import urllib.request

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static

from . import animations as anim
from . import oled
from . import oled_daemon

DEFAULT_PORT = 8765
POLL_S = 0.5

# Workbench 1.3 palette + CRT backdrop
BLACK = "#000000"
WHITE = "#FFFFFF"
BLUE = "#0F0FAF"
ORANGE = "#FF9900"
BACKDROP = "#0A0E12"
DIM = "#232C38"

CSS = f"""
Screen {{ background: {BACKDROP}; }}
#menubar {{ height: 1; background: {BLUE}; color: {WHITE}; }}
#status {{ height: 3; background: {BLACK}; color: {WHITE};
          border: solid {BLUE}; }}
#twin {{ height: auto; }}
.pane {{ border: solid {BLUE}; background: {BLACK}; }}
.pane-title {{ color: {ORANGE}; }}
#log {{ height: 8; border: solid {BLUE}; color: {DIM}; }}
Footer {{ background: {BLUE}; color: {WHITE}; }}
"""

MENUS = ("STATUS", "TWIN", "LAB", "CONTROL", "LOG")


def _base():
    host = os.environ.get("LOA_API_BIND", "127.0.0.1")
    port = os.environ.get("LOA_API_PORT", DEFAULT_PORT)
    return f"http://{host}:{port}"


def _post(path, payload):
    req = urllib.request.Request(
        f"{_base()}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _get(path):
    with urllib.request.urlopen(f"{_base()}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def _px(frame, x, y):
    """Pixel from the page-major SH1106 framebuffer (same layout as blit)."""
    return bool(frame.buf[(y // 8) * oled.WIDTH + x] & (1 << (y % 8)))


def oled_art(frame):
    """128x64 framebuffer -> half-block art, HARD-LOCKED to the matrix.

    One cell = 1x2 pixels: 128 cols x 32 rows. No scaling — the twin is the
    panel. Resize the terminal to fit, not the art.
    """
    lines = []
    for y in range(0, 64, 2):
        row = []
        for x in range(0, oled.WIDTH):
            t = _px(frame, x, y)
            b = _px(frame, x, y + 1) if y + 1 < 64 else False
            row.append("█" if t and b else "▀" if t else "▄" if b else " ")
        lines.append("".join(row))
    return "\n".join(lines)


def _led_positions(r=7, cx=19, cy=9):
    """24 LEDs on a circle in a 2:1 char grid (chars are ~2x tall)."""
    import math
    pts = []
    for i in range(24):
        a = math.radians(i * 15)
        pts.append((round(cx + r * 0.5 * math.sin(a)),
                    round(cy - r * math.cos(a))))
    return pts


def ring_art(frame):
    """Ring frame (list of RGB float tuples) -> Textual markup blocks.

    Markup, not raw ANSI, so colours honour the terminal's real capability
    (no gray fallback on 256-colour terminals).
    """
    pos = _led_positions()
    dark = "[on rgb(10,14,18)]  [/]"
    grid = [[dark for _ in range(40)] for _ in range(20)]
    for i, led in enumerate(frame):
        r, g, b = (max(0, min(255, int(c))) for c in led)
        x, y = pos[i]
        grid[y][x] = f"[on rgb({r},{g},{b})]  [/]"
    return "\n".join("".join(row) for row in grid)


def fetch_sense():
    try:
        st = _get("/state")
    except Exception:
        return "  api down"
    s = st.get("sense", {})
    page = st.get("ripperdoc_page", "sensors")
    mood = st["mood"]["feeling"]
    ring = st["ring"]["state"]
    oled_mode = st["oled"]["mode"]
    pir = "SOLID" if s.get("pir_high") else "open"
    return (f" mood {mood:8s} ring {ring:6s} oled {oled_mode:9s} "
            f"page {page:7s} PIR {pir:5s} "
            f"N{s.get('count', 0):04d} T{s.get('last_hold', 0.0):5.1f}s")


class BenchApp(App):
    """loa-bench — the bench console."""

    CSS = CSS
    BINDINGS = [
        ("r", "ripperdoc", "ripperdoc"),
        ("1", "page_sensors", "sensors page"),
        ("2", "page_pir", "pir page"),
        ("3", "page_snr", "snr page"),
        ("m", "mood", "cycle mood"),
        ("q", "quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("  " + "   ".join(MENUS), id="menubar")
        yield Static("boot", id="status")
        with Horizontal(id="twin"):
            with Vertical(classes="pane"):
                yield Static("OLED twin", classes="pane-title")
                yield Static("", id="oled-pane")
            with Vertical(classes="pane"):
                yield Static("RING twin", classes="pane-title")
                yield Static("", id="ring-pane")
        yield Static("", id="log")
        yield Footer()

    def on_mount(self):
        self.set_interval(POLL_S, self._poll)
        self._mood_i = 0

    def _poll(self):
        try:
            st = _get("/state")
        except Exception:
            st = {}
        self.query_one("#status", Static).update(fetch_sense())
        if not st:
            return
        # face twin: run the real renderer for the current mode
        frame = oled.Frame()
        mode = st["oled"]["mode"]
        if mode == "off":
            frame.clear()
        else:
            cls = oled_daemon.MODE_CLASSES.get(mode, oled.Marquee)
            try:
                if mode == "text":
                    r = cls(st["oled"].get("text") or "LOA")
                else:
                    r = cls()
                if hasattr(r, "draw_state"):
                    r.draw_state(frame, time.time(), st)
                else:
                    r.draw(frame, time.time())
            except Exception:
                pass
        self.query_one("#oled-pane", Static).update(oled_art(frame))
        # ring twin: home breath frames (the body's default)
        breath = anim.breath_frames(peak=anim.HOME_PEAK, fps=10)
        f = [anim.hsv(anim.home_hue(time.time()), 1.0,
                      max(p) / 255) for p in breath[0]]
        self.query_one("#ring-pane", Static).update(ring_art(f))

    def action_ripperdoc(self):
        try:
            _post("/ripperdoc", {"on": not _get("/state").get("ripperdoc", False)})
        except Exception:
            pass

    def action_page_sensors(self):
        try:
            _post("/ripperdoc", {"page": "sensors"})
        except Exception:
            pass

    def action_page_pir(self):
        try:
            _post("/ripperdoc", {"page": "pir"})
        except Exception:
            pass

    def action_page_snr(self):
        try:
            _post("/ripperdoc", {"page": "snr"})
        except Exception:
            pass

    def action_mood(self):
        from . import moods
        names = list(moods.MOODS)
        self._mood_i = (self._mood_i + 1) % len(names)
        try:
            _post("/feel", {"feeling": names[self._mood_i]})
        except Exception:
            pass

    def action_quit(self):
        self.exit()


def main():
    args = sys.argv[1:]
    if not args:
        BenchApp().run()
        return 0
    if args[0] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    cmd = args[0]
    try:
        if cmd == "on":
            page = args[1] if len(args) > 1 else None
            body = {"on": True}
            if page:
                body["page"] = page
            r = _post("/ripperdoc", body)
            print(f"ripperdoc ON — page {r['page']}")
        elif cmd == "off":
            r = _post("/ripperdoc", {"on": False})
            print(f"ripperdoc OFF — {r['oled']}")
        elif cmd == "page":
            page = args[1] if len(args) > 1 else None
            if page is None:
                print("usage: loa-bench page <sensors|pir|snr>")
                return 2
            r = _post("/ripperdoc", {"page": page})
            print(f"page {r['page']}")
        elif cmd == "status":
            st = _get("/state")
            print(f"ripperdoc {'ON' if st['ripperdoc'] else 'OFF'} · "
                  f"page {st['ripperdoc_page']} · oled {st['oled']['mode']}")
        else:
            print(f"unknown command: {cmd}")
            return 2
    except Exception as e:
        print(f"loa-bench: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())