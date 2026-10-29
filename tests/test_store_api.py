import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from labtender.db import Store
from labtender.server import create_app, token_hash


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.sqlite")


def test_registry_and_audit(store):
    store.add_host("box", actor="alice")
    with pytest.raises(ValueError):
        store.add_host("cloud1", actor="alice", lifecycle="ephemeral")
    store.add_host("cloud1", actor="alice", lifecycle="ephemeral", expires_at=time.time() - 1)
    assert store.expire_hosts() == ["cloud1"]
    assert [h["name"] for h in store.hosts()] == ["box"]
    actions = [e["action"] for e in store.events()]
    assert actions == ["host.remove", "host.add", "host.add"]


def test_audit_is_append_only(store):
    store.add_host("box", actor="alice")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM event")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE event SET actor = 'mallory'")


def test_interval_floor(store):
    with pytest.raises(ValueError):
        store.add_host("box", actor="alice", interval_s=5)


def test_run_state_changes_are_audited(store):
    store.add_host("box", actor="alice")
    doc = {"gpus": [{"index": 0, "util_pct": 50}], "gpu_procs": []}
    run = {"dir": "/r/a", "parser": "run-wrapper", "state": "running"}
    store.record_probe("box", doc, [run])
    store.record_probe("box", doc, [run])
    store.record_probe("box", doc, [dict(run, state="done")])
    assert [e["action"] for e in store.events() if e["action"].startswith("run.")] == [
        "run.done",
        "run.running",
    ]
    assert store.runs("box")[0]["state"] == "done"
    store.record_probe("box", doc, [dict(run, dir="/r/b")])
    store.record_probe("box", doc, [])
    states = {r["dir"]: r["state"] for r in store.runs("box")}
    assert states == {"/r/a": "done", "/r/b": "gone"}


def test_api(tmp_path, store):
    store.add_token(token_hash("lt_secret"), "alice", "person", actor="test")
    app = create_app(store=store, home=tmp_path, collect=False)
    with TestClient(app) as c:
        assert c.get("/api/state").json()["hosts"] == []
        assert c.post("/api/hosts", json={"name": "box"}).status_code == 401
        hdr = {"Authorization": "Bearer lt_secret"}
        r = c.post("/api/hosts", json={"name": "box", "ssh": "box.lan"}, headers=hdr)
        assert r.status_code == 201 and r.json()["ssh"] == "box.lan"
        assert c.post("/api/hosts", json={"name": "box"}, headers=hdr).status_code == 409
        assert c.post("/api/hosts", json={"name": "bad name"}, headers=hdr).status_code == 422
        hosts = c.get("/api/state").json()["hosts"]
        assert hosts[0]["name"] == "box" and hosts[0]["online"] is False
        assert "labtender" in c.get("/").text
        assert "/api/state" in c.get("/compact").text
        assert c.delete("/api/hosts/box", headers=hdr).status_code == 200
        assert c.get("/api/events").json()[0]["actor"] == "alice"


def test_runs_held_by_one_process_show_once():
    from labtender.server import merge_twins

    runs = [
        {"dir": "/r/logs/a/", "state": "running", "pid": 7, "last_log_at": 100.0},
        {"dir": "/r/old/", "state": "running", "pid": None, "last_log_at": 50.0},
        {"dir": "/r/wrap/a/", "state": "running", "pid": 7, "last_log_at": 90.0},
        {"dir": "/r/wrap/b/", "state": "exited", "pid": 7, "last_log_at": 40.0},
    ]
    out = merge_twins(runs)
    assert [r["dir"] for r in out] == ["/r/logs/a/", "/r/old/", "/r/wrap/b/"]
    assert out[0]["also"] == ["/r/wrap/a/"] and "also" not in out[1]


def test_gauges_list_every_gpu_with_its_history(tmp_path):
    from fastapi.testclient import TestClient

    from labtender.db import Store
    from labtender.server import create_app

    store = Store(tmp_path / "t.sqlite")
    app = create_app(store=store, home=tmp_path, collect=False)
    store.add_host("box", actor="t")
    store.add_host("laptop", actor="t")
    gpu = {
        "index": 0,
        "uuid": "GPU-x1",
        "name": "Big GPU",
        "util_pct": 80.0,
        "mem_used_mib": 4096,
        "mem_total_mib": 16384,
        "power_w": 250.0,
        "power_limit_w": 360.0,
        "temp_c": 71.0,
    }
    store.record_probe(
        "box", {"gpus": [gpu], "gpu_procs": [{"card": 0, "pid": 1, "comm": "x"}]}, []
    )
    store.record_probe("box", {"gpus": [gpu], "gpu_procs": []}, [])
    store.record_probe("laptop", {"gpus": [{"index": 0}, {"index": 1}], "gpu_procs": []}, [])
    store.record_probe("laptop", {"gpus": [{"index": 0}], "gpu_procs": []}, [])  # one unplugged
    r = TestClient(app).get("/api/gauges").json()
    box = [g for g in r["gpus"] if g["host"] == "box"][0]
    assert box["card_name"] and box["model"] == "Big GPU" and box["online"] and not box["away"]
    assert box["power_limit_w"] == 360.0 and len(box["history"]) == 2
    assert box["history"][0]["mem_total"] == 16384 and box["holders"] == []
    away = [g for g in r["gpus"] if g["host"] == "laptop" and g["away"]]
    assert [g["index"] for g in away] == [1]
    assert TestClient(app).get("/gauges").status_code == 200


def test_a_host_reached_through_a_roaming_network_says_so(tmp_path):
    from fastapi.testclient import TestClient

    from labtender.db import Store
    from labtender.server import create_app

    (tmp_path / "networks.toml").write_text('[roaming]\nvpn = ["10.8.0.0/24"]\n')
    store = Store(tmp_path / "t.sqlite")
    app = create_app(store=store, home=tmp_path, collect=False)
    for name, addr in (("laptop", "10.8.0.2"), ("tower", "192.0.2.10"), ("box", None)):
        store.add_host(name, actor="t")
        store.record_probe(name, {"host": {"reached_at": addr}, "gpus": [], "gpu_procs": []}, [])
    hosts = {h["name"]: h["roaming"] for h in TestClient(app).get("/api/state").json()["hosts"]}
    assert hosts == {"laptop": "vpn", "tower": None, "box": None}
