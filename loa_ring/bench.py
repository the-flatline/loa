"""bench — the bench-side CLI for the ripperdoc (loa-bench).

Divv types `loa-bench` at the shell and expects the bench mode on the face.
This is that command: a thin client over the loa API's /ripperdoc endpoint,
so the bench session doesn't need curl gymnastics.

    loa-bench on [page]    — flip ripperdoc on (optional page: sensors|pir|snr)
    loa-bench off          — back to scope
    loa-bench page <page>  — switch the ripperdoc page
    loa-bench status       — what the face is doing now

Works from anywhere the API is reachable; defaults to localhost.
"""

import json
import os
import sys
import urllib.request

DEFAULT_PORT = 8765


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


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        print(__doc__.strip().splitlines()[-6:][0] if False else __doc__)
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
            st = _get("/state")["ripperdoc"]
            page = _get("/state")["ripperdoc_page"]
            mode = _get("/state")["oled"]["mode"]
            print(f"ripperdoc {'ON' if st else 'OFF'} · page {page} · oled {mode}")
        else:
            print(f"unknown command: {cmd}")
            return 2
    except Exception as e:
        print(f"loa-bench: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())