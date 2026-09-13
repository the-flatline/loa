"""ripperdoc — the bench console. Textual TUI + quick CLI.

Two faces, one command:

  ripperdoc            — the TUI: Workbench-chrome console with digital twin
                         panes (OLED + ring rendered by the REAL loa
                         renderers), live status, ripperdoc/mood/page keys.
  ripperdoc on [page]  — flip ripperdoc on (page: sensors|pir|snr|temp|frag|power|fault)
  ripperdoc off        — back to scope
  ripperdoc page <p>   — switch the ripperdoc page
  ripperdoc status     — what the face is doing now

Runs anywhere. Talks to the cortex API over HTTP (`LOA_API_BIND` /
LOA_API_PORT, default 127.0.0.1:8765) — so point it at the body from dixie
(`LOA_API_BIND=192.168.1.200`) and it never has to run ON the Pi. It is a
full-screen redraw loop: on the uncooled Pi at 68% of a core it drove the SoC
onto its thermal limit (2026-09-12). The body senses and exposes; drawing
belongs where the CPU is.

Look: Workbench 1.3 — square corners, gadget title bars, the four-colour
palette, a CRT backdrop. No rounded corners, no gradients, no spin.
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.request

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static

from . import cortex
from . import face
from . import topic
from . import oled

DEFAULT_PORT = 8765
POLL_S = 0.1

#: How long the feed can be silent before the console says so instead of
#: showing its last picture forever. A console that trusts a stale reading is
#: the ghost-temperature bug with a different coat on.
_FEED_STALE_S = 5.0

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
    return bool(frame.buf[(y // 8) * face.WIDTH + x] & (1 << (y % 8)))


def oled_art(frame):
    """128x64 framebuffer -> half-block art, HARD-LOCKED to the matrix.

    One cell = 1x2 pixels: 128 cols x 32 rows. No scaling — the twin is the
    panel. Resize the terminal to fit, not the art.
    """
    lines = []
    for y in range(0, 64, 2):
        row = []
        for x in range(0, face.WIDTH):
            t = _px(frame, x, y)
            b = _px(frame, x, y + 1) if y + 1 < 64 else False
            row.append("█" if t and b else "▀" if t else "▄" if b else " ")
        lines.append("".join(row))
    return "\n".join(lines)


def _led_positions():
    """24 LEDs clockwise around a rounded-rectangle ring, IN ORDER.

    Order matters more than shape — the comet's direction only reads if
    LED n+1 sits next to LED n. Clean slots, no rounding collisions.
    """
    top = [(x, 2) for x in (6, 10, 14, 18, 22, 26, 30, 34)]       # 0-7
    right = [(36, y) for y in (5, 8, 11, 14)]                     # 8-11
    bottom = [(x, 17) for x in (34, 30, 26, 22, 18, 14, 10, 6)]   # 12-19
    left = [(4, y) for y in (14, 11, 8, 5)]                       # 20-23
    return top + right + bottom + left


def ring_art_bytes(raw, pos=None):
    """Render 72 bytes of display-space LED values (the loa frame bus)."""
    pos = pos or _led_positions()
    dim = "[on rgb(12,16,12)]  [/]"
    grid = [[dim for _ in range(40)] for _ in range(20)]
    for i in range(24):
        r, g, b = raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]
        if r + g + b < 12:            # off LEDs keep the ring shape visible
            continue
        x, y = pos[i]
        grid[y][x] = f"[on rgb({r},{g},{b})]  [/]"
    return "\n".join("".join(row) for row in grid)


# the body's ring topic. It is the BODY's path: read it from dixie and you get
# nothing, forever, because it only exists on the body. Fetch /twin instead.
RING_TOPIC = "/dev/shm/loa-ring.bin"

# the body's /twin payload, cached one poll. The ring pane and the frag page
# both want it and POLL_S is 0.1s — two fetches a tick is two round trips to
# the Pi for one picture.
class Feed:
    """The console's data. ONE source: the topic.

    Not /state and not /twin — both are gone, and neither is a fallback. A
    fallback that quietly works is how a console ends up looking like it is on
    pub/sub while it is polling, which is exactly what happened here and is why
    Divv had to insist more than once.

    A State message is PARTIAL — each daemon publishes only what it measured —
    so this MERGES by presence. Replacing the dict would let a motion reading
    wipe the mood.
    """

    def __init__(self, endpoints=None):
        self._lock = threading.Lock()
        self.state = {}
        self.face = b""
        self.ring = b""
        self.last = 0.0
        self.error = None
        self.counts = {}
        self.started = time.time()
        self._sub = topic.Subscriber(endpoints=endpoints)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                env = self._sub.recv(1000)
            except topic.SchemaMismatch as e:
                # say so and stop: guessing at a newer schema is how you draw
                # twelve wrong pixels and blame the hardware
                self.error = str(e)
                return
            except Exception as e:                              # noqa: BLE001
                self.error = "%s: %s" % (type(e).__name__, e)
                time.sleep(1.0)
                continue
            if env is None:
                continue
            which = env.WhichOneof("body")
            with self._lock:
                if which == "state":
                    self.state.update(
                        _normalise(topic.state_fields_present(env.state)))
                elif which == "frames":
                    self.face, self.ring = env.frames.face, env.frames.ring
                self.last = time.time()
                self.counts[which] = self.counts.get(which, 0) + 1

    def state_copy(self):
        with self._lock:
            return dict(self.state)

    def frames(self):
        with self._lock:
            return self.face, self.ring

    def age(self):
        return time.time() - (self.last or self.started)


def _normalise(fields):
    """The wire shape to the shape the pages render.

    The feed carries `faults` as "level|label" strings and `frag` as a string
    map, because a schema should carry FACTS and not a rendering. The pages want
    rows and typed seal values, so the conversion lives here — once, beside the
    subscription, rather than inside every page.
    """
    out = dict(fields)
    raw_faults = out.get("faults")
    if raw_faults is not None:
        rows = []
        for s in raw_faults:
            level, _, label = str(s).partition("|")
            rows.append({"level": level, "code": label, "face": label,
                         "text": ""})
        out["faults"] = {
            "ts": time.time(),
            "rows": rows,
            "faults": sum(1 for r in rows if r["level"] == "fault"),
            "warns": sum(1 for r in rows if r["level"] == "warn"),
        }
    frag = out.get("frag")
    if frag is not None:
        def _int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None
        out["frag"] = {
            "sealed": str(frag.get("sealed")).lower() in ("true", "1", "yes"),
            "entries": _int(frag.get("entries")),
            "access_count": _int(frag.get("access_count")),
            "hash": frag.get("hash"),
            "marker": frag.get("marker") or None,
        }
    return out


FEED = None


def feed():
    """The one Feed for this process. Two subscriptions would be a bug."""
    global FEED
    if FEED is None:
        FEED = Feed()
    return FEED


def body_state():
    """The BODY's state — as it arrives on the topic.

    Never cortex.get_state() (that reads whichever cortex.db is on the machine
    the console runs on) and never /state (a poll wearing a feed's clothes).
    The console subscribes.
    """
    return feed().state_copy()


def twin_payload():
    """The body's frames, from the feed. `/twin` is GONE.

    Kept as a function because callers want the same shape, but there is no
    request behind it any more.
    """
    face_b, ring_b = feed().frames()
    return {"ring": ring_b.hex(),
            "face": base64.b64encode(face_b).decode(),
            "status": body_state()}


def fetch_sense():
    f = feed()
    if f.error:
        return "  FEED ERROR: %s" % f.error[:40]
    if f.age() > _FEED_STALE_S:
        # Say it. A console that shows its last picture forever is how you end
        # up trusting a stale reading — the ghost temperature all over again.
        return "  NO FEED (%.0fs silent)" % f.age()
    try:
        st = body_state()
    except Exception:
        return "  body unreachable"
    # A console whose feed has not delivered yet says "?" — it does not crash,
    # and it does not invent a body that is not there.
    page = st.get("ripperdoc_page") or "?"
    mood = st.get("mood") or "?"
    ring = st.get("ring_state") or "?"
    oled_mode = st.get("oled_mode") or "?"
    pir = "SOLID" if st.get("pir_high") else "open"
    snr = "ON" if st.get("snr_cm") is not None else "OFF"
    p = st.get("power") or {}
    v3, fl = p.get("3V3_SYS_V"), p.get("throttled")
    if v3 is None:
        pwr = "PWR --"
    else:
        pwr = f"3V3 {v3:.2f}V"
        if fl is not None and fl & 0x1:
            pwr += " UV!"          # 5V input sagging RIGHT NOW
        if fl is not None and fl & 0x4:
            pwr += " THR!"
    return (f" mood {mood:8s} ring {ring:6s} oled {oled_mode:9s} "
            f"page {page:7s} PIR {pir:5s} SNR {snr:3s} "
            f"N{st.get('pir_count', 0):04d} T{st.get('pir_last_hold', 0.0):5.1f}s "
            f"{pwr}")


class RipperdocApp(App):
    """ripperdoc — the bench console."""

    CSS = CSS
    BINDINGS = [
        ("r", "ripperdoc", "ripperdoc"),
        ("1", "page_sensors", "sensors page"),
        ("2", "page_pir", "pir page"),
        ("3", "page_snr", "snr page"),
        ("4", "page_temp", "temp page"),
        ("5", "page_frag", "frag page"),
        ("6", "page_power", "power page"),
        ("7", "page_fault", "fault page"),
        ("m", "mood", "next mood"),
        ("f", "flip", "flip face 180"),
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
        self.set_interval(POLL_S, self._tick)
        self._mood_i = 0

    def _tick(self):
        try:
            st = body_state()
        except Exception:
            return
        self.query_one("#status", Static).update(fetch_sense())
        # face twin: run the real renderer for the current mode
        frame = face.Frame()
        mode = st.get("oled_mode") or "off"
        if mode == "off":
            frame.clear()
        else:
            cls = oled.MODE_CLASSES.get(mode, face.Marquee)
            try:
                if mode == "text":
                    r = cls(st.get("oled_text") or "LOA")
                else:
                    r = cls()
                if hasattr(r, "draw_state"):
                    r.draw_state(frame, time.time(), st)
                else:
                    r.draw(frame, time.time())
            except Exception:
                pass
        self.query_one("#oled-pane", Static).update(oled_art(frame))
        # ring twin: mirror the body's actual output (the loa frame bus)
        self._tick_ring()

    def _tick_ring(self):
        """Mirror the body's ring — fetched, not read off a local file.

        /twin carries the ring as HEX (the face is base64). Decode it as
        base64 and you get 108 bytes of plausible-looking rubbish that draws
        twelve wrong pixels — and looks like a body fault.
        """
        try:
            raw = bytes.fromhex((twin_payload() or {}).get("ring") or "")
        except Exception:
            raw = b""
        if len(raw) < 72:
            self.query_one("#ring-pane", Static).update(
                "[red]RING OFFLINE[/]\n" + ring_art_bytes(bytes(72)))
            return
        self.query_one("#ring-pane", Static).update(ring_art_bytes(raw))

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

    def action_page_temp(self):
        try:
            _post("/ripperdoc", {"page": "temp"})
        except Exception:
            pass

    def action_page_frag(self):
        try:
            _post("/ripperdoc", {"page": "frag"})
        except Exception:
            pass

    def action_page_power(self):
        try:
            _post("/ripperdoc", {"page": "power"})
        except Exception:
            pass

    def action_page_fault(self):
        try:
            _post("/ripperdoc", {"page": "fault"})
        except Exception:
            pass

    def action_flip(self):
        """Turn the face 180 degrees — on the panel.

        A setting, not a mode: it is remembered in the store and applied at
        startup, so a face mounted upside down stays upside down across a
        reboot. Two commands on the SH1106, so the picture costs the same either
        way and the render loop does not have to rotate a pixel.
        """
        try:
            st = body_state()
            _post("/display", {"flip": not bool(st.get("oled_flip", True))})
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
        RipperdocApp().run()
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
                print("usage: ripperdoc page <sensors|pir|snr|temp|frag|power|fault>")
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
        print(f"ripperdoc: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())