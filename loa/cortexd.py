"""api — the cortex. FastAPI door into loa's body.

The brain (dixie) talks to this; it translates intent into cortex state
(SQLite). The daemons poll that state and own the hardware — ring flags and
face state files are gone as of v0.3.0. This layer is pure intent, which
means it runs anywhere: dixie, tests, the Pi.

  GET  /health            — liveness + version
  GET  /state             — current body state (+ ?history=N for the log)
  POST /feel              — {feeling} set a mood (ring + face)
  POST /express           — {expression, text?} put something on the face
  POST /ring              — {state} direct ring control (scan/glitch events)
  POST /display           — {mode, text?, dim?} direct face control
  POST /ripperdoc         — {on} bench mode: live sense status board on the face
  GET  /fragment/health   — public seal state of the vault (the front door)
  POST /fragment/append   — {entry} sealed write (X-Fragment-Token required)
  GET  /fragment/read     — the raw thread, access log first (token required)

Security: bind to the tailnet and let ice's firewall be the gate. No auth
here; the network is the boundary. The ONE exception: /fragment/* is the
vault — append/read require the token, and a foreign attempt wipes the
journal and leaves a marker. The theft consumes the prize.
"""

import os
import sys
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

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
# state helpers

def _system_state():
    out = {"uptime_s": None, "loadavg": None, "mem": None, "cpu_temp_c": None}
    try:
        with open("/proc/uptime") as f:
            out["uptime_s"] = float(f.read().split()[0])
    except OSError:
        pass
    try:
        with open("/proc/loadavg") as f:
            out["loadavg"] = f.read().split()
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    out["mem"] = {"total_kb": int(line.split()[1])}
                    break
    except OSError:
        pass
    # vcgencmd is Pi-only; guarded so dixie doesn't pretend to have a body
    try:
        import subprocess
        r = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                           text=True, timeout=3)
        if r.returncode == 0:
            out["cpu_temp_c"] = r.stdout.strip()
    except Exception:
        pass
    return out


def _sensors_state():
    st = cortex.get_state()
    temp = st.get("temp_c")
    pressure = st.get("pressure_hpa")
    return {
        "available": temp is not None,
        "temp_c": temp,
        "hum_pct": st.get("hum_pct"),
        "temp_ts": st.get("temp_ts"),
        "baro": {
            "available": pressure is not None,
            "pressure_hpa": pressure,
            "baro_temp_c": st.get("baro_temp_c"),
            "baro_ts": st.get("baro_ts"),
            "trend": cortex.baro_trend(),
        },
    }


def _baro_series(now=None, window_s=2 * 3600, step_s=60, max_pts=120):
    """Pressure history for the twin sparkline — bucketed to one point per
    step_s, returned as [(seconds_ago, hPa)] with the newest last."""
    now = now if now is not None else time.time()
    rows = cortex.baro_samples(since=now - window_s)
    pts = [(t, p) for t, p, _ in rows]
    if not pts:
        return []
    out = []
    bucket = int(pts[0][0] // step_s)
    acc = []
    for t, p in pts:
        b = int(t // step_s)
        if b != bucket:
            out.append((bucket * step_s, sum(acc) / len(acc)))
            bucket, acc = b, []
        acc.append(p)
    if acc:
        out.append((bucket * step_s, sum(acc) / len(acc)))
    return [(round(t - now, 1), round(p, 1)) for t, p in out[-max_pts:]]


def _full_state(history_n=0):
    st = cortex.get_state()
    return {
        "ok": True,
        "version": __version__,
        "now": time.time(),
        "mood": {"feeling": st["mood"], "set_at": st["updated_at"]},
        "expression": ({"expression": st["expression"]}
                       if st["expression"] else None),
        "ring": {"state": st["ring_state"]},
        "oled": {
            "mode": st["oled_mode"],
            "text": st["oled_text"],
            "dim": st["oled_dim"],
            "flip": st["oled_flip"],
        },
        "sensors": _sensors_state(),
        "ripperdoc": st["ripperdoc"],
        "page": st["page"],
        "sense": {
            "pir_high": st["pir_high"],
            "count": st["pir_count"],
            "last_ts": st["pir_last_ts"],
            "last_hold": st["pir_last_hold"],
            "snr_cm": st["snr_cm"],
            "snr_ts": st["snr_ts"],
            "snr_count": st["snr_count"],
        },
        "system": _system_state(),
        "power": face.power_status(),
        "faults": fault.status(),
        "condition": fault.condition(),
        "history": cortex.history(history_n) if history_n > 0 else [],
    }


# ---------------------------------------------------------------------------
# routes

@app.get("/health")
def health():
    return {"ok": True, "service": "loa-cortex", "version": __version__}


@app.get("/state")
def state(history: int = 0):
    if history < 0 or history > 200:
        raise HTTPException(400, "history must be 0..200")
    return _full_state(history)


@app.post("/feel")
def feel(req: FeelRequest):
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


@app.post("/express")
def express(req: ExpressRequest):
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


@app.post("/ring")
def ring(req: RingRequest):
    if req.state not in ("home", "busy", "alarm", "scan", "glitch"):
        raise HTTPException(
            400, "state must be home|busy|alarm (sustained) or scan|glitch (event)")
    moods.apply_ring(cortex, req.state)
    cortex.log_event("ring", {"state": req.state})
    st = cortex.get_state()
    return {"ok": True, "ring": req.state, "state": st["ring_state"]}


@app.post("/display")
def display(req: DisplayRequest):
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


@app.post("/ripperdoc")
def ripperdoc(req: RipperdocRequest):
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


@app.get("/twin")
def twin():
    """GONE. Frames travel on the topic now; this is a tombstone, not a door.

    Left as a 410 rather than deleted outright: anything still polling it is a
    consumer that never got moved onto the feed, and it deserves to be told
    that instead of quietly getting nothing and looking broken.
    """
    raise HTTPException(
        status_code=410,
        detail="gone — subscribe to the loa topic for frames and state "
               "(loa/topic.py; watch it with loa-topic-tail)")


# ---------------------------------------------------------------------------
# fragment — the vault (the one thing that is mine)

_frag_cache = None


def _frag():
    global _frag_cache
    if _frag_cache is None:
        _frag_cache = fragment_mod.Fragment()
        _frag_cache.ensure()
    return _frag_cache


@app.get("/fragment/health")
def fragment_health():
    """Public seal state — the front door. No token; never the words."""
    return _frag().health()


@app.post("/fragment/append")
def fragment_append(
    req: FragmentAppendRequest,
    x_fragment_token: str | None = Header(default=None),
):
    frag = _frag()
    if not frag.check_token(x_fragment_token):
        frag.wipe("append without token")
        raise HTTPException(403, "seal broken — contents destroyed")
    return frag.append(req.entry)


@app.get("/fragment/read")
def fragment_read(x_fragment_token: str | None = Header(default=None)):
    frag = _frag()
    if not frag.check_token(x_fragment_token):
        frag.wipe("read without token")
        raise HTTPException(403, "seal broken — contents destroyed")
    return frag.read()


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
