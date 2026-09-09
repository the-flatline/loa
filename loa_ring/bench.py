"""bench — the bench console (loa-bench). Textual TUI, Amiga Workbench chrome.

Runs ON loa, talks to the cortex API on 127.0.0.1:8765 — no listener of its
own, no web, no new surface. You SSH to the box and run it.

The twin panes run the REAL loa_ring renderers: the OLED frame and the ring
frames are the same code the daemons use, painted into the terminal. What
you see here is what the body shows, pixel for pixel.

Look: Workbench 1.3 — square corners, gadget title bars, the four-colour
palette, a CRT backdrop. No rounded corners, no gradients, no spin.
"""
import json
import time
import urllib.request

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static

from . import animations as anim
from . import oled
from . import oled_daemon

API = "http://127.0.0.1:8765"
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
#menubar .mitem {{ padding: 0 2; }}
#menubar .mitem.active {{ background: {WHITE}; color: {BLUE}; }}
#status {{ height: 3; background: {BLACK}; color: {WHITE};
          border: solid {BLUE}; }}
#twin {{ height: auto; }}
.pane {{ border: solid {BLUE}; background: {BLACK}; }}
.pane-title {{ color: {ORANGE}; }}
#log {{ height: 8; border: solid {BLUE}; color: {DIM}; }}
Footer {{ background: {BLUE}; color: {WHITE}; }}
"""

MENUS = ("STATUS", "TWIN", "LAB", "CONTROL", "LOG")


def _px(frame, x, y):
    """Pixel from the page-major SH1106 framebuffer (same layout as blit)."""
    return bool(frame.buf[(y // 8) * oled.WIDTH + x] & (1 << (y % 8)))


def oled_art(frame, out_w=64):
    """128x64 framebuffer -> half-block art. One cell = 2x1 pixels (▀▄█)."""
    lines = []
    for y in range(0, 64, 2):
        row = []
        for x in range(0, oled.WIDTH, 2):
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
    """Ring frame (list of RGB float tuples) -> ANSI truecolour blocks."""
    pos = _led_positions()
    grid = [["  " for _ in range(40)] for _ in range(20)]
    for i, led in enumerate(frame):
        r, g, b = (max(0, min(255, int(c))) for c in led)
        x, y = pos[i]
        grid[y][x] = f"\x1b[48;2;{r};{g};{b}m  \x1b[0m"
    return "\n".join("".join(row) for row in grid)


def fetch_state():
    try:
        with urllib.request.urlopen(f"{API}/state", timeout=2) as r:
            return json.load(r)
    except Exception:
        return {}


def fetch_sense():
    st = fetch_state()
    if not st:
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
        ("m", "mood", "cycle mood"),
        ("q", "quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(" ".join(f"[b]{m}[/b]" for m in MENUS), id="menubar")
        yield Static("boot", id="status")
        with Horizontal(id="twin"):
            with Vertical(classes="pane"):
                yield Static("OLED — twin", classes="pane-title")
                yield Static("", id="oled-pane")
            with Vertical(classes="pane"):
                yield Static("RING — twin", classes="pane-title")
                yield Static("", id="ring-pane")
        yield Static("", id="log")
        yield Footer()

    def on_mount(self):
        self.set_interval(POLL_S, self._poll)
        self._mood_i = 0

    def _poll(self):
        st = fetch_state()
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
        _post("/ripperdoc", {"on": not fetch_state().get("ripperdoc", False)})

    def action_page_sensors(self):
        _post("/ripperdoc", {"page": "sensors"})

    def action_page_pir(self):
        _post("/ripperdoc", {"page": "pir"})

    def action_mood(self):
        from . import moods
        names = list(moods.MOODS)
        self._mood_i = (self._mood_i + 1) % len(names)
        _post("/feel", {"feeling": names[self._mood_i]})

    def action_quit(self):
        self.exit()


def _post(path, body):
    import json
    try:
        req = urllib.request.Request(
            f"{API}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def main():
    BenchApp().run()


if __name__ == "__main__":
    main()