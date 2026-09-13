"""Shared fixtures for the loa tests.

The cortex database is the one piece of global state these tests must not touch
for real: a test that writes the body's own state row is a test that lies about
what the body is doing.
"""
import pytest

from loa.cortex import state as cortex


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A cortex database of our own."""
    db = tmp_path / "cortex.db"
    monkeypatch.setattr(cortex, "DB_PATH", str(db))
    monkeypatch.setattr(cortex, "_conn", None)
    monkeypatch.setattr(cortex.config, "load", lambda: {})
    yield db
    monkeypatch.setattr(cortex, "_conn", None)


@pytest.fixture
def clean_publishers():
    """Drop any publish hooks a test registered, so the next test starts with
    no publisher attached and no listener left over from the last one."""
    saved = list(cortex._PUBLISHERS)
    yield
    cortex._PUBLISHERS[:] = saved
