"""Seed a hub database with a fictional lab, for screenshots, demos and UI work.

    LABTENDER_HOME=/tmp/labtender-demo python scripts/demo_state.py
    LABTENDER_HOME=/tmp/labtender-demo labtender serve --no-collect --port 8797

Every host, user, job and path here is invented. The hub must run with ``--no-collect``: the
hosts do not exist, and polling them would only record errors.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labtender.db import Store  # noqa: E402
from labtender.ledger import Ledger  # noqa: E402

NOW = time.time()
H = 3600.0


CARD_IDS = itertools.count(1)


def demo_uuid(n: int) -> str:
    """A made-up GPU UUID in NVIDIA's format, the same for the same n."""
    h = hashlib.sha256(f"demo-card-{n}".encode()).hexdigest()
    return f"GPU-{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def gpu(
    index,
    name,
    util,
    used,
    total,
    *,
    power=None,
    limit=None,
    temp=None,
    clock=None,
    display=False,
    unified=False,
    source="nvidia-smi",
    uuid=None,
):
    return {
        "index": index,
        "uuid": uuid or demo_uuid(next(CARD_IDS)),  # one per card, so none looks moved
        "name": name,
        "bus_id": None,
        "util_pct": util,
        "mem_used_mib": used * 1024,
        "mem_total_mib": total * 1024,
        "power_w": power,
        "power_limit_w": limit,
        "temp_c": temp,
        "clock_mhz": clock,
        "clock_max_mhz": None,
        "display_active": display,
        "unified": unified,
        "source": source,
    }


def proc(pid, comm, card, cmd, elapsed_h, rss_gib, log=None):
    return {
        "pid": pid,
        "comm": comm,
        "cmdline": cmd,
        "via": "libcuda",
        "env_readable": True,
        "cuda_visible_devices": str(card),
        "cuda_device_order": "PCI_BUS_ID",
        "elapsed_s": elapsed_h * H,
        "cpu_s": None,
        "rss_kb": rss_gib * 1024 * 1024,
        "open_logs": [log] if log else [],
        "card": card,
        "note": None,
    }


def run(dir_, *, state, t_h, target_h, rate, card, pid=None, log_age_s=40.0, exit_code=None):
    last = NOW - log_age_s
    eta = (target_h - t_h) * rate * 60 if state == "running" else None
    return {
        "dir": dir_,
        "name": os.path.basename(dir_.rstrip("/")),
        "parser": "run-logdir",
        "log_age_s": log_age_s,
        "last_log_at": last,
        "t_h": t_h,
        "target_h": target_h,
        "pct": 100.0 * t_h / target_h,
        "iter": int(t_h * 400),
        "wall_min_per_sim_h": rate,
        "eta_s": eta,
        "finish_at": last + eta if eta is not None else None,
        "exit_code": exit_code,
        "card": card,
        "last_line": None,
        "state": state,
        **({"pid": pid} if pid else {}),
    }


def host(platform, cpu, ncpu, busy, mem_total_gib, mem_used_gib):
    return {
        "hostname": "demo",
        "platform": platform,
        "os": "demo",
        "arch": "x86_64",
        "cpu_model": cpu,
        "ncpu": ncpu,
        "loadavg": [1.0, 1.0, 1.0],
        "cpu_busy_pct": busy,
        "mem_total_kb": mem_total_gib * 1024 * 1024,
        "mem_avail_kb": (mem_total_gib - mem_used_gib) * 1024 * 1024,
        "uptime_s": 9 * 86400,
    }


def history(store: Store, host_name: str, idx: int, level: float, wobble: float) -> None:
    rng = random.Random(f"{host_name}{idx}")
    rows = []
    for i in range(80):
        t = NOW - (80 - i) * 45
        u = max(0.0, min(100.0, level + wobble * math.sin(i / 6) + rng.uniform(-4, 4)))
        rows.append((host_name, idx, t, u))
    store.conn.executemany("INSERT INTO gpu_sample (host, idx, t, util) VALUES (?, ?, ?, ?)", rows)


def main() -> None:
    home = Path(os.environ.get("LABTENDER_HOME", ""))
    if not os.environ.get("LABTENDER_HOME"):
        sys.exit("set LABTENDER_HOME to an empty demo folder; this never touches a real hub")
    db = home / "labtender.sqlite"
    if db.exists():
        sys.exit(f"{db} exists; use a fresh LABTENDER_HOME for the demo")
    store = Store(db)
    ledger = Ledger(store)
    admin = "demo"

    store.add_host("gpu-box", actor=admin)
    store.add_host("ws-laptop", actor=admin, transport="local")
    store.add_host("jetson-1", actor=admin, accepts=["nice-to-have"])
    store.add_host("mac-studio", actor=admin)

    sweep = "/home/ana/cfd/logs/channel-flow-re5600-20261020-221500/"
    ablation = "/home/ben/train/runs/vit-ablation-lr3e-4-20261021-081200/"
    edge = "/home/ana/cfd/logs/heat-sink-sweep-07-20261021-063000/"
    store.record_probe(
        "gpu-box",
        {
            "host": host("linux", "AMD EPYC 7543 32-Core", 64, 41.0, 256, 118),
            "probe_ms": 412,
            "errors": [],
            "gpus": [
                gpu(
                    0,
                    "NVIDIA RTX 6000 Ada",
                    97,
                    38.2,
                    48,
                    power=281,
                    limit=300,
                    temp=72,
                    clock=2490,
                ),
                gpu(
                    1,
                    "NVIDIA RTX 6000 Ada",
                    94,
                    21.5,
                    48,
                    power=262,
                    limit=300,
                    temp=69,
                    clock=2445,
                ),
            ],
            "gpu_procs": [
                proc(
                    4121,
                    "python",
                    0,
                    "python solve.py --case channel-flow --re 5600",
                    15.4,
                    9.8,
                    sweep + "run.log",
                ),
                proc(
                    5230,
                    "python",
                    1,
                    "python train.py --model vit-b16 --lr 3e-4",
                    5.1,
                    14.2,
                    ablation + "run.log",
                ),
            ],
        },
        [
            run(sweep, state="running", t_h=31.2, target_h=48, rate=21.5, card=0, pid=4121),
            run(ablation, state="running", t_h=6.4, target_h=10, rate=48.0, card=1, pid=5230),
            run(
                "/home/ana/cfd/logs/channel-flow-re5600-20261019-101500/",
                state="done",
                t_h=48,
                target_h=48,
                rate=20.9,
                card=0,
                log_age_s=20 * H,
            ),
        ],
    )
    store.record_probe(
        "ws-laptop",
        {
            "host": host("wsl", "Intel Core Ultra 9 275HX", 24, 18.0, 32, 11),
            "probe_ms": 560,
            "errors": [],
            "gpus": [
                gpu(
                    0,
                    "NVIDIA RTX 5080 Laptop GPU",
                    64,
                    9.4,
                    16,
                    power=71,
                    limit=80,
                    temp=66,
                    clock=2210,
                    display=True,
                )
                | {
                    "bus_id": "00000000:01:00.0",
                    "windows_procs": [
                        {
                            "pid": 7712,
                            "name": "vmwp",
                            "kind": "wsl",
                            "util_pct": 58.0,
                            "mem_mib": 6300,
                        },
                        {
                            "pid": 2210,
                            "name": "dwm",
                            "kind": "desktop",
                            "util_pct": 3.0,
                            "mem_mib": 1400,
                        },
                        {
                            "pid": 5120,
                            "name": "firefox",
                            "kind": "desktop",
                            "util_pct": 1.0,
                            "mem_mib": 820,
                        },
                        {
                            "pid": 6033,
                            "name": "code",
                            "kind": "desktop",
                            "util_pct": 0.0,
                            "mem_mib": 310,
                        },
                    ],
                }
            ],
            "gpu_procs": [
                proc(9311, "python", 0, "python -m ipykernel_launcher -f kernel-3.json", 1.3, 6.1)
            ],
        },
        [],
    )
    store.record_probe(
        "jetson-1",
        {
            "host": host("jetson", "NVIDIA Jetson AGX Orin Developer Kit", 12, 22.0, 61, 19),
            "probe_ms": 1180,
            "errors": [],
            "gpus": [
                gpu(
                    0,
                    "NVIDIA Jetson AGX Orin Developer Kit",
                    88,
                    19,
                    61,
                    power=14.7,
                    temp=51,
                    unified=True,
                )
            ],
            "gpu_procs": [
                proc(
                    812,
                    "python",
                    0,
                    "python solve.py --case heat-sink --variant 07",
                    6.2,
                    7.4,
                    edge + "run.log",
                )
            ],
        },
        [run(edge, state="running", t_h=4.9, target_h=24, rate=58.0, card=0, pid=812)],
    )
    store.record_probe(
        "mac-studio",
        {
            "host": host("macos", "Apple M4 Max", 16, 9.0, 128, 31),
            "probe_ms": 44,
            "errors": [],
            "gpus": [gpu(0, "Apple M4 Max", 14, 1.6, 128, unified=True, source="ioreg")],
            "gpu_procs": [],
        },
        [],
    )
    for name, idx, level, wobble in (
        ("gpu-box", 0, 95, 3),
        ("gpu-box", 1, 90, 8),
        ("ws-laptop", 0, 55, 15),
        ("jetson-1", 0, 85, 10),
        ("mac-studio", 0, 15, 8),
    ):
        history(store, name, idx, level, wobble)

    for who, kind in (
        ("ana", "person"),
        ("ben", "person"),
        ("eval-agent-17", "agent"),
        ("sweep-agent-3", "agent"),
    ):
        store.add_token(f"demo-{who}", who, kind, actor=admin)
    # gpu-box/gpu0: ana's run holds it; a critical agent job and a normal one wait.
    c = ledger.request("gpu-box/gpu0", "ana", "person", est_hours=4, note="channel flow, Re 5600")
    ledger.bind_run(c["id"], "ana", sweep)
    ledger.request("gpu-box/gpu0", "sweep-agent-3", "agent", est_hours=6, note="sweep 4 of 10")
    ledger.request(
        "gpu-box/gpu0",
        "eval-agent-17",
        "agent",
        tier="critical",
        est_hours=1.5,
        note="release eval",
    )
    # gpu-box/gpu1: ben holds it with an estimate; jetson-1: ana's nice-to-have run.
    ledger.request("gpu-box/gpu1", "ben", "person", est_hours=6, note="ViT ablation")
    c = ledger.request(
        "jetson-1/gpu0",
        "ana",
        "person",
        tier="nice-to-have",
        est_hours=12,
        note="heat-sink sweep 7",
    )
    ledger.bind_run(c["id"], "ana", edge)
    for name in ("gpu-box", "ws-laptop", "jetson-1", "mac-studio"):
        snap = store.snapshot(name)
        ledger.reconcile(name, snap)
    mac = store.find_card("mac-studio/gpu0")
    store.set_card(mac["uuid"], hold="kept for notebooks and model serving", actor=admin)
    services(store, ledger)
    charters(store, ledger, home)
    print(f"demo lab written to {db}")


def services(store: Store, ledger: Ledger) -> None:
    """A results dashboard on two hosts: one answering its health check, one hung."""
    checks = (("gpu-box", True, "HTTP 200"), ("jetson-1", False, "no answer in 5 s"))
    for name, ok, detail in checks:
        url = f"http://{name}.lan:9000"
        store.update_host(
            name, actor="demo", services={"dashboard": url}, health={"dashboard": "/healthz"}
        )
        store.record_health(name, "dashboard", url + "/healthz", ok, detail, now=NOW - 0.6 * H)
        if not ok:
            ledger.service_flag(name, {"dashboard": detail})


def charters(store: Store, ledger: Ledger, home: Path) -> None:
    """Yachts from the fake plugin: one live, one waiting for a purser, one already destroyed."""
    import asyncio

    from labtender.charter import Charters
    from labtender.yachts.fake import FakePlugin

    folder = home / "charters"
    folder.mkdir(exist_ok=True)
    (folder / "l4-spot.toml").write_text(
        'name = "l4-spot"\ndescription = "One L4 for short jobs"\nplugin = "fake"\n'
        'accelerators = "L4:1"\nfake_rate_usd_h = 0.71\nmax_hours = 4\nidle_min = 20\n'
    )
    (folder / "h100-day.toml").write_text(
        'name = "h100-day"\ndescription = "One H100 for a day"\nplugin = "fake"\n'
        'accelerators = "H100:1"\nfake_rate_usd_h = 2.49\nmax_hours = 12\nidle_min = 30\n'
    )
    clock = [NOW - 5 * H]
    ch = Charters(store, ledger, home, clock=lambda: clock[0], plugins={"fake": FakePlugin()})

    def ask(profile, who, kind, note):
        q = asyncio.run(ch.quote(profile))
        offer = dict(q["offers"][0], provider="neocloud-a", region="us-east-1")
        return ch.request(profile, offer, who, kind, ["viewer", "operator"], note=note)

    done = ask("l4-spot", "sweep-agent-3", "agent", "overflow: sweep 9 of 10 while gpu-box is full")
    asyncio.run(ch.step(wait=True))
    clock[0] = NOW - 3.2 * H
    ch.end(done["id"], "sweep-agent-3", "agent", ["operator"], reason="sweep finished")
    asyncio.run(ch.step(wait=True))
    clock[0] = NOW - 48 * 60
    live = ask("l4-spot", "eval-agent-17", "agent", "release eval overflow")
    asyncio.run(ch.step(wait=True))
    yacht = ch.charter(live["id"])["host"]
    store.record_probe(
        yacht,
        {
            "host": host("linux", "AMD EPYC 9354 32-Core", 16, 22.0, 64, 21),
            "probe_ms": 380,
            "errors": [],
            "gpus": [gpu(0, "NVIDIA L4", 88, 17, 24, power=61, limit=72, temp=64, clock=1950)],
            "gpu_procs": [
                proc(51022, "python", 0, "python eval.py --suite release", 0.7, 9.6),
            ],
        },
        [],
    )
    clock[0] = NOW - 10 * 60
    ask("h100-day", "ben", "person", "full ViT pretraining run; gpu-box booked all week")


if __name__ == "__main__":
    main()
