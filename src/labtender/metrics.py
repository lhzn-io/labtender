"""Prometheus text exposition of the labtender service's view, for Prometheus and Grafana."""

from __future__ import annotations

import time

from .db import Store
from .ledger import Ledger

ACTIVE_CHARTER = ("approved", "waiting", "launching", "live", "landing", "adrift")

HELP = {
    "labtender_host_up": ("gauge", "1 when the host answered within three poll intervals"),
    "labtender_host_last_seen_seconds": ("gauge", "Seconds since the host last answered"),
    "labtender_gpu_utilization_percent": ("gauge", "GPU load"),
    "labtender_gpu_memory_used_bytes": ("gauge", "GPU memory in use (system memory if unified)"),
    "labtender_gpu_memory_total_bytes": ("gauge", "GPU memory size (system memory if unified)"),
    "labtender_gpu_power_watts": ("gauge", "GPU power draw"),
    "labtender_gpu_temperature_celsius": ("gauge", "GPU temperature"),
    "labtender_gpu_processes": ("gauge", "GPU processes placed on the GPU"),
    "labtender_gpu_claimed": ("gauge", "1 when someone holds a claim on the GPU"),
    "labtender_gpu_queue_length": ("gauge", "Claims waiting for the GPU"),
    "labtender_gpu_flag": ("gauge", "1 for each raised reconciliation flag"),
    "labtender_service_up": ("gauge", "1 when a host service answered its health check"),
    "labtender_run_progress_ratio": ("gauge", "Progress of a running run, 0 to 1"),
    "labtender_run_eta_seconds": ("gauge", "Seconds until a running run is expected to finish"),
    "labtender_charter_active": ("gauge", "1 for each charter not yet ended, by state"),
    "labtender_charter_cost_dollars": ("gauge", "What an active charter has cost so far"),
    "labtender_charter_adrift": ("gauge", "1 for each yacht whose destroy failed (alert on it)"),
    "labtender_budget_reserved_dollars": ("gauge", "Reserved charter spend per project and limit"),
    "labtender_budget_limit_dollars": ("gauge", "Charter limit per project"),
}


def _labels(**kv) -> str:
    def esc(v) -> str:
        return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in kv.items()) + "}"


def exposition(store: Store, ledger: Ledger, now: float | None = None, charters=None) -> str:
    now = now or time.time()
    series: dict[str, list[str]] = {name: [] for name in HELP}

    def add(metric: str, value, **labels) -> None:
        if value is not None:
            v = float(value)
            text = str(int(v)) if v.is_integer() else repr(v)  # exact, never 1e+09
            series[metric].append(f"{metric}{_labels(**labels)} {text}")

    for h in store.hosts():
        host = h["name"]
        seen = h["last_seen"]
        add(
            "labtender_host_up",
            1 if seen and now - seen < 3 * max(30, h["interval_s"]) else 0,
            host=host,
        )
        if seen:
            add("labtender_host_last_seen_seconds", round(now - seen, 1), host=host)
        for svc, c in store.service_health(host).items():
            add("labtender_service_up", 1 if c["ok"] else 0, host=host, service=svc)
        snap = store.snapshot(host) or {}
        procs = snap.get("gpu_procs", [])
        for g in snap.get("gpus", []):
            lab = {"host": host, "gpu": g["index"], "name": g.get("name") or ""}
            mib = 1048576
            add("labtender_gpu_utilization_percent", g.get("util_pct"), **lab)
            if g.get("mem_used_mib") is not None:
                add("labtender_gpu_memory_used_bytes", g["mem_used_mib"] * mib, **lab)
            if g.get("mem_total_mib") is not None:
                add("labtender_gpu_memory_total_bytes", g["mem_total_mib"] * mib, **lab)
            add("labtender_gpu_power_watts", g.get("power_w"), **lab)
            add("labtender_gpu_temperature_celsius", g.get("temp_c"), **lab)
            add(
                "labtender_gpu_processes",
                sum(1 for p in procs if p.get("card") == g["index"]),
                **lab,
            )
            view = ledger.resource_view(f"{host}/gpu{g['index']}")
            add("labtender_gpu_claimed", 1 if view["holders"] else 0, host=host, gpu=g["index"])
            add("labtender_gpu_queue_length", len(view["queue"]), host=host, gpu=g["index"])
            for f in view["flags"]:
                add("labtender_gpu_flag", 1, host=host, gpu=g["index"], kind=f["kind"])
        for r in store.runs(host, since=now - 72 * 3600):
            if r.get("state") != "running":
                continue
            if r.get("pct") is not None:
                add(
                    "labtender_run_progress_ratio",
                    round(r["pct"] / 100, 4),
                    host=host,
                    run=r["name"],
                )
            if r.get("finish_at"):
                add(
                    "labtender_run_eta_seconds",
                    max(0, round(r["finish_at"] - now)),
                    host=host,
                    run=r["name"],
                )
    if charters is not None:
        for c in charters.charters(ACTIVE_CHARTER):
            lab = {"charter": c["id"], "project": c["project"], "profile": c["profile"]}
            add("labtender_charter_active", 1, state=c["state"], **lab)
            add("labtender_charter_cost_dollars", round(c["cost_so_far"], 4), **lab)
            if c["state"] == "adrift":
                add("labtender_charter_adrift", 1, ref=c["ref"] or "", **lab)
        for project in charters.projects():
            u = charters.usage(project, now)
            for limit in ("monthly_usd", "daily_usd", "weekly_usd"):
                window = limit.removesuffix("_usd")
                add(
                    "labtender_budget_reserved_dollars",
                    u["reserved"][limit],
                    project=project,
                    window=window,
                )
                add(
                    "labtender_budget_limit_dollars",
                    u["limits"][limit],
                    project=project,
                    window=window,
                )
    lines = []
    for name, (kind, text) in HELP.items():
        lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}", *series[name]]
    return "\n".join(lines) + "\n"
