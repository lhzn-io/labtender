"""Health checks for host services, against a real local HTTP server."""

import asyncio
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from labtender.collector import Collector
from labtender.db import Store
from labtender.ledger import Ledger
from labtender.metrics import exposition
from labtender.server import create_app

RELEASE = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/hang":
            RELEASE.wait(5)  # accepts the connection, never answers in time
        code = {"/ok": 200, "/moved": 302, "/broken": 500}.get(self.path, 404)
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def web():
    RELEASE.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    RELEASE.set()
    srv.shutdown()


@pytest.fixture
def lab(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    col = Collector(store, tmp_path, ledger)
    col.health_timeout_s = 0.5
    return store, ledger, col


def test_health_paths_are_stored_and_validated(tmp_path, web):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("box", actor="a", services={"dashboard": web}, health={"dashboard": "/ok"})
    assert store.host("box")["health"] == {"dashboard": "/ok"}
    with pytest.raises(ValueError, match="start with /"):
        store.update_host("box", actor="a", health={"dashboard": "ok"})
    with pytest.raises(ValueError, match="no service"):
        store.update_host("box", actor="a", health={"other": "/x"})
    h = store.update_host("box", actor="a", services={"api": web}, health={"api": "/moved"})
    assert h["health"] == {"dashboard": "/ok", "api": "/moved"}
    h = store.update_host("box", actor="a", services={"dashboard": ""})  # removing a service
    assert h["health"] == {"api": "/moved"}
    h = store.update_host("box", actor="a", health={"api": ""})
    assert h["health"] == {} and h["services"] == {"api": web}


def test_old_database_gains_the_health_column(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE host (name TEXT PRIMARY KEY, transport TEXT NOT NULL DEFAULT 'ssh',"
        " ssh TEXT, lifecycle TEXT NOT NULL DEFAULT 'static', expires_at REAL,"
        " interval_s REAL NOT NULL DEFAULT 45, accepts TEXT NOT NULL DEFAULT '[]',"
        " labels TEXT NOT NULL DEFAULT '{}', added_at REAL NOT NULL, last_seen REAL,"
        " last_error TEXT)"
    )
    conn.execute("INSERT INTO host (name, added_at) VALUES ('box', 1)")
    conn.commit()
    conn.close()
    store = Store(path)
    h = store.host("box")
    assert h["health"] == {} and h["services"] == {}


def test_up_down_flag_and_audit(lab, web):
    store, ledger, col = lab
    store.add_host("box", actor="a", services={"dashboard": web}, health={"dashboard": "/ok"})
    out = asyncio.run(col.check_services("box"))
    assert out == {"dashboard": (True, "HTTP 200")}
    assert store.service_health("box")["dashboard"]["ok"] is True
    assert ledger.flags("box") == []
    store.update_host("box", actor="a", health={"dashboard": "/broken"})
    asyncio.run(col.check_services("box"))
    (flag,) = ledger.flags("box")
    assert flag["kind"] == "service-down" and flag["detail"] == "dashboard: HTTP 500"
    changed = store.service_health("box")["dashboard"]["changed_at"]
    asyncio.run(col.check_services("box"))  # still down: same flag, same change time
    assert store.service_health("box")["dashboard"]["changed_at"] == changed
    store.update_host("box", actor="a", health={"dashboard": "/moved"})
    asyncio.run(col.check_services("box"))
    assert ledger.flags("box") == []
    actions = [e["action"] for e in store.events(50)]
    assert "flag.service-down" in actions and "flag.service-down.cleared" in actions
    assert "service.down" in actions and "service.up" in actions


def test_a_hung_service_times_out(lab, web):
    store, ledger, col = lab
    store.add_host("box", actor="a", services={"dashboard": web}, health={"dashboard": "/hang"})
    t0 = time.monotonic()
    ((ok, detail),) = asyncio.run(col.check_services("box")).values()
    assert not ok and "no answer" in detail and time.monotonic() - t0 < 3
    assert "no answer" in ledger.flags("box")[0]["detail"]


def test_a_full_accept_backlog_times_out(lab):
    store, ledger, col = lab
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(0)  # never accepted: like a server whose backlog is full
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        store.add_host("box", actor="a", services={"dashboard": url}, health={"dashboard": "/x"})
        ((ok, detail),) = asyncio.run(col.check_services("box")).values()
        assert not ok
    finally:
        sock.close()


def test_refused_connection_is_down(lab):
    store, ledger, col = lab
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    store.add_host(
        "box",
        actor="a",
        services={"dashboard": f"http://127.0.0.1:{port}"},
        health={"dashboard": "/"},
    )
    ((ok, detail),) = asyncio.run(col.check_services("box")).values()
    assert not ok and detail


def test_checks_never_block_the_probe_loop(lab, web, monkeypatch):
    store, ledger, col = lab
    store.add_host("box", actor="a", services={"dashboard": web}, health={"dashboard": "/hang"})
    polled = []

    async def fake_poll(name):
        polled.append(time.monotonic())
        col._stop.set()
        return {}

    monkeypatch.setattr(col, "poll_once", fake_poll)

    async def go():
        t0 = time.monotonic()
        await col._host_loop("box")
        assert polled and polled[0] - t0 < 0.2  # the probe did not wait for the hung check
        await asyncio.gather(*col._health.values())

    asyncio.run(go())
    assert store.service_health("box")["dashboard"]["ok"] is False


def test_only_one_round_at_a_time(lab, web):
    store, ledger, col = lab
    store.add_host("box", actor="a", services={"dashboard": web}, health={"dashboard": "/hang"})

    async def go():
        col._start_health("box")
        first = col._health["box"]
        col._start_health("box")
        assert col._health["box"] is first
        await first

    asyncio.run(go())


def test_api_metrics_and_pruning(lab, web):
    store, ledger, col = lab
    store.add_host(
        "box",
        actor="a",
        services={"dashboard": web, "docs": web},
        health={"dashboard": "/ok", "docs": "/broken"},
    )
    asyncio.run(col.check_services("box"))
    text = exposition(store, ledger)
    assert 'labtender_service_up{host="box",service="dashboard"} 1' in text
    assert 'labtender_service_up{host="box",service="docs"} 0' in text
    app = create_app(store=store, home=col.home, collect=False, ledger=ledger)
    with TestClient(app) as http:
        (h,) = http.get("/api/hosts").json()
        assert h["health"] == {"dashboard": "/ok", "docs": "/broken"}
        assert h["service_health"]["docs"]["ok"] is False
        (hv,) = http.get("/api/state").json()["hosts"]
        assert hv["host_flags"][0]["kind"] == "service-down"
    store.update_host("box", actor="a", health={"docs": ""})
    asyncio.run(col.check_services("box"))
    assert list(store.service_health("box")) == ["dashboard"] and ledger.flags("box") == []


def test_removing_the_last_health_path_clears_results_and_flag(lab, web, monkeypatch):
    store, ledger, col = lab
    store.add_host("box", actor="a", services={"dashboard": web}, health={"dashboard": "/broken"})
    asyncio.run(col.check_services("box"))
    assert ledger.flags("box") and store.service_health("box")
    store.update_host("box", actor="a", health={"dashboard": ""})

    async def fake_poll(name):
        col._stop.set()
        return {}

    monkeypatch.setattr(col, "poll_once", fake_poll)

    async def one_loop():
        await col._host_loop("box")
        await asyncio.gather(*col._health.values())

    asyncio.run(one_loop())
    assert store.service_health("box") == {} and ledger.flags("box") == []
    assert "flag.service-down.cleared" in [e["action"] for e in store.events(20)]
    col._health.clear()
    col._stop.clear()
    asyncio.run(one_loop())
    assert col._health == {}  # nothing left to check: no more rounds


# ------------------------------------------------------------ fallback addresses


def dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def test_fallbacks_are_stored_validated_and_follow_their_service(tmp_path, web):
    store = Store(tmp_path / "t.sqlite")
    store.add_host(
        "box", actor="a", services={"dashboard": web}, fallbacks={"dashboard": ["http://vpn:1"]}
    )
    assert store.host("box")["fallbacks"] == {"dashboard": ["http://vpn:1"]}
    with pytest.raises(ValueError, match="http"):
        store.update_host("box", actor="a", fallbacks={"dashboard": ["vpn:1"]})
    with pytest.raises(ValueError, match="no service"):
        store.update_host("box", actor="a", fallbacks={"other": ["http://x"]})
    h = store.update_host("box", actor="a", fallbacks={"dashboard": ["http://a", "http://b"]})
    assert h["fallbacks"] == {"dashboard": ["http://a", "http://b"]}  # replaced, in order
    assert store.update_host("box", actor="a", fallbacks={"dashboard": []})["fallbacks"] == {}
    store.update_host("box", actor="a", fallbacks={"dashboard": ["http://a"]})
    h = store.update_host("box", actor="a", services={"dashboard": ""})
    assert h["fallbacks"] == {}


def test_a_service_away_from_home_is_found_at_its_fallback(lab, web):
    store, ledger, col = lab
    home = dead_url()
    store.add_host(
        "box",
        actor="a",
        services={"dashboard": home},
        health={"dashboard": "/ok"},
        fallbacks={"dashboard": [dead_url(), web]},
        run_link="{service:dashboard}/run/{name}",
    )
    assert asyncio.run(col.check_services("box")) == {"dashboard": (True, "HTTP 200")}
    c = store.service_health("box")["dashboard"]
    assert c["ok"] and c["base"] == web and c["url"] == web + "/ok"
    assert store.active_services(store.host("box")) == {"dashboard": web}
    assert ledger.flags("box") == []
    store.record_probe(
        "box", {"gpus": []}, [{"dir": "/r/a/", "name": "a", "parser": "p", "state": "running"}]
    )
    app = create_app(store=store, home=col.home, collect=False, ledger=ledger)
    with TestClient(app) as http:
        (h,) = http.get("/api/hosts").json()
        assert h["services"] == {"dashboard": home} and h["services_active"] == {"dashboard": web}
        (run,) = http.get("/api/runs").json()
        assert run["link"] == f"{web}/run/a"
        (hv,) = http.get("/api/state").json()["hosts"]
        assert hv["runs"][0]["link"] == f"{web}/run/a"


def test_moving_between_addresses_is_audited(lab, web):
    store, ledger, col = lab
    store.add_host(
        "box",
        actor="a",
        services={"dashboard": web},
        health={"dashboard": "/ok"},
        fallbacks={"dashboard": [web + "/"]},
    )
    asyncio.run(col.check_services("box"))
    assert store.service_health("box")["dashboard"]["base"] == web
    store.update_host("box", actor="a", services={"dashboard": dead_url()})
    asyncio.run(col.check_services("box"))
    assert store.service_health("box")["dashboard"]["base"] == web + "/"
    moved = [e for e in store.events(20) if e["action"] == "service.moved"]
    assert moved and moved[0]["detail"]["address"] == web + "/"


def test_down_everywhere_says_how_many_addresses_were_tried(lab):
    store, ledger, col = lab
    home = dead_url()
    store.add_host(
        "box",
        actor="a",
        services={"dashboard": home},
        health={"dashboard": "/"},
        fallbacks={"dashboard": [dead_url(), dead_url()]},
    )
    ((ok, detail),) = asyncio.run(col.check_services("box")).values()
    assert not ok and detail.endswith("(and at 2 fallbacks)")
    assert store.active_services(store.host("box")) == {"dashboard": home}  # primary when down
    assert "fallbacks" in ledger.flags("box")[0]["detail"]


def test_cli_fallback_pairs():
    from labtender.cli import _fallbacks

    assert _fallbacks(None) is None
    assert _fallbacks(["dashboard=http://a", "dashboard=http://b", "docs="]) == {
        "dashboard": ["http://a", "http://b"],
        "docs": [],
    }
    with pytest.raises(ValueError):
        _fallbacks(["dashboard"])


def test_an_optional_service_is_off_not_flagged(lab, web):
    store, ledger, collector = lab
    dead = "http://127.0.0.1:9"  # nothing listens on the discard port
    store.add_host(
        "box", actor="a", services={"hub": dead, "dash": web}, health={"hub": "/ok", "dash": "/ok"}
    )
    with pytest.raises(ValueError, match="no service"):
        store.update_host("box", actor="a", optional={"other": True})
    with pytest.raises(ValueError, match="no host"):
        store.update_host("nobox", actor="a", optional={"hub": True})
    h = store.update_host("box", actor="a", optional={"hub": True})
    assert h["optional"] == ["hub"]
    results = asyncio.run(collector.check_services("box"))
    assert results["hub"][0] is False and results["dash"][0] is True
    assert not [f for f in ledger.flags("box") if f["kind"] == "service-down"]
    store.update_host("box", actor="a", optional={"hub": False})
    asyncio.run(collector.check_services("box"))
    assert [f for f in ledger.flags("box") if f["kind"] == "service-down"]
    store.update_host("box", actor="a", optional={"hub": True})
    assert store.update_host("box", actor="a", services={"hub": ""})["optional"] == []
