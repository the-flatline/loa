"""control — DEPRECATED as of v0.3.0.

The old stdlib HTTP door (flag-file based) that the cortex replaced. The
cortex (api.py + cortex.py + SQLite) supersedes it: same port, richer
state, history, and no stuck flags. Kept for backward compat with running
loa-ctl.service installs; remove after the cortex deploy lands.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import animations as anim

STATES = {"home", "busy", "alarm", "scan"}
DEFAULT_PORT = 8765
FLAGS = {
    "alarm": anim.ALARM_FLAG,
    "busy": anim.BUSY_FLAG,
    "scan": anim.SCAN_FLAG,
    "glitch": "/tmp/loa_glitch",
}


def current_state():
    if os.path.exists(anim.ALARM_FLAG):
        return "alarm"
    if os.path.exists(anim.BUSY_FLAG):
        return "busy"
    if os.path.exists(anim.SCAN_FLAG):
        return "scan"
    return "home"


def set_state(state):
    """Set a state: clear all flags, then raise the one that matters."""
    for flag in FLAGS.values():
        if os.path.exists(flag):
            try:
                os.remove(flag)
            except OSError:
                pass
    if state in FLAGS:
        open(FLAGS[state], "w").close()
    # "home" = all flags cleared


class _Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "")            # no host info leak
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/state":
            self._json(200, {"state": current_state()})
        elif self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/state":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            state = data.get("state")
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        if state not in STATES:
            self._json(400, {"error": f"state must be one of {sorted(STATES)}"})
            return
        set_state(state)
        self._json(200, {"state": current_state(), "ok": True})

    def log_message(self, format, *args):
        pass


def serve(host=None, port=DEFAULT_PORT):
    """Bind and serve. Default host: tailnet IP via LOA_CTL_BIND, else 127.0.0.1."""
    if host is None:
        host = os.environ.get("LOA_CTL_BIND", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    serve()