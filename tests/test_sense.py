"""DHT11 decode + weather state tests — pure logic, no hardware."""
import os
import tempfile

# Isolate the cortex DB before anything imports loa.cortex.
os.environ["LOA_CORTEX_DB"] = os.path.join(tempfile.mkdtemp(), "cortex-test.db")

from loa import cortex  # noqa: E402
from loa.sense import DHT11  # noqa: E402


def _pulses_for(bytes5):
    """Build a pulse list from a DHT11 frame (5 bytes, bits MSB-first).

    Each bit is a ~50us low followed by a high: 26us = 0, 70us = 1.
    A response high (~80us) leads the frame so the alignment search has
    something to skip.
    """
    pulses = [80_000]
    for byte in bytes5:
        for bit in range(8):
            v = (byte >> (7 - bit)) & 1
            pulses.append(70_000 if v else 26_000)
    return pulses


def test_decode_valid_dht11():
    # 47.5% / 22.7C with checksum 0x51
    temp_hum = DHT11._decode(_pulses_for([0x2F, 0x05, 0x16, 0x07, 0x51]))
    assert temp_hum == (22.7, 47.5)


def test_decode_rejects_bad_checksum():
    assert DHT11._decode(_pulses_for([0x2F, 0x05, 0x16, 0x07, 0x00])) is None


def test_decode_finds_shifted_frame():
    # an extra pulse before the frame must not break alignment search
    pulses = [80_000, 26_000] + _pulses_for([0x2F, 0x05, 0x16, 0x07, 0x51])
    assert DHT11._decode(pulses) == (22.7, 47.5)


def test_decode_drifted_zero_stays_zero():
    # this clone's '0' high drifts to ~47us; threshold is 60us
    pulses = [80_000]
    for byte in [0x2F, 0x05, 0x16, 0x07, 0x51]:
        for bit in range(8):
            v = (byte >> (7 - bit)) & 1
            pulses.append(70_000 if v else 47_000)
    assert DHT11._decode(pulses) == (22.7, 47.5)


def test_set_state_allows_weather_keys():
    # regression: temp_* were missing from cortex.set_state's allowed set
    cortex.set_state({"temp_c": 22.7, "hum_pct": 48.5, "temp_ts": 1.0,
                      "temp_count": 1})
    st = cortex.get_state()
    assert st["temp_c"] == 22.7
    assert st["hum_pct"] == 48.5
    assert st["temp_count"] == 1