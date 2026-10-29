"""Claims pause while a host or GPU is away; gpu-missing flag; backups."""

import re
import time

import pytest

from labtender import backup as backup_mod
from labtender.db import Store
from labtender.ledger import Ledger, LedgerError
from labtender.names import card_name


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.add_host("box", actor="a")
    s.record_probe("box", {"gpus": [{"index": 0}, {"index": 1}], "gpu_procs": []}, [])
    return s


def test_missing_gpu_pauses_its_claims_and_is_flagged(store):
    clock = Clock()
    ledger = Ledger(store, clock=clock)
    held = ledger.request("box/gpu1", "a", "agent", lease_s=600)
    waiting = ledger.request("box/gpu1", "b", "agent")
    store.record_probe("box", {"gpus": [{"index": 0}], "gpu_procs": []}, [])  # eGPU unplugged
    ledger.reconcile("box", store.snapshot("box"))
    assert [f["kind"] for f in ledger.flags("box/gpu1")] == ["gpu-missing"]
    assert ledger.missing_gpus("box").keys() == {1}
    clock.advance(4 * 3600)
    ledger.tick()
    assert ledger.claim(held["id"])["state"] == "held"
    view = ledger.resource_view("box/gpu1")
    assert view["holders"][0]["frozen"] == "GPU missing" and not view["holders"][0]["late"]
    # Queueing for a GPU that is away is still allowed.
    assert ledger.request("box/gpu1", "c", "agent")["state"] == "queued"
    store.record_probe("box", {"gpus": [{"index": 0}, {"index": 1}], "gpu_procs": []}, [])
    ledger.reconcile("box", store.snapshot("box"))
    assert "gpu-missing" not in [f["kind"] for f in ledger.flags("box/gpu1")]
    assert ledger.claim(waiting["id"])["state"] == "queued"


def test_a_card_that_moves_ends_its_claims_and_is_not_missing(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    clock = Clock()
    ledger = Ledger(store, clock=clock)
    store.add_host("laptop", actor="a")
    store.add_host("tower", actor="a")
    inner = {"index": 0, "uuid": "GPU-inner", "name": "Laptop GPU"}
    egpu = {"index": 1, "uuid": "GPU-egpu", "name": "Desktop GPU"}
    store.record_probe("laptop", {"gpus": [inner, egpu], "gpu_procs": []}, [])
    store.record_probe("tower", {"gpus": [], "gpu_procs": []}, [])
    store.name_card("GPU-egpu", "egpu", actor="a")
    held = ledger.request("laptop/gpu1", "a", "agent", lease_s=600)
    waiting = ledger.request("laptop/gpu1", "b", "agent")
    other = ledger.request("laptop/gpu0", "c", "agent")
    # The external GPU is unplugged and taken to the tower.
    store.record_probe("laptop", {"gpus": [inner], "gpu_procs": []}, [])
    ledger.reconcile("laptop", store.snapshot("laptop"))
    assert ledger.missing_gpus("laptop").keys() == {1}  # away, not yet seen anywhere
    store.record_probe("tower", {"gpus": [dict(egpu, index=0)], "gpu_procs": []}, [])
    ledger.reconcile("laptop", store.snapshot("laptop"))
    assert ledger.missing_gpus("laptop") == {}
    assert "gpu-missing" not in [f["kind"] for f in ledger.flags("laptop/gpu1")]
    moved = [e for e in store.events() if e["action"] == "card.moved"]
    assert moved[0]["detail"]["from"] == "laptop/gpu1" and moved[0]["detail"]["to"] == "tower/gpu0"
    assert moved[0]["detail"]["name"] == "egpu"
    ledger.tick()
    for c in (held, waiting):
        c = ledger.claim(c["id"])
        assert c["state"] == "expired" and c["end_reason"] == "the card moved to tower/gpu0"
    assert ledger.claim(other["id"])["state"] == "held"
    with pytest.raises(LedgerError, match="moved to tower/gpu0"):
        ledger.request("laptop/gpu1", "d", "agent")
    assert ledger.request("tower/gpu0", "d", "agent")["state"] == "held"
    assert store.find_card("egpu")["host"] == "tower" and store.find_card("tower/gpu0")


def test_cards_get_their_own_names_and_can_be_renamed(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("box", actor="a")
    gpus = [{"index": 0, "uuid": "GPU-aaaaaaaa-1"}, {"index": 1, "uuid": "GPU-bbbbbbbb-2"}]
    store.record_probe("box", {"gpus": gpus, "gpu_procs": []}, [])
    own = card_name("GPU-aaaaaaaa-1")
    assert own == card_name("gpu-aaaaaaaa-1") and re.fullmatch(r"[a-z]+-[a-z]+", own)
    assert store.find_card(own)["idx"] == 0
    store.name_card("GPU-aaaaaaaa-1", "first", actor="a")
    with pytest.raises(ValueError, match="already called"):
        store.name_card("GPU-bbbbbbbb-2", "first", actor="a")
    with pytest.raises(ValueError, match="lower case"):
        store.name_card("GPU-bbbbbbbb-2", "Big GPU", actor="a")
    assert store.find_card("GPU-bbbbbbbb")["idx"] == 1
    assert store.name_card("GPU-aaaaaaaa-1", "", actor="a")["name"] == own


def test_two_cards_with_the_same_made_name_are_told_apart(tmp_path, monkeypatch):
    from labtender import db

    monkeypatch.setattr(db, "card_name", lambda uuid: "calm-heron")
    store = Store(tmp_path / "t.sqlite")
    store.add_host("box", actor="a")
    gpus = [{"index": 0, "uuid": "GPU-1"}, {"index": 1, "uuid": "GPU-2"}]
    store.record_probe("box", {"gpus": gpus, "gpu_procs": []}, [])
    assert [c["name"] for c in store.cards()] == ["calm-heron", "calm-heron-2"]
    assert store.name_card("GPU-2", "", actor="a")["name"] == "calm-heron-2"


def test_a_held_back_card_refuses_agents_but_not_people(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store, clock=Clock())
    store.add_host("box", actor="a")
    store.add_host("mac", actor="a")
    store.record_probe("box", {"gpus": [{"index": 0, "uuid": "GPU-slow"}], "gpu_procs": []}, [])
    store.record_probe("mac", {"gpus": [{"index": 0, "unified": True}], "gpu_procs": []}, [])
    early = ledger.request("box/gpu0", "a", "agent", lease_s=600)
    store.set_card("GPU-slow", hold="slow for long jobs", actor="p")
    with pytest.raises(LedgerError, match="held back from agents .slow for long jobs."):
        ledger.request("box/gpu0", "b", "agent")
    assert ledger.request("box/gpu0", "a", "agent")["id"] == early["id"]  # already in, stays in
    assert ledger.request("box/gpu0", "dana", "person")["state"] == "queued"
    # A GPU with no UUID is a card by its place, so it can be held back too.
    mac = store.card_at("mac", 0)
    assert mac["uuid"] == "local:mac/gpu0" and mac["name"]
    store.set_card(mac["uuid"], hold="not for agents", actor="p")
    with pytest.raises(LedgerError, match="held back"):
        ledger.request("mac/gpu0", "b", "agent")
    store.set_card("GPU-slow", hold=None, actor="p")
    assert ledger.request("box/gpu0", "b", "agent")["state"] == "queued"
    assert [e["action"] for e in store.events()].count("card.set") == 3
    with pytest.raises(ValueError, match="bandwidth"):
        store.set_card("GPU-slow", bandwidth_gbs=-1, actor="p")


def test_unreachable_host_pauses_claims(store):
    clock = Clock()
    ledger = Ledger(store, clock=clock)
    c = ledger.request("box/gpu0", "a", "agent", lease_s=600)
    store.conn.execute("UPDATE host SET last_seen = ? WHERE name = 'box'", (time.time() - 3600,))
    clock.advance(3 * 3600)
    ledger.tick()
    assert ledger.claim(c["id"])["state"] == "held"
    assert ledger.frozen(ledger.claim(c["id"])) == "host unreachable"
    # Back online: the lease runs again from the last pause.
    store.conn.execute("UPDATE host SET last_seen = ? WHERE name = 'box'", (time.time(),))
    clock.advance(1300)
    ledger.tick()
    assert ledger.claim(c["id"])["state"] == "expired"


def test_daily_backups_rotate(store, tmp_path):
    folder = tmp_path / "backups"
    now = time.time()
    first = backup_mod.backup_if_due(store, folder, now)
    assert first and first.exists()
    assert backup_mod.backup_if_due(store, folder, now + 60) is None
    for day in range(1, 10):
        p = backup_mod.backup_if_due(store, folder, now + day * 86400 + 1)
        import os

        os.utime(p, (now + day * 86400 + 1, now + day * 86400 + 1))
    assert len(list(folder.glob("labtender-*.sqlite"))) == backup_mod.KEEP
    restored = Store(sorted(folder.glob("labtender-*.sqlite"))[-1])
    assert [h["name"] for h in restored.hosts()] == ["box"]
