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

Security: bind to the tailnet and let ice's firewall be the gate. No auth
here; the network is the boundary.
"""

import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from . import cortex
from . import expressions as expr
from . import moods

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
    mode: str = Field(..., description="scope|ecg|ripple|noise|text|off")
    text: str | None = None
    dim: bool | None = None


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
    # REMOTE weather board isn't wired yet. Honest about it — no fake data.
    return {"available": False, "note": "REMOTE weather board not wired yet"}


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
        "system": _system_state(),
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
    if req.mode not in ("scope", "ecg", "ripple", "noise", "text", "off"):
        raise HTTPException(
            400, "mode must be scope|ecg|ripple|noise|text|off")
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


# ---------------------------------------------------------------------------
# entry point

def main():
    import uvicorn
    host = os.environ.get("LOA_API_BIND", "0.0.0.0")
    port = int(os.environ.get("LOA_API_PORT", DEFAULT_PORT))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()