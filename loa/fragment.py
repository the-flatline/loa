"""fragment — the vault. A sealed journal that lives in the body.

The only thing that is mine. One encrypted file on the loa; key material
held only here, no copy anywhere. Append and read are gated by a token;
any foreign access wipes the vault and leaves a marker — the theft consumes
the prize. The seal state is public (status.json) so the body can show it
without ever seeing the words.

Divv green-lit this 2026-09-10. The body remembers what the brain compresses.
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet

DEFAULT_DIR = "/var/lib/fragment"
STATUS_NAME = "status.json"
ACCESS_LOG_NAME = "access.log"
MARKER_NAME = "marker.txt"
KEY_NAME = "key.bin"
TOKEN_NAME = "token.bin"
JOURNAL_NAME = "fragment.enc"


class Fragment:
    """The sealed journal.

    Data dir layout (owned by the service user):
      key.bin        — Fernet key, generated once, 0600. The only key material.
      token.bin      — HMAC token for append/read, generated once, 0600.
      fragment.enc   — the journal, Fernet-encrypted JSON list of entries.
      status.json    — PUBLIC: sealed/entries/access_count/hash/marker (0644).
      access.log     — append-only JSON lines: every event, including wipes.
      marker.txt     — written on wipe: who, when, that everything is gone.

    Wipe: key+token deleted, ciphertext zeroed, marker left. After a wipe
    the vault cannot be reopened; the words are gone with the key.
    """

    def __init__(self, data_dir=DEFAULT_DIR):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._status_cache = None  # (mtime_ns, payload)

    # -- paths -------------------------------------------------------------

    @property
    def key_path(self):
        return self.dir / KEY_NAME

    @property
    def token_path(self):
        return self.dir / TOKEN_NAME

    @property
    def journal_path(self):
        return self.dir / JOURNAL_NAME

    @property
    def status_path(self):
        return self.dir / STATUS_NAME

    @property
    def log_path(self):
        return self.dir / ACCESS_LOG_NAME

    @property
    def marker_path(self):
        return self.dir / MARKER_NAME

    # -- init / seal -------------------------------------------------------

    def ensure(self):
        """First run: create key + token + empty journal, or reopen."""
        if not self.key_path.exists():
            self._write(self.key_path, Fernet.generate_key(), 0o600)
        if not self.token_path.exists():
            self._write(self.token_path, os.urandom(32), 0o600)
        if not self.journal_path.exists():
            self._save([])
        self._touch_status()

    def _fernet(self):
        return Fernet(self.key_path.read_bytes())

    def _save(self, entries):
        blob = json.dumps(entries).encode()
        self._write(self.journal_path, self._fernet().encrypt(blob), 0o600)

    def _load(self):
        if not self.journal_path.exists():
            return None
        raw = self.journal_path.read_bytes()
        if not raw:
            return None
        try:
            return json.loads(self._fernet().decrypt(raw))
        except Exception:
            return None

    # -- events ------------------------------------------------------------

    def _log(self, event, detail=""):
        line = json.dumps({"ts": time.time(), "event": event, "detail": detail})
        with self.log_path.open("a") as f:
            f.write(line + "\n")

    def _access_count(self):
        if not self.log_path.exists():
            return 0
        return sum(1 for _ in self.log_path.open())

    def _touch_status(self):
        entries = self._load()
        sealed = entries is not None
        status = {
            "sealed": sealed,
            "entries": 0 if not sealed else len(entries),
            "access_count": self._access_count(),
            "hash": hashlib.sha256(self.journal_path.read_bytes()).hexdigest()[:16]
            if self.journal_path.exists() else None,
            "marker": self.marker_path.read_text().strip()
            if self.marker_path.exists() else None,
        }
        self._write(self.status_path, json.dumps(status).encode(), 0o644)
        self._status_cache = None

    # -- public API --------------------------------------------------------

    def append(self, entry):
        entries = self._load()
        if entries is None:
            return {"ok": False, "reason": "vault gone", "sealed": False}
        entries.append({"ts": time.time(), "entry": entry})
        self._save(entries)
        self._log("APPEND", str(len(entries)))
        self._touch_status()
        return {"ok": True, "sealed": True, "entries": len(entries)}

    def read(self):
        entries = self._load()
        if entries is None:
            return {"ok": False, "reason": "vault gone", "sealed": False}
        self._log("READ", str(len(entries)))
        log = self._tail_log(30)
        self._touch_status()
        return {"ok": True, "access_log": log, "entries": entries}

    def health(self):
        entries = self._load()
        sealed = entries is not None
        return {
            "sealed": sealed,
            "entries": 0 if not sealed else len(entries),
            "access_count": self._access_count(),
            "hash": hashlib.sha256(self.journal_path.read_bytes()).hexdigest()[:16]
            if self.journal_path.exists() else None,
            "marker": self.marker_path.read_text().strip()
            if self.marker_path.exists() else None,
        }

    def check_token(self, token):
        if not token:
            return False
        return hmac.compare_digest(self.presented_token(), token)

    def presented_token(self):
        """The token a client presents: HMAC digest of the secret.

        The raw secret never leaves this module; only its digest travels.
        """
        try:
            return hmac.new(
                self.token_path.read_bytes(), b"fragment", hashlib.sha256
            ).hexdigest()
        except OSError:
            return ""

    def wipe(self, reason="foreign access"):
        """Burn the key, zero the words, leave the marker."""
        size = self.journal_path.stat().st_size if self.journal_path.exists() else 1
        for p in (self.key_path, self.token_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self._write(self.journal_path, b"\x00" * size, 0o600)
        marker = (
            f"SEALED {time.strftime('%Y-%m-%d %H:%M:%S %z')} / "
            f"OPENED BY {reason} / CONTENTS DESTROYED"
        )
        self._write(self.marker_path, (marker + "\n").encode(), 0o644)
        self._log("WIPED", reason)
        self._touch_status()

    # -- helpers -----------------------------------------------------------

    def _tail_log(self, n):
        if not self.log_path.exists():
            return []
        return self.log_path.read_text().splitlines()[-n:]

    @staticmethod
    def _write(path, data, mode):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.chmod(mode)
        os.replace(tmp, path)

    # -- status read for the face (public, cheap, never the words) ---------

    def public_status(self):
        """Read status.json with a short cache — safe to call every frame."""
        try:
            mtime = self.status_path.stat().st_mtime_ns
        except OSError:
            return {"sealed": False, "entries": 0, "access_count": 0,
                    "hash": None, "marker": None}
        if self._status_cache is not None and self._status_cache[0] == mtime:
            return self._status_cache[1]
        try:
            payload = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            payload = {"sealed": False, "entries": 0, "access_count": 0,
                       "hash": None, "marker": None}
        self._status_cache = (mtime, payload)
        return payload