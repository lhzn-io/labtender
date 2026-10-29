"""Sign-in through an authenticating proxy: the trusted header, the allow-list and its limits."""

import pytest
from fastapi.testclient import TestClient

from labtender.db import Store
from labtender.proxy import ProxyError, load
from labtender.server import create_app, token_hash

CONFIG = """
header = "X-Auth-Request-Email"
trusted = ["127.0.0.1", "::1"]

[[allow]]
user = "Dana@Example.org"
identity = "dana"

[[allow]]
user = "pat@example.org"
identity = "pat"
roles = ["viewer", "operator", "admin", "purser"]
"""
DANA = {"X-Auth-Request-Email": "dana@example.org"}
PAT = {"X-Auth-Request-Email": "pat@example.org"}
WRITE = {"X-Requested-With": "labtender"}


@pytest.fixture
def lab(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    (tmp_path / "proxy.toml").write_text(CONFIG)
    app = create_app(store=store, home=tmp_path, collect=False)
    store.add_host("gpu-box", actor="t", transport="local")
    via_proxy = TestClient(app, client=("127.0.0.1", 40000))
    direct = TestClient(app, client=("192.0.2.7", 40000))
    return store, via_proxy, direct


def test_the_proxy_signs_people_in(lab):
    store, http, _ = lab
    who = http.get("/api/whoami", headers=DANA).json()
    assert who == {
        "identity": "dana",
        "kind": "person",
        "roles": ["viewer", "operator", "admin"],
        "via": "proxy",
    }
    assert http.get("/api/whoami", headers=PAT).json()["roles"][-1] == "purser"


def test_a_change_needs_the_board_header(lab):
    _, http, _ = lab
    claim = {"resource": "gpu-box", "est_hours": 1}
    r = http.post("/api/claims", json=claim, headers=DANA)
    assert r.status_code == 403 and "X-Requested-With" in r.json()["detail"]
    r = http.post("/api/claims", json=claim, headers={**DANA, **WRITE})
    assert r.status_code == 201 and r.json()["owner"] == "dana"


def test_the_header_counts_only_from_the_proxy(lab):
    _, _, direct = lab
    assert direct.get("/api/whoami", headers=DANA).status_code == 401


def test_unknown_users_are_refused(lab):
    _, http, _ = lab
    r = http.get("/api/whoami", headers={"X-Auth-Request-Email": "eve@example.org"})
    assert r.status_code == 403 and "allow-list" in r.json()["detail"]


def test_a_bearer_token_always_decides(lab):
    store, http, _ = lab
    store.add_token(token_hash("agent-t"), "sweep-agent", "agent", actor="t")
    r = http.get("/api/whoami", headers={**PAT, "Authorization": "Bearer agent-t"})
    assert r.json()["identity"] == "sweep-agent" and "purser" not in r.json()["roles"]
    r = http.get("/api/whoami", headers={**PAT, "Authorization": "Bearer wrong"})
    assert r.status_code == 401  # a bad token is never rescued by the header


def test_purser_needs_the_role_on_the_allow_list(lab):
    store, http, _ = lab
    r = http.put("/api/budgets/default", json={"daily_usd": 10}, headers={**DANA, **WRITE})
    assert r.status_code == 403
    r = http.put("/api/budgets/default", json={"daily_usd": 10}, headers={**PAT, **WRITE})
    assert r.status_code == 200


def test_an_agent_identity_is_never_signed_in(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_token(token_hash("a"), "dana", "agent", actor="t")
    (tmp_path / "proxy.toml").write_text(CONFIG)
    with pytest.raises(ProxyError, match="agent identity"):
        load(tmp_path, store)


def test_an_agent_token_issued_later_shuts_the_identity_out(lab):
    store, http, _ = lab
    store.add_token(token_hash("a"), "dana", "agent", actor="t")
    r = http.get("/api/whoami", headers=DANA)
    assert r.status_code == 403 and "agent identity" in r.json()["detail"]


def test_a_bad_config_stops_the_service(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    (tmp_path / "proxy.toml").write_text('header = "Authorization"\n')
    with pytest.raises(ProxyError):
        load(tmp_path, store)
    (tmp_path / "proxy.toml").write_text(
        'header = "X-User"\n[[allow]]\nuser = "a@b"\nidentity = "a"\nroles = ["root"]\n'
    )
    with pytest.raises(ProxyError, match="unknown role"):
        load(tmp_path, store)


def test_no_config_means_no_proxy(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    assert load(tmp_path, store) is None
    app = create_app(store=store, home=tmp_path, collect=False)
    http = TestClient(app, client=("127.0.0.1", 40000))
    assert http.get("/api/whoami", headers=DANA).status_code == 401


def test_the_board_script_is_served(lab):
    _, http, _ = lab
    r = http.get("/auth.js")
    assert r.status_code == 200 and "ltFetch" in r.text
    assert '<script src="/auth.js">' in http.get("/purser").text
