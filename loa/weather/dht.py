"""dht — the DHT11-class temp/humidity driver (XC4520) on GPIO4.

The weather daemon's environment board, in the weather daemon's folder. Start
signal (20ms low), then the sensor pulls the line for a response pair and 40
data bits. The gpiod re-request between the start and the sampling window can
miss the response pair, so pulses are collected and every alignment is tried
until the frame checksum validates — a valid frame proves itself. Every wait is
deadline-guarded: a stuck or disconnected line returns None instead of hanging
the daemon.

Reads are PUBLISHED on the `weather` topic as temp_c / hum_pct / temp_ts /
temp_count so the ripperdoc board and the live can show the room. Nothing here
touches the cortex: the driver holds its own count.
"""

import sys
import threading
import time

from ..sense import publish

DEFAULT_TEMP_GPIO = 4       # REMOTE weather board DATA (XC4520, DHT11-class)
DEFAULT_TEMP_PERIOD = 10.0
DHT_PULSE_WINDOW = 0.02     # 20ms to collect pulses; stuck line must not hang
DHT_MAX_PULSES = 60
DHT_ONE_US = 60000          # high pulse longer than 60us = bit 1 — this
                            # clone's '0' drifts to ~47us, '1' starts at 70us
#: The box's gpiochips, in order — the DATA line lives on one of these, and
#: which one is a property of the board, so the driver carries its own list.
GPIO_CHIPS = ("/dev/gpiochip0", "/dev/gpiochip4")


class DHT11:
    """DHT11-class temp/humidity on one GPIO (the REMOTE weather board).

    Start signal (20ms low), then the sensor pulls the line for a response
    pair and 40 data bits. The gpiod re-request between the start and the
    sampling window can miss the response pair, so pulses are collected and
    every alignment is tried until the frame checksum validates — a valid
    frame proves itself. Every wait is deadline-guarded: a stuck or
    disconnected line returns None instead of hanging the daemon.

    Reads land in cortex as temp_c / hum_pct / temp_ts / temp_count so the
    ripperdoc board and the live can show the room.
    """

    def __init__(self, gpio=DEFAULT_TEMP_GPIO, period=DEFAULT_TEMP_PERIOD,
                 chip=None):
        self.gpio = gpio
        self.period = period
        self.chip = chip
        self._stop = threading.Event()
        self._fails = 0
        self._count = 0
        self._last_pulses = []

    def _collect(self):
        try:
            import gpiod
            from gpiod.line import Direction, Edge, Value
        except ImportError:
            return None
        chips = [self.chip] if self.chip else list(GPIO_CHIPS)
        # start signal: drive the line low for 20ms
        req = None
        for path in chips:
            try:
                req = gpiod.request_lines(
                    path, consumer="loa-dht",
                    config={self.gpio: gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=Value.INACTIVE)})
                break
            except OSError:
                continue
        if req is None:
            return None
        req.set_value(self.gpio, Value.INACTIVE)
        time.sleep(0.02)
        req.release()
        # sample as input with kernel edge detection — timestamps come from
        # the kernel, immune to GIL jitter from the PIR poller's subprocesses
        req = None
        for path in chips:
            try:
                req = gpiod.request_lines(
                    path, consumer="loa-dht",
                    config={self.gpio: gpiod.LineSettings(
                        direction=Direction.INPUT,
                        edge_detection=Edge.BOTH)})
                break
            except OSError:
                continue
        if req is None:
            return None
        import select
        edges = []
        deadline = time.monotonic() + DHT_PULSE_WINDOW
        while time.monotonic() < deadline and len(edges) < DHT_MAX_PULSES * 2:
            r, _, _ = select.select([req.fd], [], [], 0.005)
            if not r:
                continue
            for ev in req.read_edge_events():
                kind = getattr(ev, "event_type", getattr(ev, "type", None))
                ts = getattr(ev, "timestamp_ns", getattr(ev, "timestamp", None))
                rising = None
                if kind is not None:
                    try:
                        rising = int(kind) == 1  # GPIO_V2_LINE_EVENT_RISING
                    except (TypeError, ValueError):
                        rising = "RISING" in str(kind)
                edges.append((rising, ts))
        req.release()
        # high durations from rising->falling pairs; stray edges are skipped
        highs = []
        i = 0
        while i < len(edges) - 1:
            if edges[i][0]:
                highs.append(edges[i + 1][1] - edges[i][1])
                i += 2
            else:
                i += 1
        return highs

    @staticmethod
    def _decode(pulses):
        # try every start offset; only a checksum-valid frame is accepted
        for s in range(0, max(0, len(pulses) - 39)):
            bits = [1 if p > DHT_ONE_US else 0 for p in pulses[s:s + 40]]
            if len(bits) != 40:
                continue
            b = [int("".join(map(str, bits[i * 8:(i + 1) * 8])), 2)
                 for i in range(5)]
            if ((b[0] + b[1] + b[2] + b[3]) & 0xFF) != b[4]:
                continue
            hum = b[0] + b[1] / 10.0
            temp = b[2] + b[3] / 10.0
            if -40.0 <= temp <= 80.0 and 0.0 <= hum <= 100.0:
                return temp, hum
        return None

    def read(self):
        pulses = self._collect()
        self._last_pulses = pulses or []
        if not pulses:
            return None
        return self._decode(pulses)

    def tick(self):
        v = self.read()
        if v is None:
            self._fails += 1
            if self._fails == 1 or self._fails % 10 == 0:
                pulses = self._last_pulses
                detail = (f"n={len(pulses)} head={pulses[:6]}"
                          if pulses else "no pulses")
                print(f"dht: read failed ({self._fails}x) {detail}",
                      file=sys.stderr, flush=True)
            return
        self._fails = 0
        temp, hum = v
        now = time.time()
        # The driver holds its own count (per-boot): reaching into the cortex
        # for a counter is the same cross-service read as reaching for the
        # reading, and a rebooted body starts at zero.
        self._count += 1
        publish({"temp_c": temp, "hum_pct": hum,
                          "temp_ts": now, "temp_count": self._count})

    def run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.period)

    def stop(self):
        self._stop.set()
