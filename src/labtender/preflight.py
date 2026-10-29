"""Check a GPU before starting a run on it: local occupancy and memory, then the ledger.

Exit codes (a superset of a plain occupancy check, so a run wrapper can switch to it):

    0  go: the GPU is free (and, with a labtender service, held by you)
    2  the GPU or nvidia-smi is missing
    3  another process occupies the GPU
    4  the GPU is short of memory
    5  the ledger says the GPU is not yours (held by someone else, or you are queued)

Occupancy comes from this machine's /proc, because nvidia-smi under WSL lists no processes: a
process mapping libcuda.so or holding /dev/dxg is a GPU user, placed on a GPU by its
CUDA_VISIBLE_DEVICES. One not pinned to a single GPU counts on every GPU. With ``--claim``,
preflight claims the GPU (or joins its queue) on the labtender service first. If the service
cannot be reached it falls back to the local check and says so.
"""

from __future__ import annotations

import os
import socket
import sys

from . import probe
from .client import Hub, HubError
from .collector import attribute

GO, MISSING, OCCUPIED, SHORT, NOT_YOURS = 0, 2, 3, 4, 5


def _own_tree() -> set[int]:
    """This process and its ancestors, which must not count as occupants."""
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        stat = probe.read(f"/proc/{pid}/stat") or ""
        try:
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
    return pids


def local_check(card: int, need_gib: float, ignore: set[str]) -> tuple[int, list[str]]:
    gpus = probe.nvidia_gpus()
    if not gpus:
        return MISSING, ["no nvidia-smi, or it sees no GPU"]
    g = next((g for g in gpus if g["index"] == card), None)
    if g is None:
        return MISSING, [f"no GPU with nvidia-smi index {card}"]
    if probe.detect_platform() == "jetson":
        g = probe.jetson_gpus([g], probe.host_linux("jetson"))[0]
        free = None  # unified memory: there is no per-GPU free figure to test
    elif g["mem_total_mib"] is not None and g["mem_used_mib"] is not None:
        free = g["mem_total_mib"] - g["mem_used_mib"]
    else:
        free = None
    lines = [
        f"GPU {card} ({g['name']}): "
        + (f"{free:.0f} MiB free" if free is not None else "no per-GPU free memory")
        + f"; this run needs {need_gib:g} GiB"
    ]
    procs, _ = probe.gpu_procs_linux()
    doc = {"gpus": gpus, "gpu_procs": procs}
    attribute(doc)
    mine = _own_tree()
    rc = GO
    for p in doc["gpu_procs"]:
        if p["pid"] in mine or any(s in p["cmdline"] for s in ignore):
            continue
        if p["card"] == card or p["card"] is None:
            where = f"on GPU {card}" if p["card"] == card else p["note"]
            lines.append(f"OCCUPIED by pid {p['pid']} {p['comm']} ({where}): {p['cmdline'][:110]}")
            rc = OCCUPIED
    if rc == GO and free is not None and free < need_gib * 1024:
        lines.append(f"SHORT: {free:.0f} MiB free, {need_gib * 1024:.0f} MiB needed")
        rc = SHORT
    return rc, lines


def ledger_check(
    hub: Hub, resource: str, claim: bool, tier: str, est_hours: float | None, note: str
) -> tuple[int, list[str]]:
    me = hub.whoami()["identity"]
    if claim:
        c = hub.claim(resource, tier=tier, est_hours=est_hours, note=note)
        if c["state"] == "held":
            return GO, [f"holding {resource} as claim {c['id']} ({me})"]
    view = hub.resource(resource)
    held = [c for c in view["holders"] if c["owner"] == me]
    if held:
        return GO, [f"holding {resource} as claim {held[0]['id']} ({me})"]
    holders = ", ".join(c["owner"] for c in view["holders"]) or "nobody"
    queue = [c["owner"] for c in view["queue"]]
    pos = f"; you are number {queue.index(me) + 1} in the queue" if me in queue else ""
    hint = "" if claim or pos else f"; claim it first: labtender claim {resource}"
    return NOT_YOURS, [f"{resource} is held by {holders}{pos}{hint}"]


def run(
    card: int,
    need_gib: float,
    *,
    host: str | None = None,
    claim: bool = False,
    tier: str = "normal",
    est_hours: float | None = None,
    note: str = "",
    ignore: tuple[str, ...] = (),
    hub: Hub | None = None,
) -> int:
    host = host or os.environ.get("LABTENDER_HOST") or socket.gethostname()
    resource = f"{host}/gpu{card}"
    rc, lines = local_check(card, need_gib, set(ignore))
    if rc in (GO, OCCUPIED, SHORT):
        hub = hub or Hub()
        try:
            lrc, more = ledger_check(hub, resource, claim, tier, est_hours, note)
            lines += more
            if rc == GO:
                rc = lrc
        except HubError as e:
            lines.append(f"ledger not consulted: {e}")
    for line in lines:
        print(f"labtender preflight: {line}")
    print(f"labtender preflight: {'go' if rc == GO else 'stop'} (exit {rc})")
    sys.stdout.flush()
    return rc
