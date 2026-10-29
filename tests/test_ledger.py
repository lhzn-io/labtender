import pytest
from fastapi.testclient import TestClient

from labtender.db import Store
from labtender.ledger import PICKUP_S, Ledger, LedgerError, parse_resource
from labtender.server import create_app, token_hash


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.add_host("box", actor="admin")
    s.add_host("slowbox", actor="admin", accepts=["nice-to-have"])
    s.record_probe("box", {"gpus": [{"index": 0}, {"index": 1}], "gpu_procs": []}, [])
    return s


@pytest.fixture
def ledger(store, clock):
    return Ledger(store, clock=clock)


def states(ledger, res="box/gpu1"):
    return {c["owner"]: c["state"] for c in ledger.claims(res)}


def test_parse_resource():
    assert parse_resource("box/gpu1") == ("box", 1)
    assert parse_resource("box") == ("box", None)
    with pytest.raises(LedgerError):
        parse_resource("box/card1")


def test_two_agents_and_a_person_queue_for_one_card(ledger, clock):
    a = ledger.request("box/gpu1", "agent-a", "agent", est_hours=2)
    clock.advance(1)
    b = ledger.request("box/gpu1", "agent-b", "agent", est_hours=1)
    clock.advance(1)
    p = ledger.request("box/gpu1", "dana", "person", tier="critical", est_hours=3)
    assert states(ledger) == {"agent-a": "held", "agent-b": "queued", "dana": "queued"}

    view = ledger.resource_view("box/gpu1")
    assert [c["owner"] for c in view["holders"]] == ["agent-a"]
    # The critical claim moves ahead of the earlier normal one, but never displaces the holder.
    assert [c["owner"] for c in view["queue"]] == ["dana", "agent-b"]
    assert view["free_at"] == pytest.approx(a["held_at"] + 2 * 3600)
    assert view["queue"][1]["est_start"] == pytest.approx(view["free_at"] + 3 * 3600)

    ledger.release(a["id"], "agent-a")
    assert states(ledger)["dana"] == "offered"
    ledger.heartbeat(p["id"], "dana")
    assert states(ledger) == {"dana": "held", "agent-b": "queued"}
    ledger.release(p["id"], "dana")
    # Claiming again takes up an offer too.
    assert ledger.request("box/gpu1", "agent-b", "agent")["id"] == b["id"]
    assert states(ledger) == {"agent-b": "held"}


def test_request_is_idempotent_per_owner(ledger):
    a = ledger.request("box/gpu0", "agent-a", "agent")
    assert ledger.request("box/gpu0", "agent-a", "agent")["id"] == a["id"]
    assert len(ledger.claims("box/gpu0")) == 1


def test_untaken_offer_is_skipped(ledger, clock):
    a = ledger.request("box/gpu1", "agent-a", "agent")
    ledger.request("box/gpu1", "agent-b", "agent")
    ledger.request("box/gpu1", "agent-c", "agent")
    ledger.release(a["id"], "agent-a")
    clock.advance(PICKUP_S + 1)
    ledger.tick()
    assert states(ledger) == {"agent-c": "offered"}


def test_lease_expiry_and_run_binding(ledger, store, clock):
    a = ledger.request("box/gpu0", "agent-a", "agent", lease_s=600)
    clock.advance(900)
    ledger.tick()
    assert ledger.resource_view("box/gpu0")["holders"][0]["late"] is True
    clock.advance(400)
    ledger.tick()
    assert ledger.claim(a["id"])["state"] == "expired"

    run = {"dir": "/r/x", "parser": "run-wrapper", "state": "running", "finish_at": 5e6}
    store.record_probe("box", {"gpus": [{"index": 0}], "gpu_procs": []}, [run])
    b = ledger.request("box/gpu0", "agent-b", "agent", lease_s=600)
    ledger.bind_run(b["id"], "agent-b", "/r/x")
    clock.advance(3 * 3600)
    ledger.tick()
    assert ledger.claim(b["id"])["state"] == "held"
    assert ledger.estimate(ledger.claim(b["id"])) == (5e6, "run")


def test_bind_waits_for_a_run_the_probe_has_not_seen(ledger, store, clock):
    # A wrapper binds right after starting a run, usually before the next probe sees the folder.
    a = ledger.request("box/gpu0", "agent-a", "agent", lease_s=600)
    c = ledger.bind_run(a["id"], "agent-a", "/r/new/")
    assert c["pending_run"] == "/r/new/" and c["run_key"] is None
    clock.advance(1500)  # past twice the lease, but the bind is still waiting
    ledger.tick()
    assert ledger.claim(a["id"])["state"] == "held"
    run = {"dir": "/r/new", "parser": "run-wrapper", "state": "running", "finish_at": 5e6}
    store.record_probe("box", {"gpus": [{"index": 0}], "gpu_procs": []}, [run])
    ledger.tick()
    c = ledger.claim(a["id"])
    assert c["run_key"] == "box|/r/new" and c["pending_run"] is None
    assert any(e["action"] == "claim.bind" and e["detail"].get("waited") for e in store.events())


def test_a_bind_that_never_finds_its_run_falls_back_to_the_lease(ledger, store, clock):
    a = ledger.request("box/gpu0", "agent-a", "agent", lease_s=600)
    ledger.bind_run(a["id"], "agent-a", "/r/never")
    clock.advance(1801)
    ledger.tick()
    c = ledger.claim(a["id"])
    assert c["state"] == "held" and c["pending_run"] is None
    assert any(e["action"] == "claim.bind-expired" for e in store.events())
    clock.advance(1201)  # then the ordinary lease applies, counted from when the wait ended
    ledger.tick()
    assert ledger.claim(a["id"])["state"] == "expired"


def test_sharing_needs_a_holder_and_is_recorded(ledger, store):
    with pytest.raises(LedgerError):
        ledger.request("box/gpu0", "agent-a", "agent", share="dana agreed")
    ledger.request("box/gpu0", "agent-a", "agent")
    ledger.request("box/gpu0", "agent-b", "agent", share="agent-a and dana agreed, 2 GiB job")
    assert states(ledger, "box/gpu0") == {"agent-a": "held", "agent-b": "held"}
    assert any(e["detail"].get("share") for e in store.events() if e["action"] == "claim.held")


def test_host_tier_rule_and_unknown_card(ledger):
    with pytest.raises(LedgerError, match="nice-to-have"):
        ledger.request("slowbox", "agent-a", "agent")
    assert ledger.request("slowbox", "agent-a", "agent", tier="nice-to-have")["state"] == "held"
    with pytest.raises(LedgerError, match="no GPU 7"):
        ledger.request("box/gpu7", "agent-a", "agent")


def test_release_rules(ledger):
    a = ledger.request("box/gpu0", "agent-a", "agent")
    with pytest.raises(LedgerError, match="belongs to"):
        ledger.release(a["id"], "agent-b")
    with pytest.raises(LedgerError, match="reason"):
        ledger.release(a["id"], "dana", force=True)
    assert ledger.release(a["id"], "dana", force=True, reason="run finished")["state"] == "released"
    with pytest.raises(LedgerError, match="already"):
        ledger.release(a["id"], "agent-a")


def test_history_scales_estimates(ledger, clock):
    for _ in range(3):
        c = ledger.request("box/gpu0", "agent-a", "agent", est_hours=1)
        clock.advance(2 * 3600)  # always takes twice as long as estimated
        ledger.release(c["id"], "agent-a")
    c = ledger.request("box/gpu0", "agent-a", "agent", est_hours=1)
    end, src = ledger.estimate(c)
    assert end == pytest.approx(c["held_at"] + 2 * 3600) and "history" in src


def test_departed_host_ends_claims(ledger, store):
    c = ledger.request("box/gpu0", "agent-a", "agent")
    store.remove_host("box", actor="admin")
    ledger.tick()
    assert ledger.claim(c["id"])["state"] == "expired"


def test_reconcile_flags(ledger, store, clock):
    busy = {
        "gpus": [{"index": 0, "util_pct": 99}, {"index": 1, "util_pct": 0}],
        "gpu_procs": [{"pid": 7, "comm": "sim", "card": 0}],
    }
    ledger.reconcile("box", busy)
    assert [(f["resource"], f["kind"]) for f in ledger.flags()] == [("box/gpu0", "unclaimed-use")]
    ledger.request("box/gpu0", "agent-a", "agent")
    c1 = ledger.request("box/gpu1", "agent-b", "agent")
    clock.advance(1800)
    ledger.reconcile("box", busy)
    assert [(f["resource"], f["kind"]) for f in ledger.flags()] == [("box/gpu1", "idle-claim")]
    ledger.release(c1["id"], "agent-b")
    unpinned = dict(busy, gpu_procs=busy["gpu_procs"] + [{"pid": 9, "comm": "x", "card": None}])
    ledger.reconcile("box", unpinned)
    kinds = {(f["resource"], f["kind"]) for f in ledger.flags()}
    assert kinds == {("box/gpu0", "unpinned"), ("box/gpu1", "unpinned")}
    actions = [e["action"] for e in store.events() if e["action"].startswith("flag.")]
    assert "flag.unclaimed-use.cleared" in actions and "flag.idle-claim" in actions


def test_api_claims(tmp_path, store, clock):
    for name, kind in (("agent-a", "agent"), ("agent-b", "agent"), ("dana", "person")):
        store.add_token(token_hash("t-" + name), name, kind, actor="admin")
    app = create_app(store=store, home=tmp_path, collect=False, ledger=Ledger(store, clock))
    hdr = {n: {"Authorization": f"Bearer t-{n}"} for n in ("agent-a", "agent-b", "dana")}
    with TestClient(app) as c:
        assert c.get("/api/whoami", headers=hdr["dana"]).json()["kind"] == "person"
        a = c.post(
            "/api/claims", json={"resource": "box/gpu1", "est_hours": 1}, headers=hdr["agent-a"]
        ).json()
        b = c.post("/api/claims", json={"resource": "box/gpu1"}, headers=hdr["agent-b"]).json()
        assert (a["state"], b["state"]) == ("held", "queued")
        r = c.post(f"/api/claims/{a['id']}/release", json={}, headers=hdr["agent-b"])
        assert r.status_code == 409 and "belongs to" in r.json()["detail"]
        r = c.post(
            f"/api/claims/{a['id']}/release",
            json={"force": True, "reason": "x"},
            headers=hdr["agent-b"],
        )
        assert r.status_code == 403
        r = c.post(
            f"/api/claims/{a['id']}/release",
            json={"force": True, "reason": "done"},
            headers=hdr["dana"],
        )
        assert r.json()["state"] == "released"
        view = c.get("/api/resources/box/gpu1").json()
        assert view["queue"][0]["owner"] == "agent-b" and view["queue"][0]["state"] == "offered"
        gpu1 = c.get("/api/state").json()["hosts"][0]["gpus"][1]
        assert gpu1["ledger"]["queue"][0]["owner"] == "agent-b"
        r = c.post("/api/claims", json={"resource": "box/gpu9"}, headers=hdr["dana"])
        assert r.status_code == 409


def test_claim_changes_reconcile_at_once(ledger, store, clock):
    busy = {
        "gpus": [{"index": 0, "util_pct": 99}, {"index": 1, "util_pct": 0}],
        "gpu_procs": [{"pid": 7, "comm": "sim", "card": 0}],
    }
    store.record_probe("box", busy, [])
    ledger.reconcile("box", store.snapshot("box"))
    assert [f["kind"] for f in ledger.flags("box/gpu0")] == ["unclaimed-use"]
    c = ledger.request("box/gpu0", "agent-a", "agent")  # no probe in between
    assert ledger.flags("box/gpu0") == []
    ledger.release(c["id"], "agent-a")
    assert [f["kind"] for f in ledger.flags("box/gpu0")] == ["unclaimed-use"]
    c = ledger.request("box/gpu0", "agent-a", "agent", lease_s=600)
    clock.advance(1300)
    ledger.tick()  # the claim expires: the flag comes back without waiting for a probe
    assert ledger.claim(c["id"])["state"] == "expired"
    assert [f["kind"] for f in ledger.flags("box/gpu0")] == ["unclaimed-use"]


def test_a_run_bound_while_queued_takes_up_the_offer(ledger, store, clock):
    first = ledger.request("box/gpu0", "a", "agent", lease_s=600)
    waiting = ledger.request("box/gpu0", "b", "agent", pickup_s=900)
    ledger.bind_run(waiting["id"], "b", "/runs/next")  # started early, before its turn
    ledger.release(first["id"], "a")
    c = ledger.claim(waiting["id"])
    assert c["state"] == "held" and c["pending_run"] == "/runs/next"
    clock.advance(1000)  # past the pickup window: nothing lapses
    ledger.tick()
    assert ledger.claim(waiting["id"])["state"] == "held"
    held = [e for e in store.events() if e["action"] == "claim.held"]
    assert any(e["actor"] == "labtender" and e["detail"]["claim"] == waiting["id"] for e in held)


def test_binding_an_offered_claim_takes_it_up(ledger, store, clock):
    first = ledger.request("box/gpu0", "a", "agent", lease_s=600)
    waiting = ledger.request("box/gpu0", "b", "agent")
    ledger.release(first["id"], "a")
    assert ledger.claim(waiting["id"])["state"] == "offered"
    ledger.bind_run(waiting["id"], "b", "/runs/next")
    assert ledger.claim(waiting["id"])["state"] == "held"
