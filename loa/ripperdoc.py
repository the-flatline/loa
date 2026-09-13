"""ripperdoc — the bench console. Textual TUI + quick CLI.

Two faces, one command:

  ripperdoc            — the TUI: Workbench-chrome console with digital twin
                         panes (OLED + ring rendered by the REAL loa
                         renderers), live status, ripperdoc/mood/page keys.
  ripperdoc on [page]  — flip ripperdoc on (page: sensors|pir|snr|temp|frag|power|fault)
  ripperdoc off        — back to scope
  ripperdoc page <p>   — switch the ripperdoc page
  ripperdoc status     — what the face is doing now

Runs anywhere. Its DATA comes off the topic feed — a ZeroMQ subscription to the
body's own publisher (`LOA_TOPIC_ENDPOINT`, else `LOA_API_BIND` on the feed's
port) — so point it at the body from dixie (`LOA_API_BIND=192.168.1.200`) and it
never has to run ON the Pi. HTTP is for COMMANDS ONLY (`_post` to /ripperdoc,
/display, /feel); nothing a pane draws is ever fetched. It is a full-screen
redraw loop: on the uncooled Pi at 68% of a core it drove the SoC onto its
thermal limit (2026-09-12). The body senses and exposes; drawing belongs where
the CPU is.

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

from . import face
from . import topic

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
    """HTTP READS ARE GONE — the console's data arrives on the topic.

    Kept as a tombstone rather than deleted for two reasons. The test suite
    monkeypatches `ripperdoc._get` to fail loudly, so the name has to exist for
    the tripwire to be armed; and any future code that reaches for a live HTTP
    read here fails on the spot instead of quietly polling. Commands are the
    only thing HTTP is for, and they go through `_post()`.
    """
    raise RuntimeError(
        "no HTTP data path: subscribe to the feed (see loa/topic.py) — "
        "commands use _post(), reads never do")


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


# --------------------------------------------------------------------------- #
# the feed — the console's ONE source of data
# --------------------------------------------------------------------------- #

#: The feed's port on the body. The cortex BINDS tcp://0.0.0.0:5556, which is
#: an address a client cannot reach — so the console connects to the body's
#: host on this port instead.
FEED_PORT = 5556


def _feed_endpoints():
    """Where to subscribe. `LOA_TOPIC_ENDPOINT` names it outright; otherwise it
    is the same host the HTTP door lives behind, on the feed's port (the console
    runs on dixie, so with `LOA_API_BIND=192.168.1.200` the feed is
    tcp://192.168.1.200:5556)."""
    ep = os.environ.get("LOA_TOPIC_ENDPOINT")
    if ep:
        return [ep]
    host = os.environ.get("LOA_API_BIND") or "127.0.0.1"
    return ["tcp://%s:%d" % (host, FEED_PORT)]


def _faults_from(msg):
    """The fault topic's rows, in the shape the PAIN page renders.

    The rows are STRUCTURED on the wire (level/code/text/face), so nothing here
    splits a "level|label" string — that parse is exactly what left the page
    guessing. This only counts them into the faults/warns the panel wants.
    """
    rows = [{"level": r.level, "code": r.code, "text": r.text,
             "face": r.face or r.code} for r in msg.rows]
    return {"ts": msg.ts or time.time(),
            "rows": rows,
            "faults": sum(1 for r in rows if r["level"] == "fault"),
            "warns": sum(1 for r in rows if r["level"] == "warn")}


class Feed:
    """The console's data. ONE source: the topic.

    Not /state and not /twin — both are gone, and neither is a fallback. A
    fallback that quietly works is how a console ends up looking like it is on
    pub/sub while it is polling, which is exactly what happened here and is why
    Divv had to insist more than once.

    Subscribes to EVERY topic the cortex publishes and keeps the newest message
    of each. The readings are PARTIAL — a daemon sends only what it measured —
    so the merge is by UPDATE, never replace: a motion reading carrying
    pir_high=false must not wipe the mood.

    The twin comes off two topics and no side-channel: the face (1024 B, 1bpp)
    rides INSIDE the ripperdoc message, the ring (72 B, 24 px RGB) on the ring
    topic. Raw bytes, never hex and never base64 — the encoding that drew twelve
    wrong LEDs and read as a hardware fault.
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
        self._sub = topic.Subscriber(endpoints=endpoints or _feed_endpoints(),
                                     topics=list(topic.TOPICS))
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                got = self._sub.recv(1000)
            except topic.SchemaMismatch as e:
                # say so and stop: guessing at a newer schema is how you draw
                # twelve wrong pixels and blame the hardware
                self.error = str(e)
                return
            except Exception as e:                              # noqa: BLE001
                self.error = "%s: %s" % (type(e).__name__, e)
                time.sleep(1.0)
                continue
            if got is None:
                continue
            name, env = got
            try:
                with self._lock:
                    self._ingest(name, env)
                    self.last = time.time()
                    self.counts[name] = self.counts.get(name, 0) + 1
                self.error = None
            except Exception as e:                              # noqa: BLE001
                # A message the console cannot read is REPORTED, not swallowed:
                # a thread that dies on one bad topic and leaves the panes
                # looking calm is the silent-feed bug with extra steps.
                self.error = "%s: %s" % (type(e).__name__, e)

    def _ingest(self, name, env):
        """One topic message into the flat state the pages already speak."""
        msg = getattr(env, name)
        if name == "ripperdoc":
            # The panel, as the body drove it. This is the twin: hold the bytes
            # and render them, do not re-derive them — the console's copy of the
            # renderer drifts from the body's the moment either one changes.
            if msg.HasField("face"):
                self.face = bytes(msg.face)
            self.state.update(topic.dict_to_state(name, msg))
        elif name == "ring":
            if msg.HasField("ring"):
                self.ring = bytes(msg.ring)
        elif name == "power":
            # rails is a MAP, and a map has no scalar state key in
            # TOPIC_STATE_MAP — it is carried by hand, as the wire contract says.
            self.state["power"] = dict(msg.rails)
            # The builder on the body writes `frag` onto this message, but the
            # schema has no such field (Power = rails, ts), so the vault's seal
            # cannot ride the feed yet. Guarded so a schema that adds it works,
            # and never guessed at in the meantime.
            frag = getattr(msg, "frag", None)
            if frag:
                self.state.update(
                    _normalise({"frag": {str(k): str(v) for k, v in frag.items()}}))
        elif name == "fault":
            self.state["faults"] = _faults_from(msg)
            if msg.HasField("condition"):
                self.state["condition"] = msg.condition
        elif name == "event":
            return                       # a record, not the body's state
        else:
            self.state.update(topic.dict_to_state(name, msg))

    def state_copy(self):
        with self._lock:
            return dict(self.state)

    def frames(self):
        with self._lock:
            return self.face, self.ring

    def age(self):
        return time.time() - (self.last or self.started)

    @property
    def arrived(self):
        """True once any message has landed on the feed."""
        return self.last > 0.0

    def close(self):
        self._sub.close()


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


def _wait_for_feed(f, timeout=3.0):
    """Block until the feed has delivered once, or `timeout` passes.

    A one-shot CLI read (`ripperdoc status`) needs the subscription to have
    spoken before it can report anything. The console never calls this — a
    silent feed is a state it draws, not a stall it waits on.
    """
    deadline = time.time() + timeout
    while time.time() < deadline and not getattr(f, "arrived", f.age() < _FEED_STALE_S):
        time.sleep(0.05)


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
        return "  NO FEED — ALL QUIET (%.0fs silent)" % f.age()
    try:
        st = body_state()
    except Exception:
        return "  body unreachable"
    # A console whose feed has not delivered yet says "?" — it does not crash,
    # and it does not invent a body that is not there.
    page = st.get("page") or st.get("ripperdoc_page") or "?"
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
        self.query_one("#status", Static).update(fetch_sense())
        self._tick_oled()
        self._tick_ring()

    def _tick_oled(self):
        """The panel, as the body drove it — the face arrives on the feed.

        The bytes ride INSIDE the ripperdoc message (1024 B, 1bpp). Drawing them
        beats re-running the renderer here: the console's copy of the renderer
        drifts from the body's the moment either changes, and a twin that
        re-derives the picture is a second implementation wearing the first
        one's name. A feed with no face yet SAYS so — it does not draw a healthy
        body out of nothing.
        """
        face_b, _ring = feed().frames()
        if len(face_b) != face.WIDTH * face.PAGES:
            self.query_one("#oled-pane", Static).update(
                "[orange]NO FEED — the panel has not arrived[/]")
            return
        frame = face.Frame()
        frame.buf[:] = face_b
        self.query_one("#oled-pane", Static).update(oled_art(frame))

    def _tick_ring(self):
        """Mirror the body's ring — RAW bytes off the ring topic.

        The ring arrives as 72 raw bytes (3 per LED, 24 LEDs), so it is passed
        straight through: never decoded as hex, never as base64. A base64 decode
        of a hex frame drew twelve wrong LEDs and read as a hardware fault.
        Nothing has arrived yet and the pane says RING OFFLINE.
        """
        _face, raw = feed().frames()
        if len(raw) < 72:
            self.query_one("#ring-pane", Static).update(
                "[red]RING OFFLINE[/]\n" + ring_art_bytes(bytes(72)))
            return
        self.query_one("#ring-pane", Static).update(ring_art_bytes(raw[:72]))

    def action_ripperdoc(self):
        """Toggle console mode. The current value comes off the FEED — the
        panel's own report — never off a /state read."""
        try:
            on = bool(body_state().get("ripperdoc"))
            _post("/ripperdoc", {"on": not on})
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
            _post("/display", {"flip": not bool(st.get("oled_flip",
                                                       face.DEFAULT_FLIP))})
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
            # A READ, so it comes off the feed like everything else — there is
            # no /state to ask. Wait for the subscription to speak once.
            f = feed()
            _wait_for_feed(f)
            if f.error:
                print(f"ripperdoc: FEED ERROR: {f.error}", file=sys.stderr)
                return 1
            if f.age() > _FEED_STALE_S:
                print(f"ripperdoc: NO FEED — ALL QUIET ({f.age():.0f}s silent)")
                return 1
            st = f.state_copy()
            print(f"ripperdoc {'ON' if st.get('ripperdoc') else 'OFF'} · "
                  f"page {st.get('page', '?')} · oled {st.get('oled_mode', '?')}")
        else:
            print(f"unknown command: {cmd}")
            return 2
    except Exception as e:
        print(f"ripperdoc: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())