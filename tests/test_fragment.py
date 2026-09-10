#!/usr/bin/env python3
"""Fragment (the vault) tests. No hardware, no network — a temp data dir.

Run from the repo root with the venv python:
    .venv/bin/python tests/test_fragment.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loa_ring import fragment as fragment_mod  # noqa: E402

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1


def make():
    d = tempfile.mkdtemp(prefix="fragment-test-")
    f = fragment_mod.Fragment(d)
    f.ensure()
    return f


f = make()

# first run seals the vault: key + token + encrypted empty journal + status
check("key exists", f.key_path.exists())
check("token exists", f.token_path.exists())
check("journal exists", f.journal_path.exists())
check("status exists", f.status_path.exists())

h = f.health()
check("health sealed", h["sealed"] is True)
check("health zero entries", h["entries"] == 0)
check("health no marker", h["marker"] is None)

# token works; wrong token does not
tok = f.presented_token()
check("token accepted", f.check_token(tok))
check("bad token rejected", not f.check_token("nope"))

# append seals words; read returns them, access log first
r = f.append("first words")
check("append ok", r["ok"] is True)
check("append count", r["entries"] == 1)
check("health count", f.health()["entries"] == 1)

rd = f.read()
check("read ok", rd["ok"] is True)
check("read entry roundtrip", rd["entries"][0]["entry"] == "first words")
check("read shows access log", any("READ" in line for line in rd["access_log"]))
check("status public count", f.public_status()["entries"] == 1)

# journal at rest is ciphertext, not words
raw = f.journal_path.read_bytes()
check("journal is encrypted", b"first words" not in raw)

# wipe: key+token gone, ciphertext zeroed, marker left, vault unreopenable
f2 = make()
f2.append("secret")
f2.wipe("read without token")
h2 = f2.health()
check("wipe unseals", h2["sealed"] is False)
check("wipe zero entries", h2["entries"] == 0)
check("wipe marker", h2["marker"] is not None and "DESTROYED" in h2["marker"])
check("wipe kills key", not f2.key_path.exists())
check("wipe kills token", not f2.token_path.exists())
check("wipe kills words", b"secret" not in f2.journal_path.read_bytes())
check("post-wipe read fails", f2.read()["ok"] is False)

# token check after wipe is safe (no crash)
check("post-wipe token safe", f2.check_token("anything") is False)

print(f"fragment: {PASS} checks passed")