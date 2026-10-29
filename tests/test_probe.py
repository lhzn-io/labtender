import base64
import json
import os
import subprocess
import sys
import time

from labtender import probe
from labtender.collector import attribute, link_runs

NVSMI = (
    "0, GPU-aaaa, Example GPU A, 00000000:01:00.0, 97, 6650, 16303, 79.71, 95.00, 75, 2385,"
    " 3090, Disabled\n"
    "1, GPU-bbbb, Example GPU B, 00000000:02:00.0, 1, 4387, 16303, 16.28, 360.00, 45, 210,"
    " 3090, Enabled\n"
)
NVSMI_JETSON = (
    "0, 1111-2222, Example (nvgpu), [N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A],"
    " [N/A], [N/A], [N/A]\n"
)
TEGRASTATS = (
    "01-01-2026 00:00:00 RAM 1000/8000MB (lfb 2x2MB) SWAP 0/2048MB CPU [2%@729,0%@729]"
    " GR3D_FREQ 41% cpu@47.2C gpu@44.125C tj@47.2C VDD_GPU_SOC 5177mW/5177mW/5177mW"
)
IOREG = """+-o AGXAcceleratorG16X  <class AGXAccelerator>
    {
      "model" = "Example M1"
      "PerformanceStatistics" = {"In use system memory"=1954709504,"Device Utilization %"=20,"Alloc system memory"=4176445440}
    }"""  # noqa: E501


def test_nvidia_smi_rows():
    gpus = [probe.gpu_from_nvsmi(r) for r in probe.parse_nvsmi_csv(NVSMI, probe.NVSMI_FIELDS)]
    assert [g["index"] for g in gpus] == [0, 1]
    assert gpus[0]["util_pct"] == 97 and gpus[0]["mem_total_mib"] == 16303
    assert gpus[0]["display_active"] is False and gpus[1]["display_active"] is True


def test_jetson_reports_na_as_none():
    (row,) = probe.parse_nvsmi_csv(NVSMI_JETSON, probe.NVSMI_FIELDS)
    g = probe.gpu_from_nvsmi(row)
    assert g["util_pct"] is None and g["mem_total_mib"] is None and g["display_active"] is None


def test_tegrastats_line():
    t = probe.parse_tegrastats(TEGRASTATS)
    assert t == {
        "util_pct": 41.0,
        "power_w": 5.177,
        "temp_c": 44.125,
        "ram_used_mib": 1000.0,
        "ram_total_mib": 8000.0,
    }


def test_ioreg():
    io = probe.parse_ioreg(IOREG)
    assert io["util_pct"] == 20 and io["in_use_bytes"] == 1954709504 and io["model"] == "Example M1"


def test_collect_runs_picks_recent_folders(tmp_path):
    now = time.time()
    for name, age_h in (("a_48h_1", 1), ("b_48h_2", 200)):
        d = tmp_path / name
        d.mkdir()
        (d / "stdout.log").write_text("line\n" * 10)
        (d / "exit_code").write_text("0\n")
        old = now - age_h * 3600
        for p in (d / "stdout.log", d / "exit_code", d):
            os.utime(p, (old, old))
    spec = {
        "glob": str(tmp_path / "*"),
        "files": ["stdout.log"],
        "markers": ["exit_code"],
        "max_age_h": 72,
    }
    (entry,) = probe.collect_runs([spec], now)
    assert entry["dir"].endswith("a_48h_1")
    assert entry["files"]["stdout.log"]["size"] == 50
    assert entry["markers"]["exit_code"] == "0\n"
    # A recently touched folder with an old log stays out.
    os.utime(tmp_path / "b_48h_2", None)
    assert [e["dir"] for e in probe.collect_runs([spec], now)] == [entry["dir"]]
    spec["head_bytes"], spec["tail_bytes"] = 4, 5
    (entry,) = probe.collect_runs([spec], now)
    assert entry["files"]["stdout.log"]["head"] == "line"
    assert entry["files"]["stdout.log"]["tail"] == "line\n"


def test_probe_runs_as_a_piped_script(tmp_path):
    src = open(probe.__file__, "rb").read()
    arg = base64.b64encode(json.dumps({"runs": []}).encode()).decode()
    out = subprocess.run(
        [sys.executable, "-I", "-", arg], input=src, capture_output=True, check=True, cwd=tmp_path
    )
    doc = json.loads(out.stdout)
    assert doc["schema"] == 1 and doc["host"]["platform"] in ("wsl", "jetson", "linux", "macos")


def _doc(procs, ngpu=2):
    gpus = [{"index": i, "uuid": f"GPU-{i}{i}{i}"} for i in range(ngpu)]
    return {"gpus": gpus, "gpu_procs": procs}


def test_attribute_by_pci_index_and_uuid():
    doc = _doc(
        [
            {"env_readable": True, "cuda_visible_devices": "1", "cuda_device_order": "PCI_BUS_ID"},
            {"env_readable": True, "cuda_visible_devices": "GPU-000", "cuda_device_order": None},
            {"env_readable": True, "cuda_visible_devices": "1", "cuda_device_order": None},
            {"env_readable": True, "cuda_visible_devices": None, "cuda_device_order": None},
            {"env_readable": True, "cuda_visible_devices": "", "cuda_device_order": None},
            {"env_readable": False, "cuda_visible_devices": None, "cuda_device_order": None},
        ]
    )
    attribute(doc)
    cards = [p["card"] for p in doc["gpu_procs"]]
    assert cards == [1, 0, None, None, None, None]
    notes = [p["note"] for p in doc["gpu_procs"]]
    assert "counts on every GPU" in notes[2] and "counts on every GPU" in notes[3]
    assert notes[4] == "GPUs hidden" and notes[5] == "environment not readable"


def test_attribute_single_gpu_node():
    doc = _doc(
        [{"env_readable": True, "cuda_visible_devices": None, "cuda_device_order": None}], ngpu=1
    )
    attribute(doc)
    assert doc["gpu_procs"][0]["card"] == 0


def test_link_runs_by_open_log():
    doc = {
        "gpu_procs": [
            {"pid": 7, "card": 1, "open_logs": ["/r/logs/b/run.log"]},
            {"pid": 9, "card": 0, "open_logs": []},
        ]
    }
    runs = [
        {"dir": "/r/logs/b/", "card": None, "state": "stale"},
        {"dir": "/r/logs/bb", "card": None, "state": "stale"},
    ]
    link_runs(doc, runs)
    assert runs[0]["pid"] == 7 and runs[0]["card"] == 1 and runs[0]["state"] == "running"
    assert runs[1]["state"] == "stale"
    assert "pid" not in runs[1] and runs[1]["card"] is None


def test_probe_lists_open_logs(tmp_path):
    log = tmp_path / "run.log"
    with open(log, "w") as f:
        assert probe.open_logs(os.getpid(), {"run.log"}) == [str(log)]
        f.write("x")


def test_network_counters_skip_virtual_interfaces(monkeypatch):
    from labtender import probe

    dev = (
        "Inter-|   Receive    |  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes\n"
        "    lo: 9000 1 0 0 0 0 0 0 9000 1 0 0 0 0 0 0\n"
        "  eth0: 1000 1 0 0 0 0 0 0 500 1 0 0 0 0 0 0\n"
        "docker0: 7000 1 0 0 0 0 0 0 7000 1 0 0 0 0 0 0\n"
        " wlan0: 24 1 0 0 0 0 0 0 6 1 0 0 0 0 0 0\n"
    )
    monkeypatch.setattr(
        probe, "read", lambda path, *a, **k: dev if path == "/proc/net/dev" else None
    )
    assert probe.linux_net_bytes() == (1024, 506)
    free, total = probe.disk_free("/")
    assert free is not None and total >= free
