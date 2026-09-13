"""The door's shape, made permanent: ONE route, and no verb with no caller.

Divv's rule, verbatim: "ONE /api ... Even fragment/vault/seal — is a PART OF
YOUR BODY. That was the idea. So it ALL goes through cortex." And: "GET RID OF
/state". And: "the connection into you is via the ripperdoc. That's it!"

So this file guards two things at once:

  * the HTTP surface is EXACTLY {"/api"} — asserted off the live route table
    AND off the served OpenAPI schema, so a new door cannot be added a route at
    a time without failing here first;
  * every removed path returns 404 — /state above all, because it was live data
    over HTTP and the single largest source of drift.

The verbs are exactly what the two real clients issue. `ping` is deliberately
absent: a verb with no caller is a door opened to "have it available".
"""
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from loa import cortex, cortexd, expressions as expr, face, fragment as fragment_mod
from loa import moods

#: Every path that used to be a door. None may answer again.
DELETED_PATHS = (
    "/health", "/state", "/feel", "/express", "/ring", "/display", "/ripperdoc",
    "/fragment/health", "/fragment/append", "/fragment/read",
)

#: The verbs, verbatim and complete.
VERBS = {
    "feel", "express", "ring", "display", "ripperdoc",
    "vault.health", "vault.append", "vault.read",
}


@pytest.fixture(autouse=True)
def clean_body():
    # reset_for_tests() only — NOT cortex.boot(None): boot() starts a daemon
    # flusher thread per call, and with no store it flips the module-global
    # _db_down to True a second later, which races test_topics_contract's
    # "no faults" assertion. The door needs no store; RAM state is enough.
    cortex.reset_for_tests()
    yield
    cortex.reset_for_tests()


@pytest.fixture
def client():
    return TestClient(cortexd.app)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault of our own. NEVER the live /var/lib/fragment — a wrong token
    there wipes the journal permanently, and a test must not be able to."""
    f = fragment_mod.Fragment(str(tmp_path / "vault"))
    f.ensure()
    monkeypatch.setattr(cortexd, "_frag_cache", f)
    return f


# ---------------------------------------------------------------------------
# the shape

def test_the_route_table_is_exactly_one_door():
    """Read the app's own route table. Only fastapi APIRoute entries are doors;
    the OpenAPI/redoc routes are plumbing, not a way into the body."""
    doors = {r.path for r in cortexd.app.routes if isinstance(r, APIRoute)}
    assert doors == {"/api"}, (
        "the HTTP surface must be EXACTLY {/api} — add a VERB, never a route. "
        "found: %s" % sorted(doors))
    methods = {tuple(sorted(r.methods))
               for r in cortexd.app.routes
               if isinstance(r, APIRoute) and r.path == "/api"}
    assert methods == {("POST",)}, "the one door is POST, always"


def test_the_openapi_schema_advertises_one_door(client):
    """The served schema is what a client reads off the wire — it must agree."""
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {"/api"}, sorted(paths)


def test_the_verb_list_is_exactly_the_callers(client):
    """No speculative verbs. `ping` in particular must not come back."""
    assert set(cortexd._DISPATCH) == VERBS, sorted(cortexd._DISPATCH)
    assert "ping" not in cortexd._DISPATCH


# ---------------------------------------------------------------------------
# the deleted doors

@pytest.mark.parametrize("path", DELETED_PATHS)
def test_every_deleted_path_is_gone(client, path):
    assert client.get(path).status_code == 404, f"GET {path} answered"
    assert client.post(path, json={}).status_code == 404, f"POST {path} answered"


def test_state_is_specifically_dead(client):
    """Called out on its own: /state was live data over HTTP and is the drift
    this whole exercise exists to end. If it 200s again, this fails."""
    r = client.get("/state")
    assert r.status_code == 404
    r = client.get("/state", params={"history": 5})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# one call per verb, through the door

def test_ping_is_not_a_verb(client):
    r = client.post("/api", json={"cmd": "ping", "args": {}})
    assert r.status_code == 400
    assert "ping" in r.json()["detail"]


def test_feel(client):
    r = client.post("/api", json={"cmd": "feel", "args": {"feeling": "calm"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["feeling"] == "calm"
    assert body["ring"] == moods.MOODS["calm"]["ring"]


def test_feel_rejects_a_bad_value(client):
    r = client.post("/api", json={"cmd": "feel", "args": {"feeling": "nope"}})
    assert r.status_code == 400
    assert r.json()["detail"] == f"feeling must be one of {sorted(moods.MOODS)}"


def test_express(client):
    r = client.post("/api", json={"cmd": "express",
                                  "args": {"expression": "happy"}})
    assert r.status_code == 200, r.text
    assert r.json()["expression"] == "happy" and r.json()["ok"] is True


def test_express_custom_needs_text(client):
    r = client.post("/api", json={"cmd": "express",
                                  "args": {"expression": "custom"}})
    assert r.status_code == 400
    assert r.json()["detail"] == "custom expression needs text"


def test_express_rejects_a_bad_value(client):
    r = client.post("/api", json={"cmd": "express",
                                  "args": {"expression": "nope"}})
    assert r.status_code == 400
    assert r.json()["detail"] == (
        f"expression must be one of {sorted(expr.EXPRESSIONS)} or 'custom'")


def test_ring(client):
    r = client.post("/api", json={"cmd": "ring", "args": {"state": "scan"}})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["ring"] == "scan"


def test_ring_rejects_a_bad_value(client):
    r = client.post("/api", json={"cmd": "ring", "args": {"state": "nope"}})
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "state must be home|busy|alarm (sustained) or scan|glitch (event)")


def test_display(client):
    r = client.post("/api", json={"cmd": "display",
                                  "args": {"mode": "ecg", "dim": True}})
    assert r.status_code == 200, r.text
    assert r.json()["oled"]["mode"] == "ecg" and r.json()["oled"]["dim"] is True


def test_display_rejects_a_bad_value(client):
    r = client.post("/api", json={"cmd": "display", "args": {"mode": "nope"}})
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "mode must be scope|ecg|ripple|noise|text|showoff|ripperdoc|off")


def test_ripperdoc_by_on(client):
    r = client.post("/api", json={"cmd": "ripperdoc", "args": {"on": True}})
    assert r.status_code == 200, r.text
    assert r.json()["ripperdoc"] == 1 and r.json()["oled"] == "ripperdoc"


def test_ripperdoc_by_page(client):
    r = client.post("/api", json={"cmd": "ripperdoc", "args": {"page": "pir"}})
    assert r.status_code == 200, r.text
    assert r.json()["page"] == "pir"


def test_ripperdoc_rejects_a_bad_page(client):
    r = client.post("/api", json={"cmd": "ripperdoc", "args": {"page": "nope"}})
    assert r.status_code == 400
    assert r.json()["detail"] == f"page must be {'|'.join(face.Ripperdoc.PAGES)}"


def test_vault_health(client, vault):
    r = client.post("/api", json={"cmd": "vault.health", "args": {}})
    assert r.status_code == 200, r.text
    assert r.json()["sealed"] is True


def test_vault_append_and_read(client, vault):
    tok = vault.presented_token()
    r = client.post("/api", json={"cmd": "vault.append", "args": {"entry": "hello"}},
                    headers={"X-Fragment-Token": tok})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["entries"] == 1

    r = client.post("/api", json={"cmd": "vault.read", "args": {}},
                    headers={"X-Fragment-Token": tok})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["entries"][0]["entry"] == "hello"


def test_vault_append_without_token_is_refused(client, vault):
    """A wrong token wipes — by design. Proven on a throwaway vault only."""
    r = client.post("/api", json={"cmd": "vault.append", "args": {"entry": "x"}})
    assert r.status_code == 403
    assert r.json()["detail"] == "seal broken — contents destroyed"
    assert vault.health()["sealed"] is False


def test_unknown_verb_lists_the_verbs(client):
    r = client.post("/api", json={"cmd": "wat", "args": {}})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "wat" in detail
    for verb in VERBS:
        assert verb in detail, verb
