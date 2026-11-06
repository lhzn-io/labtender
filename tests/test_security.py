"""Pre-release hardening: budgets, ssh targets, run links, probe answers, proxy trust, charters."""

import asyncio
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from labtender import yachts
from labtender.charter import CharterError, Charters, land_lead, public
from labtender.collector import clean_probe, read_bounded
from labtender.db import Store
from labtender.ledger import Ledger
from labtender.proxy import ProxyError, load
from labtender.server import create_app, run_link, token_hash
from labtender.yachts.fake import FakePlugin

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
    ch = Charters(store, ledger, tmp_path, clock=clock, plugins={"fake": FakePlugin()})
    profile(tmp_path)
    return ch, store, clock


def ask(ch, who=AGENT, **kw):
    q = asyncio.run(ch.quote("small"))
    return ch.request("small", q["offers"][0], *who, **kw)


# ------------------------------------------------------------------ budgets


def test_an_agent_cannot_charge_a_project_its_profile_does_not_name(lab):
    ch, *_ = lab
    for _ in range(7):  # $2 each: the default $15 a day lets seven through
        ask(ch)
    assert ask(ch)["state"] == "requested"  # the daily limit now holds it for a purser
    for name in ("x1", "x2"):
        with pytest.raises(CharterError, match="only a purser"):
            ask(ch, project=name)
    assert ask(ch, project="default")["state"] == "requested"  # naming its own is fine


def test_a_purser_may_charge_another_project(lab):
    ch, *_ = lab
    assert ask(ch, who=PURSER, project="research")["project"] == "research"


def test_http_refuses_another_project_from_an_agent(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    profile(tmp_path)
    ch = Charters(store, Ledger(store), tmp_path, plugins={"fake": FakePlugin()})
    app = create_app(store=store, home=tmp_path, collect=False, charters=ch, proxy=False)
    store.add_token(token_hash("a"), "sweep-agent", "agent", actor="t")
    http = TestClient(app)
    hdr = {"Authorization": "Bearer a"}
    r = http.post("/api/charters", json={"profile": "small", "project": "x1"}, headers=hdr)
    assert r.status_code == 403 and "only a purser" in r.json()["detail"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_limits_must_be_finite(lab, bad):
    ch, *_ = lab
    with pytest.raises(CharterError, match="finite"):
        ch.set_limits("default", "dana", monthly_usd=bad)


# ------------------------------------------------------------------ charters


def test_profile_step_timeouts_are_bounded(lab, tmp_path):
    ch, *_ = lab
    profile(tmp_path, "slow", extra='land = ["true"]\nland_timeout_min = 600\n')
    with pytest.raises(CharterError, match="land_timeout_min"):
        ch.profiles()


def test_landing_starts_early_enough_to_fit_the_time_limit(lab, tmp_path):
    c = {"spec": {"land": ["true"], "land_timeout_min": 25}, "max_hours": 4, "lost_at": None}
    assert land_lead(c) == 25 * 60 + 300
    assert land_lead(dict(c, spec={})) == 300
    assert land_lead(dict(c, max_hours=0.25)) == 0.25 * 3600 / 2  # never more than half
    assert land_lead(dict(c, lost_at=1.0)) == 300  # a lost yacht is not landed


def test_a_charter_with_landing_is_reaped_before_its_limit(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    clock = Clock()
    ch = Charters(store, Ledger(store), tmp_path, clock=clock, plugins={"fake": FakePlugin()})
    profile(tmp_path, extra='land = ["true"]\nland_timeout_min = 20\n')
    c = ask(ch)
    asyncio.run(ch.step(wait=True))
    host = ch.charter(c["id"])["host"]
    store.record_probe(host, {"gpus": [{"index": 0, "util_pct": 99.0}], "gpu_procs": []}, [])
    clock.advance(4 * 60 - 26)  # 26 min before the limit: not yet
    asyncio.run(ch.step(wait=True))
    assert ch.charter(c["id"])["state"] == "live"
    clock.advance(2)  # 24 min before: inside the 20 min landing plus 5 min destroy
    asyncio.run(ch.step(wait=True))
    assert ch.charter(c["id"])["end_reason"] == "time limit"


def test_public_charters_carry_one_line_of_error(lab):
    ch, *_ = lab
    c = ch.charter(ask(ch)["id"])
    c["last_error"] = "exit 1 from: setup\nAWS_SECRET_ACCESS_KEY=abc\nmore"
    assert public(c)["last_error"] == "exit 1 from: setup"


def test_a_failed_launch_puts_the_whole_error_in_the_log_only(lab):
    ch, store, _ = lab
    ch.plugins["fake"].fail_create = True
    c = ask(ch)
    asyncio.run(ch.step(wait=True))
    assert "fake create failure" in ch.log_path(c["id"]).read_text()
    (ev,) = [e for e in store.events(50) if e["action"] == "charter.launch-failed"]
    assert ev["detail"]["error"] == "fake create failure"


def test_wait_ready_gives_up_on_a_login_that_hangs(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(kw.get("timeout"))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(yachts.subprocess, "run", run)
    monkeypatch.setattr(yachts.time, "sleep", lambda s: None)
    yachts.Plugin().wait_ready(yachts.Yacht("r", "alias"))
    assert calls == [yachts.READY_TRY_S, yachts.READY_TRY_S]


# ------------------------------------------------------------------ hosts and links


@pytest.mark.parametrize(
    "bad", ["-oProxyCommand=sh -c id", "host name", "-p", "a;b", "box\nx", "$(id)"]
)
def test_ssh_targets_that_ssh_would_read_as_options_are_refused(tmp_path, bad):
    store = Store(tmp_path / "t.sqlite")
    with pytest.raises(ValueError, match="ssh must be"):
        store.add_host("box", actor="t", ssh=bad)
    store.add_host("box", actor="t", ssh="box")
    with pytest.raises(ValueError, match="ssh must be"):
        store.update_host("box", actor="t", ssh=bad)


@pytest.mark.parametrize("good", ["box", "lab@box.internal", "ssh://lab@box:2222", "lt-ab12-3"])
def test_ordinary_ssh_targets_are_accepted(tmp_path, good):
    Store(tmp_path / "t.sqlite").add_host("box", actor="t", ssh=good)


def test_the_probe_never_passes_the_target_as_an_option(tmp_path):
    from labtender.collector import probe_command, windows_command

    for cmd in (
        probe_command({"transport": "ssh", "ssh": "box"}, "arg", tmp_path),
        windows_command({"transport": "ssh", "ssh": "box"}, tmp_path),
    ):
        assert cmd[cmd.index("box") - 1] == "--"


@pytest.mark.parametrize(
    "bad", ["javascript:alert(1)//{name}", "data:text/html,x", "//evil/{name}"]
)
def test_run_links_must_be_http(tmp_path, bad):
    store = Store(tmp_path / "t.sqlite")
    with pytest.raises(ValueError, match="run_link"):
        store.add_host("box", actor="t", run_link=bad)


def test_a_stored_link_that_is_not_http_is_never_shown():
    host = {"name": "box", "services": {}}
    assert run_link("javascript:alert(1)//{name}", host, {"name": "r"}) is None
    assert run_link("https://x/{name}", host, {"name": "r"}) == "https://x/r"


# ------------------------------------------------------------------ probe answers


def test_probe_fields_the_board_draws_become_numbers():
    doc = clean_probe(
        {
            "time": 1.0,
            "gpus": [{"index": "<img src=x onerror=alert(1)>", "util_pct": "nan"}, "junk"],
            "gpu_procs": [{"pid": "<b>1</b>"}, {"pid": 42, "rss_kb": "7"}],
            "runs": ["junk", {"spec": 0}],
            "errors": ["x" * 2000],
        }
    )
    assert doc["gpus"] == [{"index": 0, "util_pct": None}]
    assert doc["gpu_procs"] == [{"pid": 42, "rss_kb": 7.0}]
    assert doc["runs"] == [{"spec": 0}] and len(doc["errors"][0]) == 500


def test_a_probe_answer_that_is_not_a_probe_is_refused():
    for bad in ([], "x", {"gpus": []}):
        with pytest.raises(ValueError):
            clean_probe(bad)


async def spawn(code):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


# A host that never stops writing, on both pipes, so they are full when the reader gives up.
FLOOD = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536); sys.stderr.write('y' * 4096)"


@pytest.mark.parametrize("repeat", range(5))  # the hang this guards against was a race
def test_an_oversized_probe_answer_is_cut_off(repeat):
    async def go():
        proc = await spawn(FLOOD)
        try:
            # The outer bound turns a regression into a failure instead of a hung test run.
            await asyncio.wait_for(read_bounded(proc, limit=1_000_000), 20)
        finally:
            assert proc.returncode is not None  # killed and collected

    with pytest.raises(RuntimeError, match="over"):
        asyncio.run(go())


def test_a_probe_that_times_out_is_killed_and_collected():
    async def go():
        proc = await spawn("import sys, time\nsys.stdout.write('x' * 300_000)\ntime.sleep(60)")
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_bounded(proc), 0.5)
        assert proc.returncode is not None
        assert asyncio.get_running_loop().time() - t0 < 15

    asyncio.run(go())


def test_a_probe_answer_under_the_limit_comes_back_whole():
    async def go():
        proc = await spawn("import sys; sys.stdout.write('x' * 300_000); sys.stderr.write('e')")
        return await asyncio.wait_for(read_bounded(proc), 20)

    out, err = asyncio.run(go())
    assert len(out) == 300_000 and err == b"e"


# ------------------------------------------------------------------ proxy and pages

SECRET = "s" * 40
CONFIG = f"""
header = "X-Auth-Request-Email"
secret = "{SECRET}"

[[allow]]
user = "pat@example.org"
identity = "pat"
roles = ["viewer", "operator", "admin", "purser"]
"""


def test_the_proxy_header_needs_the_proxy_secret(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    (tmp_path / "proxy.toml").write_text(CONFIG)
    http = TestClient(
        create_app(store=store, home=tmp_path, collect=False), client=("127.0.0.1", 1)
    )
    pat = {"X-Auth-Request-Email": "pat@example.org"}
    # Another process on the proxy's machine, or a rebinding page, has the address but not this.
    assert http.get("/api/whoami", headers=pat).status_code == 401
    wrong = {**pat, "X-Labtender-Proxy-Secret": "t" * 40}
    assert http.get("/api/whoami", headers=wrong).status_code == 401
    right = {**pat, "X-Labtender-Proxy-Secret": SECRET}
    assert http.get("/api/whoami", headers=right).json()["identity"] == "pat"


@pytest.mark.parametrize("secret", ["", "short"])
def test_a_proxy_config_needs_a_long_secret(tmp_path, secret):
    store = Store(tmp_path / "t.sqlite")
    (tmp_path / "proxy.toml").write_text(CONFIG.replace(SECRET, secret))
    with pytest.raises(ProxyError, match="secret"):
        load(tmp_path, store)


def test_pages_run_only_their_own_scripts(tmp_path):
    http = TestClient(create_app(store=Store(tmp_path / "t.sqlite"), home=tmp_path, collect=False))
    for path in ("/", "/compact", "/prices", "/purser"):
        csp = http.get(path).headers["content-security-policy"]
        script = next(p for p in csp.split("; ") if p.startswith("script-src"))
        assert "'sha256-" in script and "unsafe" not in script
        assert "frame-ancestors 'none'" in csp
    assert (
        http.get("/api/state").headers["content-security-policy"].startswith("default-src 'none'")
    )
