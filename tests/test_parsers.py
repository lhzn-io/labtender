import pytest

from labtender import parsers

LINE = "step 30000 | time 36.0 h | speed 10.0 h/h"  # 36 h done at 6 wall min per hour


def logline(t_h, step=8000):
    return f"[12:28:14] step={step} time={t_h}h speed=1.7h/h"


@pytest.fixture
def spec():
    return parsers.load_specs()["run-wrapper"]


def entry(tail, markers=None, name="/runs/example_48h_20260101-000000", log_mtime=1000.0):
    return {
        "dir": name,
        "files": {"stdout.log": {"tail": tail, "mtime": log_mtime, "size": 1}},
        "markers": markers or {},
    }


def test_running_eta(spec):
    r = parsers.interpret(spec, entry("start\n" + LINE + "\n"), probe_time=1060.0, hub_time=5060.0)
    assert r["state"] == "running" and r["iter"] == 30000
    assert r["t_h"] == pytest.approx(36.0) and r["target_h"] == 48.0
    assert r["pct"] == pytest.approx(75.0)
    assert r["eta_s"] == pytest.approx(12 * 6 * 60)
    # Re-based on the hub's clock: the log was written 60 s before the probe ran.
    assert r["last_log_at"] == pytest.approx(5000.0)
    assert r["finish_at"] == pytest.approx(5000.0 + 12 * 6 * 60)


def test_target_from_args_beats_dirname(spec):
    r = parsers.interpret(
        spec, entry(LINE, {"args": "--config base.toml --hours 72 --quiet"}), 1000.0, 1000.0
    )
    assert r["target_h"] == 72.0


def test_card_from_marker(spec):
    m = {"device": "CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1\nfree\n"}
    assert parsers.interpret(spec, entry(LINE, m), 1000.0, 1000.0)["card"] == 1


def test_stale_after_silence(spec):
    r = parsers.interpret(spec, entry(LINE), probe_time=1000.0 + 31 * 60, hub_time=0.0)
    assert r["state"] == "stale" and r["eta_s"] is None


def test_exit_status_ends_the_run(spec):
    assert (
        parsers.interpret(spec, entry(LINE, {"status": "0\n"}), 1000.0, 1000.0)["state"] == "done"
    )
    r = parsers.interpret(spec, entry(LINE, {"status": "137\n"}), 1000.0, 1000.0)
    assert r["state"] == "exited" and r["exit_code"] == 137


def test_no_progress_yet(spec):
    r = parsers.interpret(spec, entry("compiling\n"), 1000.0, 1000.0)
    assert r["state"] == "running" and r["pct"] is None and r["eta_s"] is None


def test_jsonl_spec():
    spec = {
        "name": "j",
        "log": "progress.jsonl",
        "progress": {
            "format": "jsonl",
            "t_field": "t",
            "t_unit": "h",
            "rate_field": "speed",
            "iter_field": "step",
        }
        | {"rate": "sim_h_per_wall_h"},
        "target": [{"log": r'"end_h": (?P<value>[\d.]+)', "unit": "h"}],
    }
    tail = '{"end_h": 10}\n{"t": 4, "speed": 2.0, "step": 40}\n'
    e = {"dir": "/r/x", "files": {"progress.jsonl": {"tail": tail, "mtime": 0.0}}, "markers": {}}
    r = parsers.interpret(spec, e, 0.0, 0.0)
    assert r["pct"] == pytest.approx(40.0) and r["wall_min_per_sim_h"] == pytest.approx(30.0)
    assert r["eta_s"] == pytest.approx(6 * 30 * 60)


def logdir(head, tail, markers=None):
    return {
        "dir": "/r/logs/example-20260101-000000",
        "files": {"run.log": {"head": head, "tail": tail, "mtime": 0.0}},
        "markers": markers or {},
    }


def test_logdir_target_from_head_and_done_at_target():
    spec = parsers.load_specs()["run-logdir"]
    r = parsers.interpret(spec, logdir("setup\nduration_h = 48\n", logline(48.0)), 0.0, 0.0)
    assert r["target_h"] == 48.0 and r["state"] == "done"


def test_logdir_done_file_ends_the_run():
    spec = parsers.load_specs()["run-logdir"]
    e = logdir("duration_h = 36\n", logline(35.4))  # last progress line, 98.3 %
    assert parsers.interpret(spec, e, 60.0, 60.0)["state"] == "running"
    e["markers"] = {"DONE": ""}
    r = parsers.interpret(spec, e, 60.0, 60.0)
    assert r["state"] == "done" and r["pct"] == 100.0 and r["finish_at"] is None
    assert "DONE" in parsers.probe_request(spec, "~/logs/*/")["markers"]


def test_logdir_duration_marker_beats_head():
    spec = parsers.load_specs()["run-logdir"]
    e = logdir("setup\nduration_h = 24\n", logline(25.0), {"duration_h": "48\n"})
    r = parsers.interpret(spec, e, 0.0, 0.0)
    assert r["target_h"] == 48.0 and r["state"] == "running"
    assert "duration_h" in parsers.probe_request(spec, "~/logs/*/")["markers"]


def test_probe_request_includes_done_marker(spec):
    req = parsers.probe_request(spec, "~/runs/*/")
    assert req["files"] == ["stdout.log"] and "status" in req["markers"]


def test_silent_run_just_short_of_target_is_done(spec):
    line = LINE.replace("time 36.0 h", "time 47.9 h")
    assert parsers.interpret(spec, entry(line), 1000.0 + 3600, 0.0)["state"] == "done"
