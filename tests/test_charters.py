"""Charters: pre-approval limits, purser approval, the reaper, failures and the orphan sweep."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from labtender.charter import CharterError, Charters
from labtender.db import Store
from labtender.ledger import Ledger
from labtender.server import create_app, token_hash
from labtender.yachts.fake import FakePlugin

PERSON = ("dana", "person", ["viewer", "operator", "admin"])
PURSER = ("dana", "person", ["viewer", "operator", "admin", "purser"])
AGENT = ("sweep-agent", "agent", ["viewer", "operator"])


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60


def profile(tmp_path, name="small", rate=0.5, hours=4, idle=20, extra=""):
    d = tmp_path / "charters"
    d.mkdir(exist_ok=True)
    (d / f"{name}.toml").write_text(
        f'name = "{name}"\nplugin = "fake"\naccelerators = "L4:1"\n'
        f"fake_rate_usd_h = {rate}\nmax_hours = {hours}\nidle_min = {idle}\n{extra}"
    )


@pytest.fixture
def lab(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    clock = Clock()
    fake = FakePlugin()
    ch = Charters(store, ledger, tmp_path, clock=clock, plugins={"fake": fake})
    profile(tmp_path)
    profile(tmp_path, "big", rate=3.0, hours=8, idle=30)
    return ch, store, ledger, clock, fake


def ask(ch, name="small", who=AGENT, **kw):
    q = asyncio.run(ch.quote(name))
    return ch.request(name, q["offers"][0], *who, **kw)


def step(ch):
    asyncio.run(ch.step(wait=True))


def test_small_charter_is_pre_approved_and_goes_live(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    assert c["state"] == "approved" and c["approval"] == "pre-approved"
    assert c["worst_usd"] == 2.0 and c["rate_usd_h"] == 0.5  # cheapest offer chosen
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "live" and c["host"] == f"yacht-{c['id']}"
    assert c["ref"].startswith(ch.prefix) and c["ref"] in fake.machines
    h = store.host(c["host"])
    assert h["lifecycle"] == "charter" and h["labels"]["rate_usd_h"] == "0.5"
    (claim_id,) = c["claims"]
    claim = ledger.claim(claim_id)
    assert claim["state"] == "held" and claim["owner"] == "sweep-agent"
    assert claim["resource"] == f"{c['host']}/gpu0"


def test_big_charter_waits_for_a_purser(lab):
    ch, *_ = lab
    c = ask(ch, "big")
    assert c["state"] == "requested"
    assert any("threshold" in w for w in c["why_not"])
    with pytest.raises(CharterError):
        ch.approve(c["id"], *AGENT)
    with pytest.raises(CharterError):
        ch.approve(c["id"], *PERSON)
    assert ch.approve(c["id"], *PURSER)["state"] == "approved"
    assert ch.charter(c["id"])["approval"] == "purser"


def test_agent_cannot_self_approve(lab):
    ch, *_ = lab
    with pytest.raises(CharterError):
        ask(ch, "big", approve=True)
    assert ask(ch, "big", who=PURSER, approve=True)["state"] == "approved"


def test_daily_limit_sends_the_rest_to_a_purser(lab):
    ch, *_ = lab
    states = [ask(ch)["state"] for _ in range(8)]  # $2 worst case each, $15 a day
    assert states == ["approved"] * 7 + ["requested"]
    c = ask(ch)
    assert any("daily" in w for w in c["why_not"])


def test_monthly_cap_is_never_crossed(lab):
    ch, *_ = lab
    ch.set_limits("default", "dana", monthly_usd=3)
    ask(ch)
    with pytest.raises(CharterError, match="monthly cap"):
        ask(ch)
    with pytest.raises(CharterError, match="monthly cap"):
        ask(ch, who=PURSER, approve=True)


def test_reaper_destroys_a_yacht_nobody_claims(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    ledger.release(c["claims"][0], "sweep-agent")
    clock.advance(1)
    step(ch)
    assert ch.charter(c["id"])["idle_since"] is not None
    clock.advance(20)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "destroyed" and "no claim held" in c["end_reason"]
    assert c["ref"] not in fake.machines and store.host(c["host"]) is None
    assert c["cost_usd"] == pytest.approx(0.5 * 21 / 60, abs=1e-3)
    # The reservation is released: only the actual cost still counts (to the cent).
    assert ch.usage("default")["reserved"]["daily_usd"] == pytest.approx(c["cost_usd"], abs=0.01)


def test_a_live_charter_keeps_its_own_claim_alive(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    clock = Clock()
    ledger = Ledger(store, clock=clock)
    ch = Charters(store, ledger, tmp_path, clock=clock, plugins={"fake": FakePlugin()})
    profile(tmp_path, hours=4, idle=20)
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    busy = {"gpus": [{"index": 0, "util_pct": 90.0}], "gpu_procs": []}
    for _ in range(12):  # an hour of 5-minute ticks, with no heartbeat from the requester
        store.record_probe(c["host"], busy, [])
        clock.advance(5)
        ledger.tick()
        step(ch)
    assert ledger.claim(c["claims"][0])["state"] == "held"
    assert ch.charter(c["id"])["state"] == "live"
    # A released claim stays released, and the yacht then counts as idle.
    ledger.release(c["claims"][0], "sweep-agent")
    step(ch)
    clock.advance(21)
    step(ch)
    c = ch.charter(c["id"])
    assert ledger.claim(c["claims"][0])["state"] == "released"
    assert c["state"] == "destroyed" and "no claim held" in c["end_reason"]


def test_profile_watches_follow_the_yacht(lab, tmp_path):
    ch, store, ledger, clock, fake = lab
    profile(
        tmp_path,
        "watched",
        extra='watch = [{ parser = "run-wrapper", glob = "~/runs/*/" },'
        ' { parser = "run-logdir", glob = "~/logs/*/" }]\n',
    )
    c = ask(ch, "watched")
    step(ch)
    host = ch.charter(c["id"])["host"]
    assert [(w["parser"], w["glob"]) for w in store.watches(host)] == [
        ("run-wrapper", "~/runs/*/"),
        ("run-logdir", "~/logs/*/"),
    ]
    ch.end(c["id"], *AGENT, reason="done")
    step(ch)
    assert ch.charter(c["id"])["state"] == "destroyed" and store.watches(host) == []


def test_a_watch_needs_a_parser_and_a_glob(lab, tmp_path):
    ch, *_ = lab
    profile(tmp_path, "bad", extra='watch = { parser = "run-wrapper" }\n')
    with pytest.raises(CharterError, match="parser and a glob"):
        ch.profile("bad")


def test_a_done_file_lands_the_charter_at_once(lab, tmp_path):
    ch, store, ledger, clock, fake = lab
    done = tmp_path / "work-done"
    profile(tmp_path, "signalled", extra=f'done_file = "{done}"\n')
    c = ask(ch, "signalled")
    step(ch)
    busy = {"gpus": [{"index": 0, "util_pct": 95.0}], "gpu_procs": []}
    store.record_probe(ch.charter(c["id"])["host"], busy, [])
    clock.advance(2)
    step(ch)
    assert ch.charter(c["id"])["state"] == "live"  # no file yet
    done.write_text("3\n")
    clock.advance(2)
    step(ch)  # sees the file and starts landing
    step(ch)  # lands and destroys
    c = ch.charter(c["id"])
    assert c["state"] == "destroyed" and c["end_reason"] == "work done, exit 3"
    assert c["cost_usd"] == pytest.approx(0.5 * 4 / 60, abs=1e-3)


def test_reaper_watches_gpu_load(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    step(ch)
    host = ch.charter(c["id"])["host"]
    busy = {"gpus": [{"index": 0, "util_pct": 90.0}], "gpu_procs": []}
    idle = {"gpus": [{"index": 0, "util_pct": 0.0}], "gpu_procs": []}
    store.record_probe(host, busy, [])
    step(ch)
    assert ch.charter(c["id"])["idle_since"] is None
    store.record_probe(host, idle, [])
    step(ch)
    clock.advance(21)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "destroyed" and "GPU idle" in c["end_reason"]


def test_time_limit_and_manual_end(lab):
    ch, store, ledger, clock, fake = lab
    a, b = ask(ch), ask(ch)
    step(ch)
    host = ch.charter(a["id"])["host"]
    store.record_probe(host, {"gpus": [{"index": 0, "util_pct": 99.0}], "gpu_procs": []}, [])
    ch.end(b["id"], *AGENT, reason="done early")
    clock.advance(4 * 60 + 1)
    step(ch)
    assert ch.charter(a["id"])["end_reason"] == "time limit"
    assert ch.charter(b["id"])["end_reason"] == "done early"
    assert {ch.charter(x["id"])["state"] for x in (a, b)} == {"destroyed"}


def test_end_belongs_to_requester_or_purser(lab):
    ch, *_ = lab
    c = ask(ch)
    with pytest.raises(CharterError):
        ch.end(c["id"], "someone-else", "agent", ["operator"])
    assert ch.end(c["id"], *PURSER)["state"] == "withdrawn"


def test_failed_create_still_destroys(lab):
    ch, store, ledger, clock, fake = lab
    fake.fail_create = True
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "failed" and "launch failed" in c["end_reason"]
    assert fake.machines == {}


def test_failed_destroy_goes_adrift_and_retries(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    step(ch)
    fake.fail_destroy = True
    ch.end(c["id"], *AGENT)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "adrift" and c["attempts"] == 1
    fake.fail_destroy = False
    step(ch)  # too soon to retry
    assert ch.charter(c["id"])["state"] == "adrift"
    clock.advance(2)
    step(ch)
    assert ch.charter(c["id"])["state"] == "destroyed"


def test_sweep_destroys_only_our_orphans(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    step(ch)
    fake.machines[f"{ch.prefix}999"] = {"status": "up"}
    fake.machines["someone-elses-cluster"] = {"status": "up"}
    gone = asyncio.run(ch.sweep())
    assert gone == [f"{ch.prefix}999"]
    assert "someone-elses-cluster" in fake.machines
    assert ch.charter(c["id"])["ref"] in fake.machines


def test_purser_is_for_people_only(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    with pytest.raises(ValueError):
        store.add_token("h1", "bot", "agent", actor="a", roles=["operator", "purser"])
    store.add_token("h2", "bot", "agent", actor="a")
    with pytest.raises(ValueError):
        store.grant_role("bot", "purser", actor="a")
    store.add_token("h3", "dana", "person", actor="a")
    assert "purser" not in store.identity("h3")["roles"]
    store.grant_role("dana", "purser", actor="a")
    assert store.identity("h3")["roles"] == ["viewer", "operator", "admin", "purser"]


def test_http_charter_flow(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    fake = FakePlugin()
    ch = Charters(store, ledger, tmp_path, plugins={"fake": fake})
    profile(tmp_path)
    profile(tmp_path, "big", rate=3.0, hours=8, idle=30)
    store.add_token(token_hash("agent"), "sweep-agent", "agent", actor="a")
    store.add_token(token_hash("person"), "dana", "person", actor="a")
    app = create_app(store=store, home=tmp_path, collect=False, ledger=ledger, charters=ch)
    agent = {"Authorization": "Bearer agent"}
    person = {"Authorization": "Bearer person"}
    with TestClient(app) as http:
        q = http.get("/api/charters/quote", params={"profile": "small"}, headers=agent).json()
        assert q["pre_approved"] and q["worst_usd"] == 2.0
        r = http.post("/api/charters", json={"profile": "small", "note": "x"}, headers=agent)
        assert r.status_code == 201 and r.json()["state"] == "approved"
        big = http.post("/api/charters", json={"profile": "big"}, headers=agent).json()
        assert big["state"] == "requested" and big["why_not"]
        assert http.post(f"/api/charters/{big['id']}/approve", headers=agent).status_code == 403
        assert http.post(f"/api/charters/{big['id']}/approve", headers=person).status_code == 403
        store.grant_role("dana", "purser", actor="a")
        r = http.post(f"/api/charters/{big['id']}/approve", headers=person)
        assert r.status_code == 200 and r.json()["approval"] == "purser"
        cut = {"daily_usd": 1}
        assert http.put("/api/budgets/default", json=cut, headers=agent).status_code == 403
        u = http.put("/api/budgets/default", json=cut, headers=person).json()
        assert u["limits"]["daily_usd"] == 1
        state = http.get("/api/state").json()
        assert len(state["charters"]) == 2 and state["budgets"][0]["project"] == "default"
        assert "spec" not in state["charters"][0]
        metrics = http.get("/metrics").text
        assert 'labtender_budget_limit_dollars{project="default",window="daily"} 1' in metrics
        assert "labtender_charter_active{" in metrics


def test_bootstrap_runs_before_live_with_its_own_timeout(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    fake = FakePlugin()
    seen = []
    fake.bootstrap = lambda yacht, commands, timeout_s, **kw: seen.append((commands, timeout_s))
    ch = Charters(store, ledger, tmp_path, clock=Clock(), plugins={"fake": fake})
    profile(tmp_path, extra='bootstrap = ["echo set-up"]\nbootstrap_timeout_min = 45\n')
    c = ask(ch)
    step(ch)
    assert seen == [(["echo set-up"], 45 * 60)]
    assert ch.charter(c["id"])["state"] == "live"


def test_reaper_ignores_daemons_that_only_load_cuda(lab):
    # The first real charter stayed up an hour after its run: a runtime daemon had the CUDA
    # library loaded (0 MiB of GPU memory), and the process list kept the GPU "busy".
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    step(ch)
    host = ch.charter(c["id"])["host"]
    daemon = [{"pid": 3177, "comm": "python", "card": None}]
    quiet = {"gpus": [{"index": 0, "util_pct": 0.0, "mem_used_mib": 3}], "gpu_procs": daemon}
    store.record_probe(host, quiet, [])
    step(ch)
    clock.advance(21)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "destroyed" and "GPU idle" in c["end_reason"]


def test_reaper_spares_loaded_memory_and_running_runs(lab):
    ch, store, ledger, clock, fake = lab
    a, b = ask(ch), ask(ch)
    step(ch)
    ha, hb = ch.charter(a["id"])["host"], ch.charter(b["id"])["host"]
    # a: a model sits in GPU memory between requests; b: a run is in a CPU-only phase.
    store.record_probe(ha, {"gpus": [{"index": 0, "util_pct": 0.0, "mem_used_mib": 9000}]}, [])
    run = {"dir": "/runs/x/", "name": "x", "parser": "p", "state": "running"}
    store.record_probe(hb, {"gpus": [{"index": 0, "util_pct": 0.0, "mem_used_mib": 0}]}, [run])
    for _ in range(3):
        step(ch)
        clock.advance(15)
    assert {ch.charter(x["id"])["state"] for x in (a, b)} == {"live"}


def test_provider_errors_are_summarized():
    from labtender.charter import clean_error, summarize_error

    raw = (
        "SkyPilot launch: \x1b[31mFailed to provision all possible launchable resources.\x1b[0m"
        " Relax the task's resource requirements\n"
        "  Failed to acquire resources in us-east4-a for {GCP(g2-standard-8)}.\n"
        "  Failed to acquire resources in us-east4-c for {GCP(g2-standard-8)}.\n"
    )
    assert "\x1b" not in clean_error(raw)
    assert summarize_error(raw) == "no capacity in us-east4-a, us-east4-c"
    other = "ssh: connect to host 1.2.3.4 port 22: Operation timed out\nmore detail"
    assert summarize_error(other) == "ssh: connect to host 1.2.3.4 port 22: Operation timed out"


def test_failed_launch_records_a_short_reason(lab):
    ch, store, ledger, clock, fake = lab
    fake.fail_create = True
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    assert c["end_reason"] == "launch failed: fake create failure"
    assert c["last_error"] == "fake create failure"


def test_setup_errors_keep_their_message_and_their_end():
    from labtender.yachts import PluginError, Yacht, excerpt, run_commands

    long = "ERROR: the real message\n" + "\n".join(f"frame {i}" for i in range(2000))
    cut = excerpt(long, 600)
    assert cut.startswith("ERROR: the real message") and cut.endswith("frame 1999")
    assert "characters left out" in cut
    with pytest.raises(PluginError) as e:
        run_commands(Yacht("r", None, "local"), ["echo step one; echo boom >&2; exit 3"], 30)
    msg = str(e.value)
    assert msg.startswith("exit 3 from: echo step one") and "boom" in msg and "step one" in msg


def test_a_launch_the_prepaid_balance_cannot_carry_fails_before_create(lab):
    ch, store, ledger, clock, fake = lab
    fake.balance_usd = 1.0  # the small profile's worst case is $2
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "failed" and "balance $1.00" in c["end_reason"]
    assert c["ref"] not in fake.machines
    fake.balance_usd = 5.0
    c2 = ask(ch)
    step(ch)
    assert ch.charter(c2["id"])["state"] == "live"
    # A second yacht must fit beside what the first may still cost.
    fake.balance_usd = 3.0
    c3 = ask(ch)
    step(ch)
    assert ch.charter(c3["id"])["state"] == "failed"


def test_low_balance_raises_the_alarm_once(lab):
    ch, store, ledger, clock, fake = lab
    fake.balance_usd = 50.0
    ask(ch)
    step(ch)
    fake.balance_usd = 1.0
    clock.advance(6)
    step(ch)
    (p,) = ch.view()["providers"]
    assert p["provider"] == "fake" and p["low"] and p["committed_usd"] > 1.0
    clock.advance(6)
    step(ch)
    assert [e["action"] for e in store.events()].count("provider.balance-low") == 1


def test_a_yacht_its_provider_stopped_is_lost_and_billed_only_until_then(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    clock.advance(30)
    fake.machines[c["ref"]]["status"] = "stopped"  # an empty balance stops every pod
    step(ch)
    c = ch.charter(c["id"])
    assert c["lost_at"] is not None and c["stop_reason"].startswith("lost")
    clock.advance(10)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "destroyed" and c["end_reason"].startswith("lost")
    assert c["cost_usd"] == pytest.approx(0.5 * 30 / 60, abs=1e-3)  # not the 40 min to the end
    assert any(e["action"] == "charter.lost" for e in store.events())


def test_an_unclear_state_must_be_seen_twice(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    step(ch)
    ref = ch.charter(c["id"])["ref"]
    fake.machines[ref]["status"] = "init"
    clock.advance(6)
    step(ch)
    assert ch.charter(c["id"])["lost_at"] is None
    fake.machines[ref]["status"] = "up"
    clock.advance(6)
    step(ch)
    assert ch.charter(c["id"])["status_misses"] == 0 and ch.charter(c["id"])["state"] == "live"


def test_a_failed_land_is_retried_while_the_yacht_is_up(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    fake = FakePlugin()
    clock = Clock()
    ch = Charters(store, ledger, tmp_path, clock=clock, plugins={"fake": fake})
    profile(tmp_path, extra=f'land = ["test -e {tmp_path.as_posix()}/ok"]\n')
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    ch.end(c["id"], "sweep-agent", "agent", ["viewer", "operator"], reason="done")
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "landing" and c["land_tries"] == 1 and c["ref"] in fake.machines
    (tmp_path / "ok").touch()  # the results are reachable again
    clock.advance(2)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "destroyed"
    actions = [e["action"] for e in store.events()]
    assert "charter.land-failed" in actions and "charter.landed" in actions


def test_a_land_that_keeps_failing_still_destroys(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    fake = FakePlugin()
    clock = Clock()
    ch = Charters(store, ledger, tmp_path, clock=clock, plugins={"fake": fake})
    profile(tmp_path, extra='land = ["false"]\n')
    c = ask(ch)
    step(ch)
    ch.end(c["id"], "sweep-agent", "agent", ["viewer", "operator"], reason="done")
    for _ in range(4):
        step(ch)
        clock.advance(2)
    c = ch.charter(c["id"])
    assert c["state"] == "destroyed" and c["ref"] not in fake.machines


def test_sync_copies_results_while_the_yacht_runs(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    fake = FakePlugin()
    clock = Clock()
    ch = Charters(store, ledger, tmp_path, clock=clock, plugins={"fake": fake})
    marks = tmp_path / "marks"
    profile(tmp_path, extra=f'sync = ["echo x >> {marks.as_posix()}"]\nsync_every_min = 4\n')
    c = ask(ch)
    step(ch)
    assert not marks.exists()
    clock.advance(4)
    step(ch)
    clock.advance(4)
    step(ch)  # the 5-minute state check comes first
    step(ch)
    assert marks.read_text().count("x") == 2
    assert ch.charter(c["id"])["state"] == "live"


def test_a_removed_host_keeps_its_gpu_samples(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("yacht-9", actor="t", transport="local", lifecycle="charter")
    store.record_probe("yacht-9", {"gpus": [{"index": 0, "util_pct": 97}]}, [])
    store.remove_host("yacht-9", actor="t", reason="charter ended")
    n = store.conn.execute("SELECT COUNT(*) FROM gpu_sample WHERE host = 'yacht-9'").fetchone()
    assert n[0] == 1


def test_a_price_rise_at_launch_sends_a_charter_back_to_a_purser(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    assert c["approval"] == "pre-approved"
    fake.price_factor = 3.0  # $1.50/h, worst $6: over the $5 pre-approval threshold
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "requested" and c["rate_usd_h"] == 1.5 and c["ref"] is None
    assert any(e["action"] == "charter.price-rose" for e in store.events())
    assert ch.approve(c["id"], *PURSER)["state"] == "approved"
    step(ch)
    assert ch.charter(c["id"])["state"] == "live"


def test_a_small_rise_still_inside_pre_approval_launches(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch)
    fake.price_factor = 1.5  # $0.75/h, worst $3: still pre-approved
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "live" and c["rate_usd_h"] == 0.75


def test_a_purser_approved_charter_goes_back_on_any_rise(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch, "big")
    ch.approve(c["id"], *PURSER)
    fake.price_factor = 1.1
    step(ch)
    assert ch.charter(c["id"])["state"] == "requested"


def test_a_cheaper_quote_is_taken_without_asking(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch, "big")
    ch.approve(c["id"], *PURSER)
    fake.price_factor = 0.5
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "live" and c["rate_usd_h"] == 1.5


def test_requote_updates_a_waiting_charter(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch, "big")
    fake.price_factor = 1.2
    clock.advance(90)
    with pytest.raises(CharterError, match="belongs to"):
        asyncio.run(ch.requote(c["id"], "someone-else", "agent", ["operator"]))
    c = asyncio.run(ch.requote(c["id"], *AGENT))
    assert c["state"] == "requested" and c["rate_usd_h"] == 3.6
    assert c["quoted_at"] == clock.t


def test_purser_view_lists_waiting_charters_with_the_market(lab):
    ch, store, ledger, clock, fake = lab
    c = ask(ch, "big")
    market = {"L4:1": [{"provider": "fake", "cheapest": {"region": "here", "rate_usd_h": 2.0}}]}
    v = ch.purser_view(clock.t, market)
    (w,) = v["waiting"]
    assert w["id"] == c["id"] and w["market"] == market["L4:1"]
    assert any("threshold" in x for x in w["why_not"]) and "spec" not in w
    assert {b["project"] for b in v["budgets"]} == {"default"}
    assert [e["action"] for e in v["history"]] == ["charter.request"]


def test_http_purser_page_and_requote(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    fake = FakePlugin()
    ch = Charters(store, ledger, tmp_path, plugins={"fake": fake})
    profile(tmp_path, "big", rate=3.0, hours=8, idle=30)
    store.add_token(token_hash("op"), "sweep-agent", "agent", actor="t")
    app = create_app(store=store, home=tmp_path, collect=False, ledger=ledger, charters=ch)
    with TestClient(app) as http:
        auth = {"Authorization": "Bearer op"}
        c = http.post("/api/charters", json={"profile": "big"}, headers=auth).json()
        assert http.get("/purser").status_code == 200
        v = http.get("/api/purser").json()
        assert [w["id"] for w in v["waiting"]] == [c["id"]]
        fake.price_factor = 2.0
        r = http.post(f"/api/charters/{c['id']}/requote", headers=auth).json()
        assert r["rate_usd_h"] == 6.0


def test_steps_stream_to_the_log_and_report_progress(tmp_path):
    from labtender.yachts import PluginError, Yacht, run_commands

    log = tmp_path / "logs" / "7.log"
    seen = []
    steps = [{"run": "echo copying caches", "label": "caches"}, "echo second; echo oops >&2"]
    run_commands(Yacht("r", None, "local"), steps, 30, log=log, progress=lambda *a: seen.append(a))
    text = log.read_text()
    assert "== step 1 of 2: caches" in text and "copying caches" in text
    assert "== step 2 of 2" in text and "oops" in text
    assert seen == [(1, 2, "caches"), (2, 2, None)]
    with pytest.raises(PluginError) as e:
        run_commands(Yacht("r", None, "local"), ["echo before; exit 4"], 30, log=log)
    assert "exit 4" in str(e.value) and "before" in str(e.value) and "copying" not in str(e.value)


def test_set_up_output_is_kept_per_charter_and_progress_shows_while_it_runs(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    fake = FakePlugin()
    ch = Charters(store, ledger, tmp_path, clock=Clock(), plugins={"fake": fake})
    seen = []
    real = fake.bootstrap

    def watching(yacht, commands, timeout_s, log=None, progress=None):
        def spy(*a):
            progress(*a)
            seen.append(dict(ch.view()["charters"][0]["progress"]))

        real(yacht, commands, timeout_s, log=log, progress=spy)

    fake.bootstrap = watching
    profile(
        tmp_path, extra='bootstrap = [{run = "echo fetched it", label = "fetch"}, "echo done"]\n'
    )
    c = ask(ch)
    step(ch)
    c = ch.charter(c["id"])
    assert c["state"] == "live"
    assert [(s["phase"], s["step"], s["label"]) for s in seen] == [
        ("set-up", 1, "fetch"),
        ("set-up", 2, None),
    ]
    assert ch.view()["charters"][0]["progress"] is None  # cleared once set-up is done
    tail = ch.log_tail(c["id"])["tail"]
    assert "fetched it" in tail and "step 2 of 2" in tail


def test_http_charter_log_needs_an_operator(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    ch = Charters(store, ledger, tmp_path, plugins={"fake": FakePlugin()})
    profile(tmp_path, extra='bootstrap = ["echo hello from set-up"]\n')
    store.add_token(token_hash("op"), "sweep-agent", "agent", actor="t")
    app = create_app(store=store, home=tmp_path, collect=False, ledger=ledger, charters=ch)
    c = ask(ch)
    step(ch)
    with TestClient(app) as http:
        assert http.get(f"/api/charters/{c['id']}/log").status_code == 401
        r = http.get(f"/api/charters/{c['id']}/log", headers={"Authorization": "Bearer op"})
        assert r.status_code == 200 and "hello from set-up" in r.json()["tail"]


def test_profiles_and_databases_from_before_the_rename_still_work(tmp_path):
    old = "dr" + "ain"
    store = Store(tmp_path / "t.sqlite")
    store.conn.execute(
        f"CREATE TABLE charter (id INTEGER PRIMARY KEY, state TEXT, {old}_tries INTEGER)"
    )
    store.conn.execute(f"INSERT INTO charter (id, state, {old}_tries) VALUES (1, '{old}ing', 2)")
    store.conn.commit()
    clock = Clock()
    ch = Charters(store, Ledger(store), tmp_path, clock=clock, plugins={"fake": FakePlugin()})
    row = store.conn.execute("SELECT state, land_tries FROM charter WHERE id = 1").fetchone()
    assert tuple(row) == ("landing", 2)
    profile(tmp_path, "older", extra=f'{old} = ["true"]\n{old}_timeout_min = 5\n')
    p = ch.profile("older")
    assert p["land"] == ["true"] and p["land_timeout_min"] == 5 and old not in p


def test_land_commands_get_the_charter_and_its_cost(tmp_path):
    from labtender.yachts import Yacht, run_commands

    out = tmp_path / "landed.txt"
    run_commands(
        Yacht("r", None, "local"),
        [f'echo {{charter}} {{cost_usd}} {{rate_usd_h}} $LABTENDER_CHARTER > "{out.as_posix()}"'],
        30,
        values={"charter": 7, "cost_usd": "1.25", "rate_usd_h": "2.49"},
    )
    assert out.read_text().split() == ["7", "1.25", "2.49", "7"]
