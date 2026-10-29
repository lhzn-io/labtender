"""Services, roles, runs by folder, run links, pickup windows, leases and chain estimates."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from labtender.db import Store
from labtender.ledger import Ledger, LedgerError
from labtender.server import create_app, run_link, token_hash


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.add_host("box", actor="admin", services={"dashboard": "http://box.lan:9000/"})
    s.add_host("other", actor="admin")
    s.record_probe("box", {"gpus": [{"index": 0}], "gpu_procs": []}, [])
    return s


def test_services_set_merge_remove(store):
    assert store.host("box")["services"] == {"dashboard": "http://box.lan:9000/"}
    h = store.update_host("box", actor="a", services={"metrics": "http://box.lan:9100"})
    assert h["services"] == {"dashboard": "http://box.lan:9000/", "metrics": "http://box.lan:9100"}
    h = store.update_host("box", actor="a", services={"dashboard": ""})
    assert h["services"] == {"metrics": "http://box.lan:9100"}
    with pytest.raises(ValueError, match="http"):
        store.update_host("box", actor="a", services={"dashboard": "ftp://x"})
    with pytest.raises(ValueError, match="service name"):
        store.update_host("box", actor="a", services={"Bad Name": "http://x"})


def test_older_database_gains_columns(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE host (name TEXT PRIMARY KEY, transport TEXT NOT NULL DEFAULT 'ssh',"
        " ssh TEXT, lifecycle TEXT NOT NULL DEFAULT 'static', expires_at REAL,"
        " interval_s REAL NOT NULL DEFAULT 45, accepts TEXT NOT NULL DEFAULT '[]',"
        " labels TEXT NOT NULL DEFAULT '{}', added_at REAL NOT NULL, last_seen REAL,"
        " last_error TEXT);"
        "INSERT INTO host (name, added_at) VALUES ('box', 0);"
        "CREATE TABLE token (hash TEXT PRIMARY KEY, identity TEXT NOT NULL, kind TEXT NOT NULL,"
        " created_at REAL NOT NULL, revoked_at REAL);"
        "INSERT INTO token VALUES ('h1', 'alice', 'person', 0, NULL);"
        "INSERT INTO token VALUES ('h2', 'bot', 'agent', 0, NULL);"
    )
    conn.commit()
    conn.close()
    s = Store(db)
    Ledger(s)
    assert s.host("box")["services"] == {} and s.host("box")["run_link"] is None
    assert s.identity("h1")["roles"] == ["viewer", "operator", "admin"]
    assert s.identity("h2")["roles"] == ["viewer", "operator"]


def test_run_link_template():
    host = {"name": "box", "services": {"dashboard": "http://box.lan:9000/"}}
    run = {"name": "sweep 01", "dir": "/r/sweep 01/"}
    assert (
        run_link("{service:dashboard}/run/{name}", host, run)
        == "http://box.lan:9000/run/sweep%2001"
    )
    assert run_link("{service:missing}/run/{name}", host, run) is None
    assert run_link(None, host, run) is None


def test_pickup_window_per_claim(store):
    clock = Clock()
    ledger = Ledger(store, clock=clock)
    a = ledger.request("box/gpu0", "a", "agent")
    b = ledger.request("box/gpu0", "b", "agent", pickup_s=7200)
    with pytest.raises(LedgerError, match="pickup"):
        ledger.request("box/gpu0", "c", "agent", pickup_s=10)
    ledger.release(a["id"], "a")
    clock.advance(3600)
    ledger.tick()
    assert ledger.claim(b["id"])["state"] == "offered"
    clock.advance(3601)
    ledger.tick()
    assert ledger.claim(b["id"])["state"] == "skipped"


def test_heartbeat_changes_lease(store):
    clock = Clock()
    ledger = Ledger(store, clock=clock)
    c = ledger.request("box/gpu0", "a", "agent", lease_s=600)
    ledger.heartbeat(c["id"], "a", lease_s=7200)
    clock.advance(4 * 3600 - 10)
    ledger.tick()
    assert ledger.claim(c["id"])["state"] == "held"
    with pytest.raises(LedgerError):
        ledger.heartbeat(c["id"], "a", lease_s=10)
    assert any(e["action"] == "claim.lease" for e in store.events())


def test_estimate_covers_a_chain(store):
    clock = Clock()
    ledger = Ledger(store, clock=clock)
    run = {"dir": "/r/a/", "parser": "p", "state": "running", "finish_at": clock() + 3600}
    store.record_probe("box", {"gpus": [{"index": 0}], "gpu_procs": []}, [run])
    c = ledger.request("box/gpu0", "a", "agent", est_hours=10)
    ledger.bind_run(c["id"], "a", "/r/a")
    end, src = ledger.estimate(ledger.claim(c["id"]))
    assert end == pytest.approx(clock() + 10 * 3600) and "beyond the current run" in src
    ledger.heartbeat(c["id"], "a", est_hours=0.5)
    assert ledger.estimate(ledger.claim(c["id"])) == (clock() + 3600, "run")


def test_api_roles_services_and_runs(tmp_path, store):
    store.add_token(token_hash("t-admin"), "dana", "person", actor="x")
    store.add_token(token_hash("t-agent"), "bot", "agent", actor="x")
    store.add_token(token_hash("t-view"), "intern", "person", actor="x", roles=["viewer"])
    with pytest.raises(ValueError, match="unknown role"):
        store.add_token(token_hash("t-bad"), "x", "agent", actor="x", roles=["root"])
    run = {"dir": "/r/sweep-01/", "name": "sweep-01", "parser": "p", "state": "running"}
    store.record_probe("box", {"gpus": [{"index": 0}], "gpu_procs": []}, [run])
    store.update_host("box", actor="x", run_link="{service:dashboard}/run/{name}")
    app = create_app(store=store, home=tmp_path, collect=False)
    hdr = {n: {"Authorization": f"Bearer t-{n}"} for n in ("admin", "agent", "view")}
    with TestClient(app) as c:
        assert c.get("/api/whoami", headers=hdr["view"]).json() == {
            "identity": "intern",
            "kind": "person",
            "roles": ["viewer"],
        }
        assert c.get("/api/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401
        # viewers read but cannot claim; agents claim but cannot change the registry
        r = c.post("/api/claims", json={"resource": "box/gpu0"}, headers=hdr["view"])
        assert r.status_code == 403
        r = c.post(
            "/api/claims", json={"resource": "box/gpu0", "pickup_min": 120}, headers=hdr["agent"]
        )
        assert r.status_code == 201 and r.json()["pickup_s"] == 7200
        assert c.post("/api/hosts", json={"name": "x"}, headers=hdr["agent"]).status_code == 403
        r = c.patch(
            "/api/hosts/other",
            json={"services": {"dashboard": "http://other:9000"}},
            headers=hdr["admin"],
        )
        assert r.status_code == 200
        names = [h["name"] for h in c.get("/api/hosts?service=dashboard").json()]
        assert names == ["box", "other"]
        assert c.get("/api/hosts?service=metrics").json() == []
        runs = c.get("/api/runs", params={"host": "box", "dir": "/r/sweep-01"}).json()
        assert [r["link"] for r in runs] == ["http://box.lan:9000/run/sweep-01"]
        assert c.get("/api/runs", params={"dir": "/r/none"}).json() == []
        state = c.get("/api/state").json()
        box = next(h for h in state["hosts"] if h["name"] == "box")
        assert box["services"]["dashboard"] and box["runs"][0]["link"].endswith("/run/sweep-01")
