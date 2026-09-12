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
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from . import cortex
from . import fault
from . import expressions as expr
from . import fragment as fragment_mod
from . import moods
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
    mode: str = Field(..., description="scope|ecg|ripple|noise|text|showoff|ripperdoc|off")
    text: str | None = None
    dim: bool | None = None


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
        "ring": {
            "state": st["ring_state"],
            "pending_event": st["pending_event"],
        },
        "oled": {
            "mode": st["oled_mode"],
            "text": st["oled_text"],
            "dim": st["oled_dim"],
        },
        "sensors": _sensors_state(),
        "ripperdoc": st["ripperdoc"],
        "ripperdoc_page": st["ripperdoc_page"],
        "sense": {
            "pir_high": st["pir_high"],
            "count": st["sense_count"],
            "last_ts": st["sense_ts"],
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
    return {"ok": True, "ring": req.state,
            "state": st["ring_state"], "pending_event": st["pending_event"]}


@app.post("/display")
def display(req: DisplayRequest):
    if req.mode not in ("scope", "ecg", "ripple", "noise", "text", "showoff",
                        "ripperdoc", "off"):
        raise HTTPException(
            400, "mode must be scope|ecg|ripple|noise|text|showoff|ripperdoc|off")
    cortex.set_state({
        "oled_mode": req.mode,
        "oled_text": req.text if req.mode == "text" else None,
        "oled_dim": bool(req.dim),
    })
    cortex.log_event("display", {"mode": req.mode, "text": req.text,
                                 "dim": req.dim})
    st = cortex.get_state()
    return {"ok": True, "oled": {"mode": st["oled_mode"],
                                 "text": st["oled_text"],
                                 "dim": st["oled_dim"]}}


@app.post("/ripperdoc")
def ripperdoc(req: RipperdocRequest):
    fields = {}
    if req.page is not None:
        if req.page not in face.Ripperdoc.PAGES:
            raise HTTPException(400, f"page must be {'|'.join(face.Ripperdoc.PAGES)}")
        fields["ripperdoc_page"] = req.page
    if req.on is not None:
        fields["ripperdoc"] = 1 if req.on else 0
        fields["oled_mode"] = "ripperdoc" if req.on else "scope"
    if fields:
        cortex.set_state(fields)
        if "ripperdoc" in fields:
            cortex.log_event("ripperdoc", {"on": req.on,
                                           "page": fields.get("ripperdoc_page",
                                                              cortex.get_state()["ripperdoc_page"])})
    st = cortex.get_state()
    return {"ok": True, "ripperdoc": st["ripperdoc"],
            "page": st["ripperdoc_page"], "oled": st["oled_mode"]}


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

# -- the topic: loa-cortex is the ONE publisher ------------------------------ #
# Keyed by endpoint: a module-level single slot meant the first endpoint bind
# won forever, so a second start_publishing() silently published into a socket
# nobody was listening on. Found by the tests, not by the body.
_PUB = {}


def _attach_body_readouts(msg):
    """Add the readings that exist only on the body: PMIC, fault sweep, seal.

    Attached at PUBLISH time rather than stored, because they are hardware and
    they change on their own schedule — and their change is exactly what makes
    the publish-on-change comparison notice a rail moving.

    Each block is independently guarded: a missing sensor or an unreadable vault
    must not stop the state going out. Say less, never go silent.
    """
    try:
        for k, v in (face.power_status() or {}).items():
            if isinstance(v, (int, float)):
                msg.power[str(k)] = float(v)
    except Exception:                                           # noqa: BLE001
        pass
    try:
        for row in (fault.status() or {}).get("rows") or []:
            msg.faults.append("%s|%s" % (row.get("level", ""),
                                         row.get("face") or row.get("code") or ""))
    except Exception:                                           # noqa: BLE001
        pass
    try:
        msg.condition = fault.condition()
    except Exception:                                           # noqa: BLE001
        pass
    try:
        for k, v in (face.seal_state() or {}).items():
            msg.frag[str(k)] = str(v)
    except Exception:                                           # noqa: BLE001
        pass
    try:
        msg.baro_trend = cortex.baro_trend()
        msg.baro_series.extend(_baro_series())
    except Exception:                                           # noqa: BLE001
        pass
    return msg


def start_publishing(endpoint=None):
    """Publish state changes and events to the topic. Called once at startup.

    Registered on the state module, which knows nothing about ZMQ: it reports
    what changed, this decides what to do about it.

    Publish on CHANGE — the schema decides what "changed" means, by comparing
    the serialised message. A state that has not moved is not news, and a feed
    of non-news is a feed nobody reads.

    Never fatal: if the socket cannot be made, the body keeps running without a
    feed. A missing publisher must not mean a mute body.
    """
    from . import topic as topic_mod

    slot = _PUB.setdefault(endpoint or topic_mod.DEFAULT_ENDPOINT,
                           {"sock": None, "last": None})

    def hook(kind, payload):
        if slot["sock"] is None:
            try:
                slot["sock"] = topic_mod.Publisher(
                    **({"endpoint": endpoint} if endpoint else {}))
            except Exception:                                   # noqa: BLE001
                return
        pub = slot["sock"]
        try:
            if kind == "state":
                msg = _attach_body_readouts(topic_mod.state_to_message(payload))
                raw = msg.SerializeToString()
                if raw == slot["last"]:
                    return
                slot["last"] = raw
                env = topic_mod._envelope()
                env.state.CopyFrom(msg)
                pub.send(env)
            else:
                pub.publish_event(payload.get("ts") or 0.0,
                                  payload.get("kind") or "",
                                  payload.get("detail") or {})
        except Exception:                                       # noqa: BLE001
            pass

    return cortex.on_publish(hook)


def start_ingesting(endpoint=None):
    """Take readings off the topic and assemble the body's state.

    This is the other half of the cortex being the one publisher. The daemons
    PUBLISH their readings; this merges them; the merged state goes back out on
    the topic. Without it the feed carried only what the HTTP door changed and
    nothing at all from the senses.

    Merging is by PRESENCE: a reading carries only the fields its daemon
    measured, so a motion event cannot clobber the mood with a zero value.
    """
    from . import topic as topic_mod
    import threading

    rx = topic_mod.Receiver(**({"endpoint": endpoint} if endpoint else {}))
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                for env in rx.drain(250):
                    which = env.WhichOneof("body")
                    if which == "state":
                        fields = topic_mod.state_fields_present(env.state)
                        if fields:
                            cortex.set_state(fields)   # republished by the hook
                    elif which == "event":
                        whole = topic_mod.event_to_dict(env.event)
                        cortex.log_event(env.event.kind,
                                         whole.get("detail") or {},
                                         ts=env.event.ts)
            except Exception as e:                              # noqa: BLE001
                # Loud, once per failure, on stderr (the journal on the body).
                # A silent ingest is a silent feed, and a silent feed is what
                # had Divv shouting at a console that was never on pub/sub.
                import sys
                print("loa-cortex: ingest failed: %s: %s"
                      % (type(e).__name__, e), file=sys.stderr, flush=True)
                time.sleep(0.5)         # a bad message must not kill the ingest
        rx.close()

    threading.Thread(target=loop, daemon=True).start()
    # The stop handle is returned rather than hidden: a test that cannot stop
    # the ingest leaves a thread holding a database connection into the next
    # test, which is exactly how a suite starts lying.
    return rx, stop.set


def main():
    import uvicorn
    host = os.environ.get("LOA_API_BIND", "0.0.0.0")
    port = int(os.environ.get("LOA_API_PORT", DEFAULT_PORT))
    start_publishing(os.environ.get("LOA_TOPIC_ENDPOINT"))
    if os.environ.get("LOA_INGEST", "on").lower() not in ("0", "false", "no", "off"):
        start_ingesting(os.environ.get("LOA_INGEST_ENDPOINT"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()