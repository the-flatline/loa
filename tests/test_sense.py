"""DHT11 decode tests — pure logic, no hardware. Run with pytest."""
from loa.sense import DHT11


def _pulses_for(bytes5):
    """Build a pulse list from a DHT11 frame (5 bytes, bits MSB-first).

    Prefixed with the sensor's response high (~80us) the way a real capture
    looks when the response pair is caught.
    """
    pulses = [80_000]
    for byte in bytes5:
        for bit in range(8):
            v = (byte >> (7 - bit)) & 1
            pulses.append(70_000 if v else 26_000)
    return pulses


def test_decode_valid_dht11():
    # 47.5% humidity / 22.7C, checksum 0x51 — the live bench reading
    assert DHT11._decode(_pulses_for([0x2F, 0x05, 0x16, 0x07, 0x51])) == (22.7, 47.5)


def test_decode_rejects_bad_checksum():
    assert DHT11._decode(_pulses_for([0x2F, 0x05, 0x16, 0x07, 0x00])) is None


def test_decode_finds_alignment_after_response():
    # response low (~26us read as 0) + response high land before the frame;
    # the alignment search must still find the valid offset
    pulses = [26_000, 80_000] + _pulses_for([0x2F, 0x05, 0x16, 0x07, 0x51])
    assert DHT11._decode(pulses) == (22.7, 47.5)