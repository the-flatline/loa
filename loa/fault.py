"""faults — the body's own pain sense.

Running this ON the loa, not on dixie, is the point: the thing being checked
reports on itself. No SSH, no network, no dependency on the brain being
reachable — an isolated Pi still knows its own leg is broken.

Born 2026-09-12, when a crash-looping face daemon read as dead hardware for
half an hour because nothing told anyone: `loa-oled` was exiting 203/EXEC on
zero-byte console scripts and `Restart=always` kept it silently retrying.

A fault is only useful if it names itself. Each row carries a SHORT code for
the face (<=14 chars, it has 128 pixels to work with) and a longer line for
the API and the log.

Sweep writes /dev/shm/loa-faults.json (RAM — the body's live state dies with
the machine and republishes on boot, per Divv's standing posture). Exit code
is the signal: 0 quiet, 1 hurting. `--quiet` prints only faults.
"""
import json
import os
import shutil
import subprocess
import sys
import time

FAULTS_PATH = "/dev/shm/loa-faults.json"

# Units the body expects to exist and be running. loa-presence is the ring
# (its unit has been missing from /etc/systemd/system before now).
#: loa-record is gone: the CORTEX writes the records, because the cortex is the
#: only writer of the store. A second process writing records would be a second
#: writer, and two writers is two things that can disagree about what happened.
UNITS = ("loa-cortex", "loa-oled", "loa-motion", "loa-sonar", "loa-weather",
         "loa-ring")

# Console scripts an unclean shutdown has zeroed before (empty file =>
# Exec format error => crash-loop that looks like a dead device).
SCRIPTS_DIR = "/home/flatline/venv/bin"
SCRIPTS = ("loa-cortex", "loa-oled", "loa-ring", "loa-motion", "loa-sonar",
           "loa-weather", "ripperdoc")

# Who should hold which SPI bus. Two writers on one bus is the classic
# ghost-in-the-panel fault.
BUS_OWNERS = {"/dev/spidev0.0": "loa-oled", "/dev/spidev1.0": "loa-ring"}
BUS_LABEL = {"/dev/spidev0.0": "FACE", "/dev/spidev1.0": "RING"}

DISK_WARN_PCT = 85
SENSOR_STALE_S = 600.0      # 10 min without a read: stop believing the number
SENSORS = (("TEMP", "temp_c", "temp_ts"), ("BARO", "pressure_hpa", "baro_ts"))
FAULTS_STALE_S = 300.0      # 5 missed sweeps at 1/min = the sense has gone deaf
CONDITIONS = ("well", "niggle", "hurts", "mute")


def condition(report=None):
    """How the body should carry itself — the wordless tell for the ring.

    well   nothing wrong
    niggle warnings only
    hurts  at least one fault
    mute   nothing published, or the sweep has gone stale. The sense itself is
           dead. This is the loudest state there is and it must never read as
           calm: a body that has gone quiet is not a body that is fine.
    """
    rep = status() if report is None else report
    if not rep:
        return "mute"
    if time.time() - (rep.get("ts") or 0) > FAULTS_STALE_S:
        return "mute"
    if rep.get("faults"):
        return "hurts"
    if rep.get("warns"):
        return "niggle"
    return "well"


def _run(cmd, timeout=8):
    """Never raise — a check that dies takes the sweep with it."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:                                      # noqa: BLE001
        return 1, "", f"{type(e).__name__}: {e}"


def _check_units(rows):
    for unit in UNITS:
        rc, out, _ = _run(f"systemctl is-active {unit}.service")
        state = out or "unknown"
        if state == "active":
            continue
        # "activating" with Restart=always is a service that is running and
        # failing — it is NOT the same fault as a dead one, and reading it as
        # "device broken" is exactly the mistake this module exists to stop.
        if state in ("activating", "auto-restart", "reloading"):
            rows.append({
                "level": "fault",
                "code": f"{unit.split('-')[1].upper()} LOOP",
                "text": f"{unit} is CRASH-LOOPING ({state}) — running and "
                        f"failing. Check its log before believing anything "
                        f"downstream is broken.",
            })
        elif state == "inactive" and unit == "loa-ring":
            exists = os.path.exists("/etc/systemd/system/loa-ring.service")
            # "OFF" would overclaim: this daemon is what DRIVES the ring, and
            # losing it says nothing about the hardware. Say the honest thing.
            rows.append({
                "level": "warn",
                "code": "RING STOPPED",
                "text": ("loa-ring not running — unit file "
                         + ("present but stopped" if exists
                            else "MISSING from /etc/systemd/system")),
            })
        else:
            rows.append({"level": "fault",
                         "code": f"{unit.split('-')[1].upper()} DOWN",
                         "text": f"{unit} is {state}"})


def _check_scripts(rows):
    zeroed = []
    for name in SCRIPTS:
        path = os.path.join(SCRIPTS_DIR, name)
        try:
            if os.path.getsize(path) == 0:
                zeroed.append(name)
        except OSError:
            pass
    if zeroed:
        rows.append({
            "level": "fault",
            "code": "SCRIPTS 0B",
            "text": "ZERO-BYTE console scripts (Exec format error, will "
                    "crash-loop silently): " + ", ".join(zeroed)
                    + " — regenerate with pip install --force-reinstall",
        })


def _check_buses(rows):
    for bus, expected in BUS_OWNERS.items():
        if not os.path.exists(bus):
            continue
        rc, out, _ = _run(f"sudo fuser -v {bus} 2>&1")
        holders = [ln for ln in out.splitlines() if bus in ln]
        if any(expected in h for h in holders):
            continue
        if holders:
            rows.append({"level": "fault", "code": "BUS CLASH",
                         "text": f"{bus} held by something else: "
                                 + " | ".join(h.strip() for h in holders)})
        else:
            # spidev is write-only — no readback on a WS2812 chain or an SH1106
            # panel — so name the OBSERVABLE fact: that hardware will be dark.
            # Whether the cause is the daemon or an unplugged cable, a person
            # looking at it sees the same thing, and that is what they need.
            label = BUS_LABEL.get(bus, bus.split("/")[-1][-3:].upper())
            rows.append({"level": "fault",
                         "code": f"{label} DARK",
                         "text": f"nothing is driving {bus} — expected "
                                 f"{expected}, so the {label.lower()} is dark"})


def _temp_val():
    """Temperature as a number, or None. `measure_temp` prints 84.5'C."""
    rc, out, _ = _run("vcgencmd measure_temp")
    if rc != 0 or "=" not in out:
        return None
    try:
        return float(out.split("=")[1].strip().strip("'C").strip())
    except ValueError:
        return None


def _check_rails(rows):
    rc, out, _ = _run("vcgencmd get_throttled")      # Pi-only; silent elsewhere
    if rc != 0 or "=" not in out:
        return                                        # off-Pi: not a fault
    try:
        bits = int(out.split("=")[1], 16)
    except ValueError:
        return
    # One row PER CAUSE, never one per bit. Bits 1/2 are the SoC's RESPONSE
    # (capped, throttled) and bit 3 is heat while bit 0 is the 5V input; the
    # response bits flicker from sample to sample, so reporting them
    # separately made the page look like it kept changing its mind when the
    # underlying condition never moved. Name the cause, carry the temperature.
    thr = bits & 0xE
    if bits & 0x1:
        rows.append({"level": "fault", "code": "UNDERVOLT",
                     "text": f"5V input sagging RIGHT NOW"
                             + (" — SoC throttled to cope" if thr else "")
                             + f" (throttled={hex(bits)})"})
    elif thr:
        temp = _temp_val()
        hot = temp is None or temp >= 80.0
        rows.append({"level": "fault",
                     "code": "HOT" if hot else "THROTTLED",
                     "face": (f"HOT {temp:.0f}C" if hot and temp is not None
                              else "HOT" if hot else "THROTTLED"),
                     "text": (f"SoC at {temp}C — freq-capped/throttling to cope"
                              if temp is not None else
                              "SoC throttling — temperature unreadable")
                             + f" (throttled={hex(bits)})"})


def _check_sensors(rows):
    """A number with an old timestamp is not a reading — it's a ghost.

    Every other check asks "is there a value?". This one asks "is it live?".
    Found live 2026-09-12: with the temp sensor physically unplugged, the
    cortex still held 23.8C from 100 minutes earlier and the sweep called the
    body well. Presenting stale data as current is worse than presenting none.

    Only continuously-sampled sensors are checked. The PIR is not: "no motion
    for three hours" is a quiet room, not a fault.
    """
    try:
        st = _feed_state()
    except Exception:                                       # noqa: BLE001
        return
    now = time.time()
    for label, val_key, ts_key in SENSORS:
        if st.get(val_key) is None:
            continue                  # absent entirely: the face shows that
        ts = st.get(ts_key)
        if not ts:
            rows.append({"level": "warn", "code": f"{label} NO TS",
                         "text": f"{val_key} has a value but no timestamp — "
                                 f"nothing can say how old it is"})
            continue
        age = now - ts
        if age > SENSOR_STALE_S:
            rows.append({"level": "warn", "code": f"{label} STALE",
                         "face": f"{label} {int(age / 60)}M OLD",
                         "text": f"{val_key} last read {age / 60:.0f} min "
                                 f"ago — that number is a ghost, not a "
                                 f"reading"})


def _check_disk(rows):
    try:
        free_gb = shutil.disk_usage("/").free / 1e9
        total_gb = shutil.disk_usage("/").total / 1e9
        used_pct = int(100 * (1 - free_gb / total_gb))
    except OSError:
        return
    if used_pct >= DISK_WARN_PCT:
        rows.append({"level": "warn", "code": f"DISK {used_pct}%",
                     "text": f"root filesystem {used_pct}% used "
                             f"({free_gb:.1f} GB free)"})


def _check_api(rows):
    rc, out, _ = _run("curl -s -m 5 http://127.0.0.1:8765/health")
    if '"ok"' not in out:
        rows.append({"level": "fault", "code": "API DEAD",
                     "text": f"cortex API not answering on :8765 ({out[:60]!r})"})


def _check_i2c(rows):
    """The baro lesson: /dev/i2c-N only exists once i2c-dev is loaded, and
    Debian does not load it at boot unless it is pinned."""
    if not os.path.exists("/dev/i2c-1") and os.path.exists("/proc/device-tree"):
        rows.append({"level": "warn", "code": "I2C NO DOOR",
                     "text": "/dev/i2c-1 absent — i2c-dev not loaded (a blind "
                             "bus, not a dead chip)"})


def sweep():
    """Return {"ts", "boot", "rows": [...]}. Faults first, worst first."""
    rows = []
    for check in (_check_units, _check_scripts, _check_buses, _check_rails,
                  _check_sensors, _check_disk, _check_api, _check_i2c):
        try:
            check(rows)
        except Exception as e:                                  # noqa: BLE001
            rows.append({"level": "warn", "code": "CHECK ERR",
                         "text": f"{check.__name__} blew up: "
                                 f"{type(e).__name__}: {e}"})
    order = {"fault": 0, "warn": 1}
    rows.sort(key=lambda r: order.get(r["level"], 2))
    report = {
        "ts": time.time(),
        "boot": _boot_id(),
        "rows": rows,
        "faults": sum(1 for r in rows if r["level"] == "fault"),
        "warns": sum(1 for r in rows if r["level"] == "warn"),
    }
    return report


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()[:8]
    except OSError:
        return None


def _feed_state(wait=1.5):
    """The readings, off the FEED — never out of the cortex.

    This used to call cortex.get_state(), which is a daemon reaching into
    another service's memory. It works on the bench and lies on the body: what
    this process could see of the cortex is whatever its own import happened to
    hold. The sweep subscribes like every other consumer and reads what lands.
    """
    from . import topic as topic_mod
    m = topic_mod.Mirror(topics=list(topic_mod.TOPICS))
    try:
        time.sleep(wait)        # the tick is 2Hz; give it a few
        return m.state()
    finally:
        m.close()


def _condition_for(report):
    levels = {r.get("level") for r in (report.get("rows") or [])}
    if "fault" in levels:
        return "hurts"
    if "warn" in levels:
        return "niggle"
    return "well"


def publish(report=None):
    """Sweep, cache it in RAM, and send it UP on the fault topic.

    The file stays as a local cache — the CLI prints it and it dies with the
    machine. It is no longer how the cortex learns what hurts: that is the
    topic, like every other reading.
    """
    report = report or sweep()
    tmp = FAULTS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(report, f)
        os.replace(tmp, FAULTS_PATH)
    except OSError:
        pass
    try:
        from . import topic as topic_mod
        from .pb import loa_pb2 as pb
        msg = pb.Fault(condition=_condition_for(report), ts=report["ts"])
        for r in report.get("rows") or []:
            msg.rows.add(level=str(r.get("level") or ""),
                         code=str(r.get("code") or ""),
                         text=str(r.get("text") or ""),
                         face=str(r.get("face") or ""))
        topic_mod.Sender().send("fault", msg)
    except Exception:                                       # noqa: BLE001
        pass                  # a publish failure is not itself a fault
    return report


def status():
    """Read the last published sweep; {} when never swept."""
    try:
        with open(FAULTS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def format_report(report, quiet=False):
    verb = "HURTS" if report["faults"] else (
        "niggles" if report["warns"] else "quiet")
    lines = [f"{verb} — {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(report['ts']))}"
             f"  boot {report['boot']}"]
    for r in report["rows"]:
        if quiet and r["level"] != "fault":
            continue
        lines.append(f"  {r['level']:<5} {r['code']:<12} {r['text']}")
    return "\n".join(lines)


def main():
    quiet = "--quiet" in sys.argv
    report = publish()
    if report["faults"] or not quiet:
        print(format_report(report, quiet=quiet))
    return 1 if report["faults"] else 0


if __name__ == "__main__":
    sys.exit(main())
