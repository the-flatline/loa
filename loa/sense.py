"""sense — the senses' plumbing. THE FRAMEWORK, not the drivers.

What a sense daemon needs to be a sense: the wire (publish / publish_event), the
topic it claims, and the init handshake that re-sends a full payload to a
restarted cortex. Nothing here knows what a PIR or a DHT is.

Each DRIVER lives in the subsystem folder that owns it, so the folder is the
whole sense and no other subsystem has a reason to import into it:

  motion/   __main__ (the daemon) + pir.py        the PIR on GPIO17
  sonar/    __main__ (the daemon) + ultrasonic.py the range, TRIG 23 / ECHO 22
  weather/  __main__ (the daemon) + dht.py
                                   + baro.py       the remote board + I2C 0x77

They were all in this module (687 lines: PIR, sonar, DHT, BMP180 and the wire
in one file). The process split came first — one daemon per sense, because the
DHT's blocking pulse-collection once sat on the same loop as the PIR poll and
the PIR logged nothing for two hours while it was working fine. This is the
other half of that split: a driver that shares a module with three other
sensors shares a module with their failures, and a driver in another
subsystem's folder is a driver that reaches across the tree. A test
(tests/test_split_daemons.py) asserts this module no longer answers for any
driver and that each daemon reaches only its own.

The topic name the daemon is the SOURCE of — "pir", "sonar", "weather",
"baro" — is claimed once at startup by the daemon in its own folder, not here.
"""

import threading
import time

from .cortex import state as cortex

# -- the wire --------------------------------------------------------------- #
# A reading is PUBLISHED, not written. The daemons are separate processes that
# once wrote sqlite directly — which meant the cortex's publisher could not see
# their changes, and the feed carried nothing from the senses at all. Each
# daemon now claims the topic at startup and its readings go out on it.
#
# The drivers stay ignorant of the wire: they call publish(), and how it leaves
# is whoever claimed it.
_SENDERS = {}
_TOPIC = None


def sender_for(topic, endpoint=None):
    """One Sender per topic, made on first use and kept for the process."""
    sock = _SENDERS.get(topic)
    if sock is None:
        from .topic import Sender
        sock = _SENDERS[topic] = Sender(
            **({"endpoint": endpoint} if endpoint else {}))
    return sock


def use_topic(topic, endpoint=None):
    """Claim a topic. This daemon's readings go out on it from now on.

    Called once at daemon startup with the topic this daemon is the source of —
    "pir", "sonar", "weather", "baro". A daemon owns a topic the way it owns a
    sensor: one source, and the reading goes out under the name the ingest reads.

    Returns the Sender, so a daemon that publishes more than readings (the face,
    the ring) can hold on to it.
    """
    global _TOPIC
    _TOPIC = topic
    return sender_for(topic, endpoint)


def publish(fields, topic=None):
    """A reading. On its topic when a daemon has claimed one, else the database.

    `topic` is for a process that hosts more than one source — loa-weather
    carries the weather board AND the baro, and each reading should arrive under
    its own name. The driver knows what it measured, so the driver says.

    The database fallback is not dead code: the bench runs these drivers directly
    in one process with no topic, and that must keep working."""
    where = topic or _TOPIC
    if where is None:
        return cortex.set_state(fields)
    from .topic import partial
    _LAST.setdefault(where, {}).update(fields)
    sender_for(where).send(where, partial(where, fields))


#: What each topic last carried. A daemon cannot know its own full payload from
#: the wire, so it remembers what it published — that memory is what the init
#: handshake re-sends.
_LAST: dict = {}


def republish_full():
    """Re-send every topic's accumulated fields — the answer to an init.

    The cortex restarted with empty RAM and ASKED; a sensor answers with its
    whole current state, not a change. Grouped by topic because loa-weather
    hosts two sources under two names (weather and baro).
    """
    for where, fields in list(_LAST.items()):
        if not fields:
            continue
        try:
            if _TOPIC is None:
                cortex.set_state(fields)
                continue
            from .topic import partial
            sender_for(where).send(where, partial(where, fields))
        except Exception:                                       # noqa: BLE001
            pass


#: The init listener's stop handle.
_INIT_STOP = threading.Event()


def subscribe_init(on_ask=None):
    """Hear the cortex's init and answer it with a full payload.

    The daemons PUSH up and the cortex PULLs, so the cortex cannot ask anything
    down that leg — that is what "ZMQ is directional" means here. The ask
    travels the OTHER way, out on the `init` topic, so a daemon that expects to
    be asked holds a SUB socket as well as its PUSH one.

    Without it, a restarted cortex is blind to every change-only sensor until
    something happens to move: it knows nothing about the body it just became.
    """
    from .topic import Subscriber
    ask = on_ask or republish_full
    _INIT_STOP.clear()

    def loop():
        try:
            sub = Subscriber(topics=["init"])
        except Exception:                                       # noqa: BLE001
            return
        try:
            while not _INIT_STOP.is_set():
                try:
                    got = sub.recv(500)
                except Exception:                               # noqa: BLE001
                    # Shutting down: the context is going away under the socket.
                    # A pending process must not log a traceback as it exits.
                    return
                if got is None:
                    continue
                try:
                    ask()
                except Exception:                               # noqa: BLE001
                    pass
        finally:
            try:
                sub.close()
            except Exception:                                   # noqa: BLE001
                pass

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def stop_init():
    """Stop the init listener (tests; a daemon runs until it is killed)."""
    _INIT_STOP.set()


def publish_event(kind, detail=None, ts=None):
    """A record. Always the `event` topic — see topic.event()."""
    if _TOPIC is None:
        return cortex.log_event(kind, detail, ts=ts)
    from .topic import event as _event
    sender_for("event").send("event",
                             _event(time.time() if ts is None else ts,
                                    kind, detail))
