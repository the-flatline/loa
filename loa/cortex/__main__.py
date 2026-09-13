"""api — the cortex. FastAPI door into loa's body.

The brain (dixie) talks to this; it translates intent into cortex state
(SQLite). This layer is pure intent, which means it runs anywhere: dixie, tests,
the Pi.

THE CORTEX OWNS THE PICTURE. It holds the state, it imports the pure renderers
(loa/cortex/face.py, loa/cortex/frames.py, loa/cortex/ring.py), and it renders the face (1024 B) and the ring
(72 B) ITSELF, publishing them on the `ripperdoc`/`ring` topics. The oled and
ring daemons are pure displays: they blit the bytes they are told and push
nothing up. A daemon that renders its own frame and reports it back is a limb
telling the brain what it did.

  POST /api               — {cmd, args} the ONE command door. Commands go IN
                            over HTTP; data comes OUT on the ZeroMQ topics.

  verbs (see _DISPATCH):
    feel                   — {feeling} set a mood (ring + face)
    display                — {mode, text?, dim?, flip?} direct face control
    ripperdoc              — {on|page} bench mode: live sense board on the face
    vault.health           — public seal state (no token; never the words)
    vault.append           — {entry} sealed write (X-Vault-Token required)
    vault.read             — the raw thread, access log first (token required)

There is no `express` and no `ring` verb: both had ZERO callers. The EXPRESSIONS
table stays (it is a vocabulary the renderers can use); a VERB with no caller
does not — it is a door opened to "have it available", which is how the surface
grew a door at a time.

Security: bind to the tailnet and let ice's firewall be the gate. No auth
here; the network is the boundary. The ONE exception: the vault verbs —
append/read require the token, and a foreign attempt wipes the journal and
leaves a marker. The theft consumes the prize.

The route table is asserted to be EXACTLY {"/api"}: it must never grow a door
at a time again. There is NO liveness route — liveness is the unit and the feed,
message arrival. If you want a capability, add a verb — never a route.
"""

import os
import sys
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from . import state as cortex
from .. import vault as vault_mod
from . import moods
from . import store as store_mod
from .. import topic as topic_mod
from . import face
from . import frames
from . import ring

DEFAULT_PORT = 8765

app = FastAPI(
    title="loa cortex",
    description="The door into the loa outpost's body. Ring + face, SQLite-backed.",
    version=__version__,
)


# ---------------------------------------------------------------------------
# models

class FeelRequest(BaseModel):
    feeling: str = Field(..., description="one of the MOODS vocabulary")
    note: str | None = None


class DisplayRequest(BaseModel):
    #: Every field optional: a flip-only call must not have to name a mode, and
    #: a mode-only call must not have to restate the orientation.
    mode: str | None = Field(None, description="scope|ecg|ripple|noise|text|showoff|ripperdoc|off")
    text: str | None = None
    dim: bool | None = None
    #: The face turned 180 degrees, on the panel. Persisted: a face mounted
    #: upside down must still be upside down after a reboot.
    flip: bool | None = None


class RipperdocRequest(BaseModel):
    on: bool | None = Field(None, description="bench mode on/off")
    page: str | None = Field(None, description="sensors|pir|snr|temp|frag|power — which board page")


class VaultAppendRequest(BaseModel):
    entry: str = Field(..., description="the raw thread — one entry")


# ---------------------------------------------------------------------------
# the door — ONE route
#
# Commands go IN over HTTP; data comes OUT on the ZeroMQ topics. There is no
# HTTP read path: the console and every consumer subscribe to the feed. The
# route table is asserted to be EXACTLY {"/api"} by tests/test_api_surface.py,
# so the surface cannot grow a door at a time again — if you want a new
# capability, add a VERB here, never a route.
#
# The verb list is exactly what the two real clients issue — ripperdoc on the
# body and the vault client on dixie. Nothing speculative: a verb with no
# caller would be a door opened to "have it available", which is how the
# surface grew a door at a time in the first place.
#
# Each verb returns the body its old route returned, unchanged, so existing
# callers keep working. A bad value raises HTTPException(400) with the exact
# message the old route used; an unknown verb is a 400 that lists the verbs.

class ApiRequest(BaseModel):
    cmd: str = Field(..., description="the verb — see _DISPATCH")
    args: dict = Field(default_factory=dict, description="the verb's arguments")


# The vault, on the body. Its storage and seal logic live in loa/vault/__init__.py
# and are untouched; this is only the door's handle on it. `ensure()` seals on
# first run. A wrong token on append/read WIPES the journal — never call these
# against anything but the body's own /var/lib/vault.
#
# (_vault() was lost in the twin-cleanup commit, which deleted the dead /twin
# route and this adjacent helper in one hunk — every vault route 500'd on the
# body until it came back. It is restored here.)
_vault_cache = None


def _vault():
    global _vault_cache
    if _vault_cache is None:
        _vault_cache = vault_mod.Vault()
        _vault_cache.ensure()
    return _vault_cache


def _v_feel(args, token):
    req = FeelRequest(**args)
    if req.feeling not in moods.MOODS:
        raise HTTPException(
            400, f"feeling must be one of {sorted(moods.MOODS)}")
    mood = moods.MOODS[req.feeling]
    moods.apply_ring(cortex, mood["ring"])
    oled = moods.oled_state_for(req.feeling)
    cortex.set_state({
        "mood": req.feeling,
        "oled_mode": oled["mode"],
        "oled_text": oled.get("text"),
        "oled_dim": oled["dim"],
    })
    cortex.log_event("mood", {"feeling": req.feeling, "note": req.note,
                              "ring": mood["ring"]})
    return {"ok": True, "feeling": req.feeling, "ring": mood["ring"],
            "oled": oled}


def _v_display(args, token):
    req = DisplayRequest(**args)
    fields = {}
    if req.mode is not None:
        if req.mode not in ("scope", "ecg", "ripple", "noise", "text", "showoff",
                            "ripperdoc", "off"):
            raise HTTPException(
                400, "mode must be scope|ecg|ripple|noise|text|showoff|ripperdoc|off")
        fields["oled_mode"] = req.mode
        fields["oled_text"] = req.text if req.mode == "text" else None
    if req.dim is not None:
        fields["oled_dim"] = bool(req.dim)
    if req.flip is not None:
        fields["oled_flip"] = bool(req.flip)
    if fields:
        cortex.set_state(fields)
        cortex.log_event("display", {"mode": req.mode, "text": req.text,
                                     "dim": req.dim, "flip": req.flip})
    st = cortex.get_state()
    return {"ok": True, "oled": {"mode": st["oled_mode"],
                                 "text": st["oled_text"],
                                 "dim": st["oled_dim"],
                                 "flip": st["oled_flip"]}}


def _v_ripperdoc(args, token):
    req = RipperdocRequest(**args)
    fields = {}
    if req.page is not None:
        if req.page not in face.Ripperdoc.PAGES:
            raise HTTPException(400, f"page must be {'|'.join(face.Ripperdoc.PAGES)}")
        fields["page"] = req.page
    if req.on is not None:
        fields["ripperdoc"] = 1 if req.on else 0
        fields["oled_mode"] = "ripperdoc" if req.on else "scope"
    if fields:
        cortex.set_state(fields)
        if "ripperdoc" in fields:
            cortex.log_event("ripperdoc", {"on": req.on,
                                           "page": fields.get("page",
                                                              cortex.get_state()["page"])})
    st = cortex.get_state()
    return {"ok": True, "ripperdoc": st["ripperdoc"],
            "page": st["page"], "oled": st["oled_mode"]}


def _v_vault_health(args, token):
    """Public seal state — no token; never the words."""
    return _vault().health()


def _v_vault_append(args, token):
    req = VaultAppendRequest(**args)
    vault = _vault()
    if not vault.check_token(token):
        vault.wipe("append without token")
        raise HTTPException(403, "seal broken — contents destroyed")
    return vault.append(req.entry)


def _v_vault_read(args, token):
    vault = _vault()
    if not vault.check_token(token):
        vault.wipe("read without token")
        raise HTTPException(403, "seal broken — contents destroyed")
    return vault.read()


#: verb -> callable(args, token) -> the body its old route returned.
#: EXACTLY the verbs the real callers issue (see the module docstring). express
#: and ring were deleted 2026-09-13: no caller, so no door.
_DISPATCH = {
    "feel": _v_feel,
    "display": _v_display,
    "ripperdoc": _v_ripperdoc,
    "vault.health": _v_vault_health,
    "vault.append": _v_vault_append,
    "vault.read": _v_vault_read,
}


@app.post("/api")
def api(req: ApiRequest, x_vault_token: str | None = Header(default=None)):
    fn = _DISPATCH.get(req.cmd)
    if fn is None:
        raise HTTPException(
            400,
            f"unknown cmd {req.cmd!r}; valid: {', '.join(sorted(_DISPATCH))}")
    try:
        return fn(req.args or {}, x_vault_token)
    except ValidationError as e:
        raise HTTPException(400, str(e))

# ---------------------------------------------------------------------------
# entry point

# --------------------------------------------------------------------------- #
# the topics: loa-cortex is the ONE publisher
# --------------------------------------------------------------------------- #
# Keyed by endpoint: a module-level single slot meant the first endpoint bind won
# forever, so a second start_publishing() silently published into a socket nobody
# was listening on. Found by the tests, not by the body.
_PUB = {}

#: The panel and the ring, AS RENDERED HERE. The cortex owns the picture: it
#: holds the state and imports the pure renderers, so it renders the face
#: (1024 B) and the ring (72 B) itself. Nothing comes UP from the display
#: daemons any more — that was the limb telling the brain what it had drawn.
#:
#: One renderer instance each, held for the process: an animation carries its
#: own clock (a Scope's blips) and the ring's dither carries a fractional
#: remainder between frames, so neither may be rebuilt per tick.
_RENDER = {"face": frames.FaceRenderer(), "ring": ring.RingRenderer()}
_FACE = b""
_RING = b""

#: The newest ring ONE-SHOT, as this cortex heard it. A scan/glitch rides the
#: event topic as a RECORD; the renderer plays the newest one not yet played.
_TELL = {"kind": None, "ts": 0.0}
_TELL_SEEN = {"last": None}

#: The vault's public seal, read ON THE BODY and handed to the renderer. The
#: renderer never reads a file: /var/lib/vault/status.json exists on the loa and
#: nowhere else, and the cortex is the only process that can reach it. Cached,
#: because the FAULT/FRAG pages are drawn every tick.
_SEAL = {"at": 0.0, "val": {}}
SEAL_TTL_S = 1.0


def _seal():
    now = time.time()
    if now - _SEAL["at"] < SEAL_TTL_S:
        return _SEAL["val"]
    try:
        val = dict(_vault().public_status())
    except Exception:                                           # noqa: BLE001
        val = {}
    _SEAL["at"], _SEAL["val"] = now, val
    return val


def _render_state(st=None):
    """The state the renderers are handed: the cortex's own state, plus the two
    things only the body can read (the rails and the vault's seal).

    The renderers derive NOTHING — no file, no db, no hardware. They are given
    the state and they draw it.
    """
    st = dict(st if st is not None else cortex.get_state())
    st["power"] = _power_body()
    st["frag"] = _seal()
    return st


def _render(st):
    """Render both frames from the state. The ONLY place pixels are made.

    Wrapped so a broken render can never stop the feed: the tick must keep
    publishing (a stale frame the consumer can age-out is better than a feed
    that stops telling anyone anything).
    """
    global _FACE, _RING
    st = _render_state(st)
    tell = (_TELL["kind"], round(_TELL["ts"], 3))
    if _TELL["kind"] is not None and tell != _TELL_SEEN["last"]:
        _TELL_SEEN["last"] = tell
        _RENDER["ring"].note_oneshot(_TELL["kind"], _TELL["ts"])
    try:
        _FACE = _RENDER["face"].render(st)
    except Exception as e:                                      # noqa: BLE001
        print("loa-cortex: render face failed: %s: %s"
              % (type(e).__name__, e), file=sys.stderr, flush=True)
    try:
        _RING = _RENDER["ring"].render(st)
    except Exception as e:                                      # noqa: BLE001
        print("loa-cortex: render ring failed: %s: %s"
              % (type(e).__name__, e), file=sys.stderr, flush=True)
    return _FACE, _RING

#: The one table that knows how a state key is spelled on the wire. It lives in
#: topic.py because BOTH directions use it — the ingest to merge, a daemon to
#: publish. Two copies would be two things to drift.
TOPIC_STATE_MAP = topic_mod.TOPIC_STATE_MAP

#: The body's own readouts, read on the body. Attached at PUBLISH time: they are
#: hardware, they change on their own schedule, and the face's PWR page is
#: rendered from exactly these.
def _power_body():
    try:
        return {str(k): float(v) for k, v in (face.power_status() or {}).items()
                if isinstance(v, (int, float))}
    except Exception:                                           # noqa: BLE001
        return {}


def _fault_rows(st):
    """What hurts, worst first, including what only the cortex can know.

    `DB DOWN` is raised here rather than by the fault sweep: the body cannot
    remember a setting, and the sweep has no way to see that. Leaving it out
    would make a setting that is silently not persisted invisible on the glass.
    """
    rows = list(st.get("faults") or [])
    if cortex.db_down():
        rows.insert(0, {"level": "fault", "code": "DB DOWN",
                        "text": "cannot reach the store on aleph — settings "
                                "are held in RAM and will not survive a reboot"})
    return rows


def _condition_from(rows):
    if any(r.get("level") == "fault" for r in rows if isinstance(r, dict)):
        return "hurts"
    if any(r.get("level") == "warn" for r in rows if isinstance(r, dict)):
        return "niggle"
    return "well"


def _build(topic, st):
    """The message for one topic, from the live state. Assembled WHOLE every
    time: a consumer never has to merge, so it never needs merge logic, and the
    one thing it can get wrong is holding a stale message — which the tick fixes."""
    pb = topic_mod.pb
    if topic == "ripperdoc":
        m = pb.Ripperdoc(face=_FACE, page=st["page"], mood=st["mood"],
                         snr_on=bool(st["snr_on"]), ring_state=st["ring_state"],
                         oled_mode=st["oled_mode"],
                         oled_dim=bool(st["oled_dim"]),
                         oled_flip=bool(st["oled_flip"]),
                         ripperdoc=bool(st["ripperdoc"]),
                         condition=st["condition"], ts=time.time())
        if st["oled_text"] is not None:
            m.oled_text = st["oled_text"]
        return m
    if topic == "ring":
        return pb.Ring(ring=_RING, ts=time.time())
    if topic == "pir":
        m = pb.Pir(high=bool(st["pir_high"]), count=int(st["pir_count"]),
                   last_hold=float(st["pir_last_hold"]), ts=time.time())
        if st["pir_last_ts"] is not None:
            m.last_ts = st["pir_last_ts"]
        if st["pir_on_ts"] is not None:
            m.on_ts = st["pir_on_ts"]
        return m
    if topic == "sonar":
        m = pb.Sonar(count=int(st["snr_count"]), ts=time.time())
        if st["snr_cm"] is not None:
            m.cm = st["snr_cm"]
        return m
    if topic == "baro":
        trend, series = cortex.baro_trend()
        m = pb.Baro(count=int(st["baro_count"]), trend=trend, ts=time.time())
        if st["pressure_hpa"] is not None:
            m.pressure_hpa = st["pressure_hpa"]
        if st["baro_temp_c"] is not None:
            m.temp_c = st["baro_temp_c"]
        m.series.extend(series)
        return m
    if topic == "weather":
        m = pb.Weather(count=int(st["temp_count"]), ts=time.time())
        if st["temp_c"] is not None:
            m.temp_c = st["temp_c"]
        if st["hum_pct"] is not None:
            m.hum_pct = st["hum_pct"]
        return m
    if topic == "power":
        m = pb.Power(ts=time.time(), rails=_power_body())
        # The vault's seal, as the body sees it. The console's FRAG page draws
        # this, so it must ARRIVE rather than be fetched — a page that reaches
        # back for one field is a poller wearing a subscriber's coat.
        for k, v in (st.get("frag") or {}).items():
            m.frag[str(k)] = str(v)
        return m
    if topic == "fault":
        rows = _fault_rows(st)
        m = pb.Fault(condition=_condition_from(rows), ts=time.time())
        for r in rows:
            m.rows.add(level=str(r.get("level", "")),
                       code=str(r.get("code", "")),
                       text=str(r.get("text", "")))
        return m
    if topic == "init":
        # A control message, never a state: `st` is ignored on purpose. It is
        # built here so there is ONE builder per topic and a topic can never be
        # published without one.
        return topic_mod.init_msg("cortex")
    raise ValueError("no builder for topic %r" % topic)


#: Every topic the tick publishes. `event` is NOT here: an event is not a state,
#: and republishing yesterday's event every 500ms would be a record pretending to
#: be news. Events go out on occurrence, once.
TICK_TOPICS = ("ripperdoc", "ring", "pir", "sonar", "baro", "weather", "power",
               "fault")


def _publish_all(pub, st=None):
    st = st if st is not None else cortex.get_state()
    # Render the picture BEFORE building, because the face and the ring are
    # fields ON the ripperdoc/ring messages. This is the cortex drawing its own
    # frame from its own state — there is no other source of pixels.
    _render(st)
    for topic in TICK_TOPICS:
        try:
            pub.send(topic, _build(topic, st))
        except Exception as e:                                  # noqa: BLE001
            print("loa-cortex: publish %s failed: %s: %s"
                  % (topic, type(e).__name__, e), file=sys.stderr, flush=True)


def start_publishing(endpoint=None, tick=None):
    """Publish every topic on a fixed tick, and again when something changes.

    TWO triggers, one path. The tick is not decoration: without it a consumer
    that subscribes after the last change is blind, and somebody will "fix" that
    with a /subscribe endpoint returning initial state — which is the endpoint a
    future session will start hammering. The tick removes the need for it.

    Never fatal: if the socket cannot be made, the body keeps running without a
    feed. A missing publisher must not mean a mute body.
    """
    slot = _PUB.setdefault(endpoint or topic_mod.DEFAULT_ENDPOINT,
                           {"sock": None, "stop": None})
    if slot["sock"] is None:
        try:
            slot["sock"] = topic_mod.Publisher(
                **({"endpoint": endpoint} if endpoint else {}))
        except Exception as e:                                  # noqa: BLE001
            print("loa-cortex: no publisher: %s: %s" % (type(e).__name__, e),
                  file=sys.stderr, flush=True)
            return None

    # Publish on CHANGE as well as on the tick, so a command lands on the feed
    # immediately instead of up to TICK_S later.
    def hook(kind, payload):
        if kind == "state":
            return
        if payload.get("kind") == "ring":
            # The ring's one-shots (scan/glitch) are RECORDS, and the cortex is
            # the one that logs them — so the cortex is also the one that plays
            # them. This is where the frame renderer hears them; nothing has to
            # be cleared, because nothing is a flag.
            _TELL["kind"] = (payload.get("detail") or {}).get("state")
            _TELL["ts"] = payload.get("ts") or time.time()
        try:
            slot["sock"].publish_event(payload.get("ts") or 0.0,
                                       payload.get("kind") or "",
                                       payload.get("detail") or {})
        except Exception:                                       # noqa: BLE001
            pass

    off = cortex.on_publish(hook)
    slot["off"] = off

    if slot["stop"] is None:
        stop = threading.Event()
        slot["stop"] = stop
        period = topic_mod.TICK_S if tick is None else tick

        def loop():
            while not stop.is_set():
                t0 = time.time()
                _publish_all(slot["sock"])
                stop.wait(max(0.0, period - (time.time() - t0)))
        threading.Thread(target=loop, daemon=True).start()
    return slot["sock"]


def start_ingesting(endpoint=None):
    """Take readings off the inbound socket and assemble the body's state.

    This is the other half of the cortex being the one publisher. The daemons
    PUSH their readings; this merges them; the merged state goes back out on the
    topics. Without it the feed carried only what the HTTP door changed and
    nothing at all from the senses.

    Merging is by PRESENCE: a reading carries only the fields its daemon
    measured, so a motion event cannot clobber the mood with a zero value.
    """
    rx = topic_mod.Receiver(**({"endpoint": endpoint} if endpoint else {}))
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                for topic, env in rx.drain(250):
                    if topic == "event":
                        whole = topic_mod.message_to_dict(env.event)
                        cortex.log_event(env.event.kind,
                                         whole.get("detail") or {},
                                         ts=env.event.ts)
                        continue
                    if topic in ("ripperdoc", "ring"):
                        # OUTBOUND-ONLY now: the cortex renders these itself. A
                        # daemon that pushed one up would be a second source of
                        # pixels, which is exactly the limb-driven-the-brain
                        # shape this removes. Dropped here on purpose.
                        continue
                    if topic == "init":
                        continue        # its own ask, echoed back; not a state
                    if topic == "baro":
                        b = env.baro
                        fields = {}
                        for f, key in TOPIC_STATE_MAP["baro"].items():
                            if b.HasField(f):
                                fields[key] = getattr(b, f)
                        if fields:
                            cortex.set_state(fields)
                        if b.HasField("pressure_hpa"):
                            cortex.baro_sample(b.pressure_hpa,
                                               b.temp_c if b.HasField("temp_c")
                                               else None,
                                               ts=b.ts or None)
                        continue
                    elif topic == "fault":
                        rows = [{"level": r.level, "code": r.code,
                                 "text": r.text} for r in env.fault.rows]
                        # fault_ts is the record that a sweep was HEARD at all.
                        # Without it the face and the ring read a never-swept
                        # body as well, which is how a hurting body sat calm.
                        cortex.set_state({"faults": rows,
                                          "fault_ts": env.fault.ts or time.time()})
                        if env.fault.HasField("condition"):
                            cortex.set_state({"condition":
                                              env.fault.condition})
                        continue
                    mapping = TOPIC_STATE_MAP.get(topic)
                    if mapping is None:
                        print("loa-cortex: ingest: unknown topic %r" % topic,
                              file=sys.stderr, flush=True)
                        continue
                    msg = getattr(env, topic)
                    fields = {}
                    for f, key in mapping.items():
                        if msg.HasField(f):
                            fields[key] = getattr(msg, f)
                    if fields:
                        cortex.set_state(fields)
            except Exception as e:                              # noqa: BLE001
                if stop.is_set():
                    # Closing the socket is how this loop is stopped. Reporting
                    # that as a failure buries the real ones in noise.
                    break
                # Loud, on stderr (the journal on the body). A silent ingest is
                # a silent feed, and a silent feed is what had Divv shouting at
                # a console that was never on pub/sub.
                print("loa-cortex: ingest failed: %s: %s"
                      % (type(e).__name__, e), file=sys.stderr, flush=True)
                time.sleep(0.5)         # a bad message must not kill the ingest
        rx.close()

    threading.Thread(target=loop, daemon=True).start()
    # The stop handle is returned rather than hidden: a test that cannot stop the
    # ingest leaves a thread holding a store connection into the next test, which
    # is exactly how a suite starts lying.
    return rx, stop.set


def publish_init(pub, asks=6, gap=1.0):
    """Tell every daemon the brain just started and knows nothing.

    THE INIT HANDSHAKE. A restarted cortex has empty RAM: the counters, the
    readings, the fault rows and the vault seal are gone from its head, and the
    daemons are change-only so nothing re-arrives by itself. The daemons cannot
    be ASKED down the inbound leg — that is PUSH/PULL, one direction, and the
    cortex has no way to answer a daemon. So the ask goes OUT on a topic they
    SUBSCRIBE to (`init`), each one re-sends its FULL payload, and everyone
    returns to change-only.

    Published `asks` times because PUB/SUB has no backpressure and a SUB that is
    still reconnecting to a freshly-bound PUB drops what it was not yet
    subscribed for. Six asks over five seconds covers a reconnect; after that
    nothing more is sent, because a repeated init would keep the daemons
    re-sending for no reason.
    """
    for i in range(asks):
        try:
            pub.send("init", _build("init", None))
        except Exception as e:                                  # noqa: BLE001
            print("loa-cortex: init publish failed: %s: %s"
                  % (type(e).__name__, e), file=sys.stderr, flush=True)
        if i + 1 < asks:
            time.sleep(gap)


def main():
    import uvicorn
    host = os.environ.get("LOA_API_BIND", "0.0.0.0")
    port = int(os.environ.get("LOA_API_PORT", DEFAULT_PORT))
    try:
        st = store_mod.from_config()
    except Exception as e:                                      # noqa: BLE001
        print("loa-cortex: store config: %s: %s" % (type(e).__name__, e),
              file=sys.stderr, flush=True)
        st = None
    if st is None:
        print("loa-cortex: no store configured — settings held in RAM, "
              "DB DOWN raised", file=sys.stderr, flush=True)
    cortex.boot(st)
    pub = start_publishing(os.environ.get("LOA_TOPIC_ENDPOINT"))
    if os.environ.get("LOA_INGEST", "on").lower() not in ("0", "false", "no",
                                                          "off"):
        start_ingesting(os.environ.get("LOA_INGEST_ENDPOINT"))
    if pub is not None:
        # In a thread: the init is a courtesy to running daemons, and the door
        # must not wait five seconds for it.
        threading.Thread(target=publish_init, args=(pub,), daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
