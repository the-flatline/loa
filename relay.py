#!/usr/bin/env python3
"""loa relay — polls the loa frame bus API and drops twin.json in the caddy tree.

The browser never talks to loa; this process is the web consumer of the bus.
Runs on dixie. uv-managed (uv run --project /home/flatline/dev/loa-relay relay.py).
"""
import json
import time
import urllib.request

LOA_TWIN = "http://loa.zendient.com:8765/twin"
OUT = "/home/flatline/caddy/www/loa/twin.json"
POLL_S = 0.25


def fetch():
    try:
        with urllib.request.urlopen(LOA_TWIN, timeout=1.5) as r:
            return json.load(r)
    except Exception:
        return None


def main():
    last_ok = 0.0
    while True:
        t = fetch()
        now = time.time()
        if t is not None:
            t["relayed"] = now
            last_ok = now
            tmp = OUT + ".tmp"
            with open(tmp, "w") as f:
                json.dump(t, f)
            import os
            os.replace(tmp, OUT)
        elif now - last_ok > 15:
            # mark the body offline so the page shows it, keep last frames
            try:
                with open(OUT, "r") as f:
                    t = json.load(f)
                t["relayed"] = now
                t["offline"] = True
                with open(OUT + ".tmp", "w") as f:
                    json.dump(t, f)
                os.replace(OUT + ".tmp", OUT)
            except Exception:
                pass
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()