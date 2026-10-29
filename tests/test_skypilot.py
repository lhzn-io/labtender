"""The SkyPilot plugin and its helper, with SkyPilot itself replaced by fakes (no cloud)."""

import asyncio
import io
import json
import math
import sys
import types
from collections import namedtuple

import pytest

from labtender.charter import CharterError, Charters
from labtender.db import Store
from labtender.ledger import Ledger
from labtender.yachts import Offer, PluginError, sky_helper
from labtender.yachts.skypilot import SkyPilotPlugin

ROWS = [
    {"cloud": "runpod", "region": "CA", "instance_type": "1x_L4", "price": 0.59, "enabled": False},
    {
        "cloud": "gcp",
        "region": "us-east4",
        "instance_type": "g2-standard-4",
        "price": 0.71,
        "enabled": True,
    },
    {
        "cloud": "gcp",
        "region": "us-central1",
        "instance_type": "g2-standard-4",
        "price": 0.70,
        "enabled": True,
    },
]


class Runner:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        reply = self.replies[request["op"]]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_quote_puts_launchable_offers_first():
    p = SkyPilotPlugin(Runner({"quote": ROWS}))
    offers = p.quote({"accelerators": "L4:1"})
    assert [(o.provider, o.region) for o in offers] == [
        ("gcp", "us-central1"),
        ("gcp", "us-east4"),
        ("runpod", "CA"),
    ]
    assert offers[0].detail["instance_type"] == "g2-standard-4" and offers[0].gpu == "L4:1"
    call = p._runner.calls[0]
    assert (call["op"], call["gpu"], call["count"], call["instance_type"]) == (
        "quote",
        "L4",
        1,
        None,
    )


def test_infra_pins_cloud_and_region():
    runner = Runner({"quote": ROWS})
    p = SkyPilotPlugin(runner)
    offers = p.quote({"accelerators": "L4:1", "infra": "gcp/us-east4"})
    assert runner.calls[0]["clouds"] == ["gcp"]
    assert [o.region for o in offers] == ["us-east4"]


def test_create_pins_the_offer_and_the_time_limit():
    runner = Runner({"launch": {"name": "lt-x-1", "to_down": True, "autostop": 240}})
    p = SkyPilotPlugin(runner)
    offer = Offer(
        "gcp", "us-central1", "L4:1", 0.7, {"instance_type": "g2-standard-4", "enabled": True}
    )
    y = p.create("lt-x-1", {"disk_gb": 100}, offer, 4)
    assert y.ssh == "lt-x-1" and y.transport == "ssh" and y.provider == "gcp"
    call = runner.calls[0]
    assert call["max_hourly_cost"] == 0.7 and call["max_minutes"] == 240
    assert (call["cloud"], call["region"], call["instance_type"]) == (
        "gcp",
        "us-central1",
        "g2-standard-4",
    )


def test_create_passes_a_registry_token_and_keeps_it_out_of_errors():
    runpod = Offer("runpod", "CA", "L4:1", 0.43, {"instance_type": "1x_L4", "enabled": True})
    cmd = [sys.executable, "-c", "print('tok-123')"]
    runner = Runner({"launch": {"name": "lt-x-3", "to_down": True, "autostop": 60}})
    SkyPilotPlugin(runner).create("lt-x-3", {"registry_token_cmd": cmd}, runpod, 1)
    assert runner.calls[0]["registry_password"] == "tok-123"
    leaky = Runner({"launch": PluginError("SkyPilot launch: bad login tok-123")})
    with pytest.raises(PluginError) as e:
        SkyPilotPlugin(leaky).create("lt-x-4", {"registry_token_cmd": cmd}, runpod, 1)
    assert "tok-123" not in str(e.value) and "[registry token]" in str(e.value)
    failing = [sys.executable, "-c", "import sys; sys.exit('no credentials')"]
    with pytest.raises(PluginError, match="registry_token_cmd failed"):
        SkyPilotPlugin(Runner({})).create("lt-x-5", {"registry_token_cmd": failing}, runpod, 1)


def test_create_refuses_a_cloud_without_credentials():
    p = SkyPilotPlugin(Runner({}))
    offer = Offer("runpod", "CA", "L4:1", 0.59, {"instance_type": "1x_L4", "enabled": False})
    with pytest.raises(PluginError):
        p.create("lt-x-2", {}, offer, 1)


def test_helper_errors_become_plugin_errors():
    p = SkyPilotPlugin(Runner({"down": PluginError("SkyPilot down: boom")}))
    with pytest.raises(PluginError):
        p.destroy("lt-x-3")


def test_charter_refuses_offers_nobody_can_launch(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    (tmp_path / "charters").mkdir()
    (tmp_path / "charters" / "l4.toml").write_text(
        'name = "l4"\nplugin = "skypilot"\naccelerators = "L4:1"\n'
    )
    only_runpod = [ROWS[0]]
    ch = Charters(
        store,
        ledger,
        tmp_path,
        plugins={"skypilot": SkyPilotPlugin(Runner({"quote": only_runpod}))},
    )
    q = asyncio.run(ch.quote("l4"))
    assert q["launchable"] is False and q["worst_usd"] == round(0.59 * 4, 2)
    with pytest.raises(CharterError, match="credentials"):
        ch.request("l4", q["offers"][0], "dana", "person", ["operator"])


# ------------------------------------------------------------ the helper, with a fake sky module

Info = namedtuple("Info", "cloud instance_type accelerator_count price region cpu_count memory")


def fake_sky(monkeypatch, records=()):
    sky = types.ModuleType("sky")
    sdk = types.ModuleType("sky.client.sdk")
    client = types.ModuleType("sky.client")
    sdk.enabled_clouds = lambda: ["GCP"]
    sky.get = lambda x: x
    sky.list_accelerators = lambda **kw: {
        "L4": [
            Info("GCP", "g2-standard-4", 1.0, 0.70, "us-central1", 4, 16),
            Info("RunPod", "1x_L4", 1.0, 0.59, "CA", 8, 32),
            Info("GCP", "g2-standard-24", 2.0, 2.0, "us-central1", 24, 96),
            Info("Vast", "odd", 1.0, math.nan, "JP", 4, 16),
        ],
        "L40S": [Info("RunPod", "1x_L40S", 1.0, 1.09, "CA", 8, 64)],
    }
    Rec = namedtuple("Rec", "name status autostop to_down cloud region")
    sky.StatusRefreshMode = types.SimpleNamespace(AUTO="AUTO", NONE="NONE")
    sky.status = lambda names=None, refresh=None: [
        r for r in records if names is None or r.name in names
    ]

    class ClusterDoesNotExist(Exception):
        pass

    sky.exceptions = types.SimpleNamespace(ClusterDoesNotExist=ClusterDoesNotExist)

    def down(ref):
        if ref not in [r.name for r in records]:
            raise ClusterDoesNotExist(ref)

    sky.down = down
    client.sdk = sdk
    monkeypatch.setitem(sys.modules, "sky", sky)
    monkeypatch.setitem(sys.modules, "sky.client", client)
    monkeypatch.setitem(sys.modules, "sky.client.sdk", sdk)
    return Rec


def run_helper(monkeypatch, request):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    sky_helper.main()
    return json.loads(out.getvalue())


def test_helper_quote_keeps_exact_gpu_count_and_price(monkeypatch):
    fake_sky(monkeypatch)
    reply = run_helper(monkeypatch, {"op": "quote", "gpu": "l4", "count": 1, "clouds": ["gcp"]})
    rows = reply["result"]
    assert sorted((r["cloud"], r["instance_type"], r["enabled"]) for r in rows) == [
        ("gcp", "g2-standard-4", True),
        ("runpod", "1x_L4", False),
    ]


def test_helper_status_down_and_errors(monkeypatch):
    Rec = fake_sky(monkeypatch)
    fake_sky(monkeypatch, [Rec("lt-a", "UP", 240, True, "gcp", "us-central1")])
    assert run_helper(monkeypatch, {"op": "status", "ref": "lt-a"})["result"] == "up"
    assert run_helper(monkeypatch, {"op": "status", "ref": "lt-b"})["result"] == "gone"
    assert run_helper(monkeypatch, {"op": "down", "ref": "lt-b"}) == {"result": None}
    assert run_helper(monkeypatch, {"op": "clusters"})["result"] == ["lt-a"]
    bad = run_helper(monkeypatch, {"op": "nope"})
    assert bad["kind"] == "KeyError"


BIG = [
    {
        "cloud": "gcp",
        "region": "us-east4",
        "instance_type": "g2-standard-8",
        "price": 0.8508,
        "enabled": True,
        "vcpus": 8,
        "memory_gb": 32,
    }
]


def test_named_machine_and_extra_cost():
    runner = Runner({"quote": BIG, "launch": {"to_down": True, "autostop": 90}})
    p = SkyPilotPlugin(runner)
    spec = {"accelerators": "L4:1", "instance_type": "g2-standard-8", "extra_usd_h": 0.021}
    (o,) = p.quote(spec)
    assert runner.calls[0]["instance_type"] == "g2-standard-8"
    assert o.detail["instance_type"] == "g2-standard-8" and o.rate_usd_h == 0.8718
    p.create("lt-x-9", spec, o, 1.5)
    # SkyPilot's own price cap is the machine's price; the extra covers what it does not price.
    assert runner.calls[-1]["max_hourly_cost"] == 0.8508
    assert runner.calls[-1]["max_minutes"] == 90


def test_helper_prices_a_named_machine(monkeypatch):
    fake_sky(monkeypatch)
    sky = sys.modules["sky"]

    class Resources:
        def __init__(self, cloud, region, instance_type, accelerators):
            if cloud != "GCP":
                raise ValueError("not offered")
            self.cpus, self.memory = 8.0, 32.0

        def get_cost(self, seconds):
            return 0.8508 * seconds / 3600

    sky.Resources = Resources
    sky.CLOUD_REGISTRY = types.SimpleNamespace(from_str=lambda name: name.upper())
    reply = run_helper(
        monkeypatch,
        {
            "op": "quote",
            "gpu": "L4",
            "count": 1,
            "clouds": ["gcp"],
            "instance_type": "g2-standard-8",
        },
    )
    rows = reply["result"]
    assert [(r["cloud"], r["instance_type"], r["price"]) for r in rows] == [
        ("gcp", "g2-standard-8", 0.8508)
    ]


def test_private_artifact_registry_images_use_the_machine_account():
    env = sky_helper.registry_envs("docker:us-central1-docker.pkg.dev/proj/repo/img:tag")
    assert env == {
        "SKYPILOT_DOCKER_USERNAME": "oauth2accesstoken",
        "SKYPILOT_DOCKER_PASSWORD": "",
        "SKYPILOT_DOCKER_SERVER": "us-central1-docker.pkg.dev",
    }
    assert sky_helper.registry_envs("docker:ubuntu:24.04") == {}
    assert sky_helper.registry_envs("projects/deeplearning-platform/images/x") == {}
    assert sky_helper.registry_envs(None) == {}


def test_registry_tokens_are_scrubbed_from_skypilot_logs(tmp_path):
    log = tmp_path / "sky_logs/sky-1/provision.log"
    task = tmp_path / ".sky/generated/lt-x-1.yml.debug"
    other = tmp_path / "sky_logs/sky-1/other.log"
    for f, text in ((log, '"password": "tok-9"'), (task, "PW: tok-9"), (other, "nothing")):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    assert sky_helper.scrub_secret("tok-9", home=tmp_path) == 2
    assert "tok-9" not in log.read_text() + task.read_text()
    assert "[redacted]" in log.read_text() and other.read_text() == "nothing"
    assert (tmp_path / ".sky").stat().st_mode & 0o077 == 0
    assert (tmp_path / "sky_logs").stat().st_mode & 0o077 == 0


def test_registry_tokens_sign_in_off_the_registry_cloud():
    gar = "docker:us-central1-docker.pkg.dev/proj/repo/img:tag"
    assert sky_helper.registry_envs(gar, "tok")["SKYPILOT_DOCKER_PASSWORD"] == "tok"
    assert sky_helper.registry_envs(gar, "tok")["SKYPILOT_DOCKER_USERNAME"] == "oauth2accesstoken"
    env = sky_helper.registry_envs("docker:ghcr.io/org/img:1", "tok", "bot")
    assert env["SKYPILOT_DOCKER_USERNAME"] == "bot" and env["SKYPILOT_DOCKER_SERVER"] == "ghcr.io"
    with pytest.raises(ValueError, match="registry_user"):
        sky_helper.registry_envs("docker:ghcr.io/org/img:1", "tok")


def test_balances_pass_through_and_the_runpod_key_is_found(tmp_path, monkeypatch):
    runner = Runner({"balances": {"runpod": {"balance_usd": 149.92, "spend_usd_h": 0.0}}})
    assert SkyPilotPlugin(runner).balances()["runpod"]["balance_usd"] == 149.92
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert sky_helper._runpod_key(home=tmp_path) is None
    (tmp_path / ".runpod").mkdir()
    (tmp_path / ".runpod/config.toml").write_text('[default]\napi_key = "rp-test"\n')
    assert sky_helper._runpod_key(home=tmp_path) == "rp-test"
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-env")
    assert sky_helper._runpod_key(home=tmp_path) == "rp-env"
