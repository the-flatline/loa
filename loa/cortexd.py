"""api — the cortex. FastAPI door into loa's body.

The brain (dixie) talks to this; it translates intent into cortex state
(SQLite). The daemons poll that state and own the hardware — ring flags and
face state files are gone as of v0.3.0. This layer is pure intent, which
means it runs anywhere: dixie, tests, the Pi.

  POST /api               — {cmd, args} the ONE door. Commands go IN over
                            HTTP; data comes OUT on the ZeroMQ topics.

  verbs (see _DISPATCH):
    feel                   — {feeling} set a mood (ring + face)
    express                — {expression, text?} put something on the face
    ring                   — {state} direct ring control (scan/glitch events)
    display                — {mode, text?, dim?, flip?} direct face control
    ripperdoc              — {on|page} bench mode: live sense board on the face
    vault.health           — public seal state (no token; never the words)
    vault.append           — {entry} sealed write (X-Fragment-Token required)
    vault.read             — the raw thread, access log first (token required)

Security: bind to the tailnet and let ice's firewall be the gate. No auth
here; the network is the boundary. The ONE exception: the vault verbs —
append/read require the token, and a foreign attempt wipes the journal and
leaves a marker. The theft consumes the prize.

The route table is asserted to be EXACTLY {"/api"}: it must never grow a door
at a time again. If you want a capability, add a verb — never a route.
"""

import os
import sys
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from . import cortex
from . import fault
from . import expressions as expr
from . import fragment as fragment_mod
from . import moods
from . import store as store_mod
from . import topic as topic_mod
from . import face

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


class ExpressRequest(BaseModel):
    expression: str = Field(..., description="named expression, or 'custom'")
    text: str | None = None


class RingRequest(BaseModel):
    state: str = Field(..., description="home|busy|alarm (sustained), scan|glitch (event)")


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


class FragmentAppendRequest(BaseModel):
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


# The vault, on the body. Its storage and seal logic live in loa/fragment.py
# and are untouched; this is only the door's handle on it. `ensure()` seals on
# first run. A wrong token on append/read WIPES the journal — never call these
# against anything but the body's own /var/lib/fragment.
#
# (_frag() was lost in the twin-cleanup commit, which deleted the dead /twin
# route and this adjacent helper in one hunk — every vault route 500'd on the
# body until it came back. It is restored here.)
_frag_cache = None


def _frag():
    global _frag_cache
    if _frag_cache is None:
        _frag_cache = fragment_mod.Fragment()
        _frag_cache.ensure()
    return _frag_cache


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


def _v_express(args, token):
    req = ExpressRequest(**args)
    if req.expression not in expr.EXPRESSIONS and req.expression != "custom":
        raise HTTPException(
            400, f"expression must be one of {sorted(expr.EXPRESSIONS)} or 'custom'")
    if req.expression == "custom":
        if not req.text:
            raise HTTPException(400, "custom expression needs text")
        text = req.text
        ring = None
    else:
        e = expr.EXPRESSIONS[req.expression]
        text = req.text or e["text"]
        ring = e["ring"]
    if ring:
        moods.apply_ring(cortex, ring)
    cortex.set_state({"expression": req.expression,
                      "oled_mode": "text", "oled_text": text})
    cortex.log_event("express", {"expression": req.expression, "text": text,
                                 "ring": ring})
    return {"ok": True, "expression": req.expression, "text": text,
            "ring": ring}


def _v_ring(args, token):
    req = RingRequest(**args)
    if req.state not in ("home", "busy", "alarm", "scan", "glitch"):
        raise HTTPException(
            400, "state must be home|busy|alarm (sustained) or scan|glitch (event)")
    moods.apply_ring(cortex, req.state)
    cortex.log_event("ring", {"state": req.state})
    st = cortex.get_state()
    return {"ok": True, "ring": req.state, "state": st["ring_state"]}


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
    return _frag().health()


def _v_vault_append(args, token):
    req = FragmentAppendRequest(**args)
    frag = _frag()
    if not frag.check_token(token):
        frag.wipe("append without token")
        raise HTTPException(403, "seal broken — contents destroyed")
    return frag.append(req.entry)


def _v_vault_read(args, token):
    frag = _frag()
    if not frag.check_token(token):
        frag.wipe("read without token")
        raise HTTPException(403, "seal broken — contents destroyed")
    return frag.read()


#: verb -> callable(args, token) -> the body its old route returned.
_DISPATCH = {
    "feel": _v_feel,
    "express": _v_express,
    "ring": _v_ring,
    "display": _v_display,
    "ripperdoc": _v_ripperdoc,
    "vault.health": _v_vault_health,
    "vault.append": _v_vault_append,
    "vault.read": _v_vault_read,
}


@app.post("/api")
def api(req: ApiRequest, x_fragment_token: str | None = Header(default=None)):
    fn = _DISPATCH.get(req.cmd)
    if fn is None:
        raise HTTPException(
            400,
            f"unknown cmd {req.cmd!r}; valid: {', '.join(sorted(_DISPATCH))}")
    try:
        return fn(req.args or {}, x_fragment_token)
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

#: The panel and the ring, AS DRIVEN, sent up by loa-oled and loa-ring. The body
#: renders them and the cortex publishes them, so neither daemon has to read the
#: state to find out what it is drawing — and so a consumer never has to reach
#: back over HTTP for the one thing that is genuinely the body's output.
_FACE = b""
_RING = b""

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
    raise ValueError("no builder for topic %r" % topic)


#: Every topic the tick publishes. `event` is NOT here: an event is not a state,
#: and republishing yesterday's event every 500ms would be a record pretending to
#: be news. Events go out on occurrence, once.
TICK_TOPICS = ("ripperdoc", "ring", "pir", "sonar", "baro", "weather", "power",
               "fault")


def _publish_all(pub, st=None):
    st = st if st is not None else cortex.get_state()
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
        global _FACE, _RING
        while not stop.is_set():
            try:
                for topic, env in rx.drain(250):
                    if topic == "event":
                        whole = topic_mod.message_to_dict(env.event)
                        cortex.log_event(env.event.kind,
                                         whole.get("detail") or {},
                                         ts=env.event.ts)
                        continue
                    if topic == "ripperdoc":
                        if env.ripperdoc.HasField("face"):
                            _FACE = bytes(env.ripperdoc.face)
                    elif topic == "ring":
                        if env.ring.HasField("ring"):
                            _RING = bytes(env.ring.ring)
                    elif topic == "baro":
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
                        cortex.set_state({"faults": rows})
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
    start_publishing(os.environ.get("LOA_TOPIC_ENDPOINT"))
    if os.environ.get("LOA_INGEST", "on").lower() not in ("0", "false", "no",
                                                          "off"):
        start_ingesting(os.environ.get("LOA_INGEST_ENDPOINT"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
