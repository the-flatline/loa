"""bridge — the topic, in the browser's dialect.

A browser cannot open a ZMQ socket. That is the ONLY reason this exists.

It is not a consumer. It does not decode, translate or repackage anything: it
takes the bytes the cortex published and forwards them down a WebSocket. The
PAGE is the subscriber, and it decodes with the same .proto the body encodes
with.

The test of a shim is that it understands nothing. The relay this replaces
hand-mapped every field into JSON — a second schema, in a second process, free
to drift from the first. That is the bug this removes, and it is the same bug as
the hex-as-base64 frame, one level up.

A page built for a different schema is refused at the door (close code 1008)
rather than handed bytes it will misread.
"""
import asyncio
import sys

import websockets

from . import topic

WS_HOST = "127.0.0.1"
WS_PORT = 8766

_clients = set()


async def _handler(ws):
    """Accept a page only if it speaks this schema."""
    request = getattr(ws, "request", None)
    path = getattr(request, "path", "") or ""
    if "v=%d" % topic.SCHEMA_VERSION not in path:
        await ws.close(code=1008,
                       reason="schema v%d required" % topic.SCHEMA_VERSION)
        return
    _clients.add(ws)
    try:
        await ws.wait_closed()
    finally:
        _clients.discard(ws)


async def _fanout(payload):
    for ws in list(_clients):
        try:
            await ws.send(payload)
        except Exception:                                       # noqa: BLE001
            _clients.discard(ws)


async def _pump():
    """Forward the topic, byte for byte. No decoding, ever."""
    sub = topic.Subscriber()
    loop = asyncio.get_running_loop()
    while True:
        env = await loop.run_in_executor(None, sub.recv, 250)
        if env is None:
            continue
        await _fanout(env.SerializeToString())


async def _serve():
    async with websockets.serve(_handler, WS_HOST, WS_PORT, max_size=None):
        await _pump()


def main():
    print("loa-bridge: ws://%s:%d  schema v%d  — forwarding raw protobuf"
          % (WS_HOST, WS_PORT, topic.SCHEMA_VERSION), flush=True)
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    except Exception as e:                                      # noqa: BLE001
        print("loa-bridge: %s: %s" % (type(e).__name__, e),
              file=sys.stderr, flush=True)
        raise
