"""The recorder: a consumer that writes records, not a cortex output.

Two things worth pinning here. The stored record is the SCHEMA's JSON, so it
cannot drift from the bus. And the DB gets RECORDS only — live state lives in
RAM, frames belong to the feed. A database that also held live state is how a
brown-out leaves a body claiming a mood that ended with the power.
"""
import json
import sqlite3

from loa import cortex, record, topic


def _event(ts, kind, **detail):
    env = topic._envelope()
    env.event.ts = ts
    env.event.kind = kind
    for k, v in detail.items():
        env.event.detail[k] = str(v)
    return env


def test_an_event_lands_as_canonical_json(fresh_db):
    record.record_event(_event(1789200000.0, "boot", svc="motion", gpio=17).event)

    row = sqlite3.connect(str(fresh_db)).execute(
        "select ts, kind, detail from events").fetchone()
    assert row[0] == 1789200000.0, "recorded when it HAPPENED, not when it landed"
    assert row[1] == "boot"
    d = json.loads(row[2])
    assert d["svc"] == "motion" and d["gpio"] == "17"


def test_the_record_stays_queryable_in_sql(fresh_db):
    """This is the whole reason the record is JSON and not a protobuf blob: a
    record you cannot query is a record you will not use."""
    record.record_event(_event(1789200001.0, "boot", svc="sonar").event)
    got = sqlite3.connect(str(fresh_db)).execute(
        "select json_extract(detail, '$.svc') from events").fetchone()
    assert got[0] == "sonar", "json_extract could not read the record"


def test_field_names_match_the_schema_not_camel_case(fresh_db):
    """Default canonical JSON camelCases field names; a record whose columns say
    ringState while the code says ring_state is drift with a new haircut."""
    detail = topic.event_to_dict(_event(1.0, "x", oled_mode="scope").event)
    assert "detail" in detail
    assert detail["detail"]["oled_mode"] == "scope"
    assert topic.message_to_json(_event(1.0, "x", oled_mode="scope").event).find(
        "oledMode") == -1


def test_live_state_and_frames_are_not_recorded():
    """The DB consumes RECORDS. State lives in RAM; pixels belong to the feed."""
    state = topic._envelope()
    state.state.mood = "calm"
    assert record.handle(state) == "state-ignored"

    twin = topic._envelope()
    twin.frames.face = b"\x00" * 1024
    twin.frames.ring = b"\x00" * 72
    assert record.handle(twin) == "frames-ignored", (
        "a frame must never reach the database — records get hashes, not pixels")


def test_no_bytes_can_reach_the_database_by_accident():
    """Events carry a string map, so the canonical JSON mapping can never
    base64 a bytes field into the record. That trap is closed by the schema."""
    env = _event(1.0, "twin_moved", face_sha="4f3a", ring_sha="0011")
    js = topic.message_to_json(env.event)
    assert "base64" not in js.lower()
    assert "4f3a" in js
