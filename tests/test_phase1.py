"""all-smi as a probe source, the Windows GPU module, /metrics and host labels."""

import asyncio
import time

from fastapi.testclient import TestClient

from labtender import probe
from labtender.collector import Collector, attach_windows, pci_bus, windows_command
from labtender.db import Store
from labtender.ledger import Ledger
from labtender.metrics import exposition
from labtender.server import create_app

ALLSMI = {
    "schema": 1,
    "hostname": "box",
    "gpus": [
        {
            "uuid": "GPU-1",
            "name": "Example GPU",
            "device_type": "GPU",
            "utilization": 41.0,
            "used_memory": 2 * 1073741824,
            "total_memory": 16 * 1073741824,
            "temperature": 55,
            "power_consumption": 120.5,
            "frequency": 1800,
        },
        {"uuid": "NPU-1", "name": "Example NPU", "device_type": "NPU", "utilization": 3.0},
    ],
}


def test_allsmi_rows():
    (g,) = probe.gpus_from_allsmi(ALLSMI)
    assert g["index"] == 0 and g["util_pct"] == 41.0 and g["source"] == "all-smi"
    assert g["mem_used_mib"] == 2048 and g["mem_total_mib"] == 16384
    assert g["power_w"] == 120.5 and g["temp_c"] == 55 and g["clock_mhz"] == 1800


def test_fill_missing_keeps_primary_values():
    base = [{"index": 0, "util_pct": 90.0, "power_w": None, "temp_c": None, "source": "nvidia-smi"}]
    probe.fill_missing(base, probe.gpus_from_allsmi(ALLSMI))
    assert base[0]["util_pct"] == 90.0 and base[0]["power_w"] == 120.5
    assert base[0]["temp_c"] == 55 and base[0]["source"] == "nvidia-smi"


def test_windows_join_by_pci_bus():
    assert pci_bus("00000000:0A:00.0") == 10 and pci_bus(None) is None
    doc = {
        "gpus": [
            {"index": 0, "bus_id": "00000000:02:00.0"},
            {"index": 1, "bus_id": "00000000:0A:00.0"},
        ],
        "errors": [],
    }
    sample = {
        "procs": [
            {"pid": 1, "name": "vmwp", "bus": 10, "util_pct": 99.0, "mem_mib": 8000},
            {"pid": 2, "name": "dwm", "bus": 2, "util_pct": 2.0, "mem_mib": 900},
            {"pid": 3, "name": "python", "bus": 2, "util_pct": 40.0, "mem_mib": 3000},
            {"pid": 4, "name": "ghost", "bus": 99, "util_pct": 1.0, "mem_mib": 10},
        ],
        "errors": ["partial"],
    }
    attach_windows(doc, sample)
    g0, g1 = doc["gpus"]
    assert [(p["name"], p["kind"]) for p in g0["windows_procs"]] == [
        ("python", "compute"),
        ("dwm", "desktop"),
    ]
    assert [(p["name"], p["kind"]) for p in g1["windows_procs"]] == [("vmwp", "wsl")]
    assert doc["errors"] == ["windows: partial"]


def test_windows_command_is_encoded_and_agentless(tmp_path):
    local = windows_command({"transport": "local"}, tmp_path)
    assert local[0].endswith("powershell.exe") and "-EncodedCommand" in local
    remote = windows_command({"transport": "ssh", "ssh": "box"}, tmp_path)
    assert remote[0] == "ssh" and "box" in remote
    assert remote[-1].startswith('"$(command -v powershell.exe || echo /mnt/c/')
    assert len(remote[-1]) < 30000  # within the Windows command-line limit


def test_windows_sample_respects_label_and_cache(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("ws", actor="a", transport="local")
    col = Collector(store, tmp_path)
    calls = []

    async def fake(host, control_dir):
        calls.append(time.time())
        return {
            "procs": [{"pid": 1, "name": "vmwp", "bus": 1, "util_pct": 50, "mem_mib": 1}],
            "errors": [],
        }

    monkeypatch.setattr("labtender.collector.run_windows", fake)
    doc = {"host": {"platform": "wsl"}, "gpus": [{"index": 0, "bus_id": "0:01:00.0"}]}
    asyncio.run(col._windows_sample(store.host("ws"), doc))
    asyncio.run(col._windows_sample(store.host("ws"), doc))
    assert len(calls) == 1 and doc["gpus"][0]["windows_procs"][0]["kind"] == "wsl"
    store.update_host("ws", actor="a", labels={"windows_gpu": "off"})
    fresh = {"host": {"platform": "wsl"}, "gpus": [{"index": 0, "bus_id": "0:01:00.0"}]}
    asyncio.run(col._windows_sample(store.host("ws"), fresh))
    assert "windows_procs" not in fresh["gpus"][0]
    linux = {"host": {"platform": "linux"}, "gpus": [{"index": 0}]}
    asyncio.run(col._windows_sample(store.host("ws"), linux))
    assert "windows_procs" not in linux["gpus"][0]


def test_labels_merge_and_remove(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("box", actor="a", labels={"room": "b12"})
    h = store.update_host("box", actor="a", labels={"windows_gpu": "off"})
    assert h["labels"] == {"room": "b12", "windows_gpu": "off"}
    assert store.update_host("box", actor="a", labels={"room": ""})["labels"] == {
        "windows_gpu": "off"
    }


def test_metrics(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("box", actor="a")
    ledger = Ledger(store)
    run = {
        "dir": "/r/a/",
        "name": 'a"b',
        "parser": "p",
        "state": "running",
        "pct": 50.0,
        "finish_at": time.time() + 600,
    }
    store.record_probe(
        "box",
        {
            "gpus": [
                {
                    "index": 0,
                    "name": "G",
                    "util_pct": 75.0,
                    "mem_used_mib": 1024,
                    "mem_total_mib": 2048,
                    "power_w": 100.0,
                    "temp_c": 60.0,
                }
            ],
            "gpu_procs": [{"pid": 1, "comm": "x", "card": 0}],
        },
        [run],
    )
    ledger.request("box/gpu0", "alice", "person")
    text = exposition(store, ledger)
    assert "# TYPE labtender_gpu_utilization_percent gauge" in text
    assert 'labtender_gpu_utilization_percent{host="box",gpu="0",name="G"} 75' in text
    assert 'labtender_gpu_memory_used_bytes{host="box",gpu="0",name="G"} 1073741824' in text
    assert 'labtender_gpu_claimed{host="box",gpu="0"} 1' in text
    assert 'labtender_host_up{host="box"} 1' in text
    assert 'labtender_run_progress_ratio{host="box",run="a\\"b"} 0.5' in text
    app = create_app(store=store, home=tmp_path, collect=False, ledger=ledger)
    with TestClient(app) as c:
        r = c.get("/metrics")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        assert "labtender_gpu_queue_length" in r.text


def test_host_transport_change(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("old", actor="a", transport="local")
    store.add_host("new", actor="a")
    h = store.update_host("old", actor="a", transport="ssh")
    assert h["transport"] == "ssh" and h["ssh"] == "old"
    assert store.update_host("new", actor="a", transport="local")["transport"] == "local"
    import pytest

    with pytest.raises(ValueError):
        store.update_host("new", actor="a", transport="carrier-pigeon")
