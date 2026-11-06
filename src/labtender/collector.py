"""Polls each registered host with the probe and records what it sees.

Each host is polled on its own interval (30 s at the least), one probe at a time, over plain
``ssh`` (honouring ``~/.ssh/config``, with connection sharing) or locally. A host that fails
backs off up to ten intervals. Nothing is installed or left on the node.

Host services with a health path are checked from the labtender service's own machine, once per
poll of their host, in a task of their own so a hung service never delays a probe.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import shutil
import time
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path

from . import parsers
from .backup import backup_if_due
from .db import Store, health_url
from .ledger import Ledger

log = logging.getLogger("labtender.collector")
PROBE_TIMEOUT_S = 30
MAX_PROBE_BYTES = 8 * 1024 * 1024  # a probe's answer is a few KiB to a few hundred KiB
REAP_TIMEOUT_S = 10
MIN_INTERVAL_S = 30
WINDOWS_EVERY_S = 120  # the Windows sample costs a PowerShell start-up, so it runs less often
WINDOWS_TIMEOUT_S = 60
HEALTH_TIMEOUT_S = 5.0
# Where WSL finds Windows PowerShell when the Windows PATH is not appended (ssh sessions).
WSL_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
# Windows processes that run real (non-display) GPU work; the rest count as desktop.
WINDOWS_COMPUTE = {
    "python",
    "pythonw",
    "python3",
    "julia",
    "ollama",
    "blender",
    "llama-server",
    "comfyui",
    "tritonserver",
    "nvcc",
}


def probe_source() -> bytes:
    return resources.files("labtender").joinpath("probe.py").read_bytes()


def windows_command(host: dict, control_dir: Path) -> list[str]:
    """powershell.exe with the Windows GPU sampler as an encoded command, over the transport."""
    script = resources.files("labtender").joinpath("windows_gpu.ps1").read_text("utf-8")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    args = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"]
    args.append(encoded)
    if host["transport"] == "local":
        return [shutil.which("powershell.exe") or WSL_POWERSHELL, *args]
    # An ssh session into WSL may lack the Windows PATH; fall back to the standard location.
    remote = [f'"$(command -v powershell.exe || echo {WSL_POWERSHELL})"', *args]
    control_dir.mkdir(parents=True, exist_ok=True)
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_dir}/%C",
        "-o",
        "ControlPersist=300",
        "--",  # the destination is never read as an option, whatever is stored
        host["ssh"],
        " ".join(remote),
    ]


async def read_bounded(proc, data: bytes | None = None, limit: int = MAX_PROBE_BYTES):
    """A subprocess's stdout and stderr, like ``communicate``, but stopped once stdout passes
    ``limit`` bytes: a monitored host answers with whatever it likes, and must not be able to
    fill the hub's memory."""

    async def read(stream, cap: int) -> bytes:
        buf = bytearray()
        while chunk := await stream.read(65536):
            buf += chunk
            if len(buf) > cap:
                raise RuntimeError(f"answer over {cap // (1024 * 1024)} MiB")
        return bytes(buf)

    async def drain_err(stream) -> bytes:
        buf = bytearray()
        while chunk := await stream.read(65536):
            buf = (buf + chunk)[-65536:]  # only the end is ever shown
        return bytes(buf)

    tasks = []
    try:
        if data is not None:
            proc.stdin.write(data)
            await proc.stdin.drain()
            proc.stdin.close()
        tasks.append(asyncio.ensure_future(read(proc.stdout, limit)))
        if proc.stderr:
            tasks.append(asyncio.ensure_future(drain_err(proc.stderr)))
        out, *rest = await asyncio.gather(*tasks)
        await proc.wait()
    except BaseException:  # too much, a timeout from the caller, or anything else
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await reap(proc)
        raise
    return out, rest[0] if rest else b""


async def reap(proc) -> None:
    """Kill a subprocess and collect it. Since Python 3.12 ``wait()`` returns only once the
    process's pipes have closed, and a pipe nobody reads stops at 128 KiB and never does, so what
    is left in them is read and thrown away first. A pipe held open by some other process is
    given up on after ``REAP_TIMEOUT_S``, so one host can never stall its poll loop."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # already gone

    async def discard(stream) -> None:
        while await stream.read(65536):
            pass

    streams = [s for s in (proc.stdout, proc.stderr) if s is not None]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(discard(s) for s in streams), proc.wait()), REAP_TIMEOUT_S
        )
    except TimeoutError:
        pass


async def run_windows(host: dict, control_dir: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *windows_command(host, control_dir),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(read_bounded(proc), WINDOWS_TIMEOUT_S)
    except TimeoutError:  # read_bounded has already killed and collected the process
        raise RuntimeError(f"Windows sample timed out after {WINDOWS_TIMEOUT_S} s") from None
    if proc.returncode != 0:
        raise RuntimeError(f"Windows sample exited {proc.returncode}")
    return json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is an answer; never follow it to another host


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_url(url: str, timeout: float = HEALTH_TIMEOUT_S) -> tuple[bool, str]:
    """GET a health URL: (ok, detail). Any 2xx or 3xx answer counts as up."""
    req = urllib.request.Request(url, headers={"User-Agent": "labtender-health"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            r.read(65536)
            return 200 <= r.status < 400, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return 300 <= e.code < 400, f"HTTP {e.code}"
    except (TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return False, f"no answer in {timeout:g} s"
        return False, str(reason)[:200] or type(e).__name__


def pci_bus(bus_id: str | None) -> int | None:
    """The bus number from an nvidia-smi pci.bus_id such as 00000000:0A:00.0."""
    try:
        return int((bus_id or "").split(":")[1], 16)
    except (IndexError, ValueError):
        return None


def attach_windows(doc: dict, sample: dict) -> None:
    """Put each Windows process on the GPU with the same PCI bus, classed wsl/compute/desktop."""
    by_bus = {pci_bus(g.get("bus_id")): g for g in doc.get("gpus", [])}
    for g in doc.get("gpus", []):
        g["windows_procs"] = []
    for p in sample.get("procs", []):
        g = by_bus.get(p.get("bus"))
        if g is None:
            continue
        name = p.get("name") or "?"
        kind = (
            "wsl"
            if name.lower() == "vmwp"
            else "compute"
            if name.lower() in WINDOWS_COMPUTE
            else "desktop"
        )
        g["windows_procs"].append(dict(p, kind=kind))
    for g in doc.get("gpus", []):
        g["windows_procs"].sort(key=lambda p: -(p.get("mem_mib") or 0))
    doc.setdefault("errors", []).extend(f"windows: {e}" for e in sample.get("errors", []))


def probe_command(host: dict, arg: str, control_dir: Path) -> list[str]:
    remote = ["python3", "-I", "-", arg]
    if host["transport"] == "local":
        return remote
    control_dir.mkdir(parents=True, exist_ok=True)
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_dir}/%C",
        "-o",
        "ControlPersist=300",
        "--",  # the destination is never read as an option, whatever is stored
        host["ssh"],
        " ".join(remote),
    ]


async def run_probe(host: dict, request: dict, control_dir: Path) -> dict:
    arg = base64.b64encode(json.dumps(request).encode()).decode()
    proc = await asyncio.create_subprocess_exec(
        *probe_command(host, arg, control_dir),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(read_bounded(proc, probe_source()), PROBE_TIMEOUT_S)
    except TimeoutError:  # read_bounded has already killed and collected the process
        raise RuntimeError(f"probe timed out after {PROBE_TIMEOUT_S} s") from None
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"probe exited {proc.returncode}: {msg[-1] if msg else ''}")
    return clean_probe(json.loads(out))


# Probe fields the board does arithmetic on or puts into markup: a host answers with whatever
# it likes (anyone with a shell there can put their own python3 first on the PATH), so these
# are made numbers here, once, and everything else that reaches the board is escaped there.
GPU_NUMBERS = (
    "util_pct",
    "mem_used_mib",
    "mem_total_mib",
    "power_w",
    "power_limit_w",
    "temp_c",
    "clock_mhz",
    "clock_max_mhz",
)
PROC_NUMBERS = ("elapsed_s", "cpu_s", "rss_kb", "gpu_mem_mib", "used_mib")
MAX_GPUS = 64
MAX_PROCS = 1024
MAX_RUNS = 256


def number(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
    return v if math.isfinite(v) else None


def clean_probe(doc) -> dict:
    """A probe document with the shape the hub relies on: lists capped, GPU indexes and PIDs
    whole numbers, readings numbers or None. Raises ValueError for anything not a probe."""
    if not isinstance(doc, dict):
        raise ValueError("probe answer is not an object")
    t = number(doc.get("time"))
    if t is None:
        raise ValueError("probe answer has no time")
    doc["time"] = t
    gpus = []
    for i, g in enumerate(list(doc.get("gpus") or [])[:MAX_GPUS]):
        if not isinstance(g, dict):
            continue
        idx = number(g.get("index"))
        g["index"] = int(idx) if idx is not None and 0 <= idx < 1024 else i
        for k in GPU_NUMBERS:
            if k in g:
                g[k] = number(g[k])
        gpus.append(g)
    doc["gpus"] = gpus
    procs = []
    for p in list(doc.get("gpu_procs") or [])[:MAX_PROCS]:
        if not isinstance(p, dict):
            continue
        pid = number(p.get("pid"))
        if pid is None or pid < 0:
            continue
        p["pid"] = int(pid)
        for k in PROC_NUMBERS:
            if k in p:
                p[k] = number(p[k])
        procs.append(p)
    doc["gpu_procs"] = procs
    doc["runs"] = [r for r in list(doc.get("runs") or [])[:MAX_RUNS] if isinstance(r, dict)]
    doc["errors"] = [str(e)[:500] for e in list(doc.get("errors") or [])[:50]]
    if not isinstance(doc.get("host"), dict):
        doc["host"] = {}
    return doc


def attribute(doc: dict) -> None:
    """Place each GPU process on its GPU from its CUDA pin, in place.

    A process is placed when pinned by UUID, or by index with CUDA_DEVICE_ORDER=PCI_BUS_ID
    (then the index is the nvidia-smi index). On a single-GPU node every process is on GPU 0.
    Anything else is unpinned and counts on every GPU.
    """
    gpus = doc.get("gpus", [])
    for p in doc.get("gpu_procs", []):
        vis = p.get("cuda_visible_devices")
        p["card"], p["note"] = None, None
        toks = [t.strip() for t in (vis or "").split(",") if t.strip()]
        if not p.get("env_readable"):
            p["note"] = "environment not readable"
        elif vis is not None and (not toks or toks[0] == "-1"):
            p["note"] = "GPUs hidden"
        elif toks and toks[0].startswith("GPU-"):
            p["card"] = next(
                (g["index"] for g in gpus if (g.get("uuid") or "").startswith(toks[0])), None
            )
        elif (
            toks
            and toks[0].isdigit()
            and (p.get("cuda_device_order") == "PCI_BUS_ID" or len(gpus) == 1)
        ):
            p["card"] = int(toks[0])
        if p["card"] is None and len(gpus) == 1 and p["note"] is None:
            p["card"] = gpus[0]["index"]
        if p["card"] is None and p["note"] is None:
            p["note"] = "not pinned to one GPU; counts on every GPU"


def link_runs(doc: dict, runs: list[dict]) -> None:
    """Give each run the GPU process that holds its log open, and that process's GPU.

    A run whose log a live GPU process holds open is running, however long the log has been
    quiet (slow first steps, long output writes).
    """
    for r in runs:
        prefix = r["dir"].rstrip("/") + "/"
        for p in doc.get("gpu_procs", []):
            if any(path.startswith(prefix) for path in p.get("open_logs") or []):
                r["pid"] = p["pid"]
                if r.get("card") is None:
                    r["card"] = p.get("card")
                if r.get("state") == "stale":
                    r["state"] = "running"
                break


class Collector:
    def __init__(self, store: Store, home: Path, ledger: Ledger | None = None):
        self.store = store
        self.ledger = ledger
        self.home = home
        self.control_dir = home / "ssh"
        self.specs = parsers.load_specs(home)
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._windows: dict[str, tuple[float, dict]] = {}
        self._health: dict[str, asyncio.Task] = {}
        self.health_timeout_s = HEALTH_TIMEOUT_S

    def request_for(self, host: str) -> tuple[dict, list[dict]]:
        watches = [w for w in self.store.watches(host) if w["parser"] in self.specs]
        reqs = [parsers.probe_request(self.specs[w["parser"]], w["glob"]) for w in watches]
        files = sorted({f["path"] for f in (self.store.host(host) or {}).get("files", {}).values()})
        return {"runs": reqs, "files": files}, watches

    async def poll_once(self, name: str) -> dict:
        host = self.store.host(name)
        if host is None:
            raise KeyError(name)
        request, watches = self.request_for(name)
        doc = await run_probe(host, request, self.control_dir)
        attribute(doc)
        await self._windows_sample(host, doc)
        hub_now = time.time()
        runs = []
        for entry in doc.pop("runs", []):
            try:
                w = watches[entry["spec"]]
                runs.append(parsers.interpret(self.specs[w["parser"]], entry, doc["time"], hub_now))
            except (ValueError, KeyError, IndexError, TypeError) as e:
                log.warning("%s: cannot parse run %r: %s", name, str(entry.get("dir"))[:200], e)
        link_runs(doc, runs)
        self.store.record_probe(name, doc, runs)
        if self.ledger:
            self.ledger.reconcile(name, doc)
        return doc

    async def _windows_sample(self, host: dict, doc: dict) -> None:
        """On a WSL host, add per-process GPU use from the Windows side (unless turned off)."""
        if (doc.get("host") or {}).get("platform") != "wsl":
            return
        if str(host["labels"].get("windows_gpu", "on")).lower() in ("off", "false", "0", "no"):
            return
        now = time.time()
        cached = self._windows.get(host["name"])
        if cached is None or now - cached[0] >= WINDOWS_EVERY_S:
            try:
                cached = (now, await run_windows(host, self.control_dir))
            except (RuntimeError, ValueError, OSError, IndexError) as e:
                cached = (now, {"procs": [], "errors": [str(e)]})
            self._windows[host["name"]] = cached
        attach_windows(doc, cached[1])

    async def check_services(self, name: str) -> dict[str, tuple[bool, str]]:
        """Check every service of the host that has a health path, and set its flag.

        A service with fallback addresses is tried at its primary address first, then at each
        fallback in order; the first that answers is where the service is now.
        """
        host = self.store.host(name)
        if host is None:
            return {}
        bases = {
            svc: [host["services"][svc], *host["fallbacks"].get(svc, [])]
            for svc in host["health"]
            if host["services"].get(svc)
        }

        async def one(svc: str) -> tuple[bool, str, str]:
            path = host["health"][svc]
            first = None
            for base in bases[svc]:
                try:
                    ok, detail = await asyncio.wait_for(
                        asyncio.to_thread(check_url, health_url(base, path), self.health_timeout_s),
                        self.health_timeout_s + 3,
                    )
                except Exception:  # noqa: BLE001  (a stuck check counts as no answer)
                    ok, detail = False, "check failed"
                if ok:
                    return ok, detail, base
                first = first or detail
            n = len(bases[svc]) - 1
            also = f" (and at {n} fallback{'s' if n > 1 else ''})" if n else ""
            return False, first + also, bases[svc][0]

        found = dict(zip(bases, await asyncio.gather(*(one(s) for s in bases)), strict=True))
        results = {svc: (ok, detail) for svc, (ok, detail, _) in found.items()}
        if self.store.host(name) is None:
            return results  # removed while the checks ran
        with self.store.tx():
            before = self.store.service_health(name)
            self.store.prune_health(name, list(bases))
            for svc, (ok, detail, base) in found.items():
                url = health_url(base, host["health"][svc])
                if self.store.record_health(name, svc, url, ok, detail, base=base):
                    self.store.audit(
                        "labtender",
                        "service.up" if ok else "service.down",
                        name,
                        service=svc,
                        detail=detail,
                        address=base,
                    )
                elif ok and (before.get(svc) or {}).get("base") not in (None, base):
                    self.store.audit(
                        "labtender",
                        "service.moved",
                        name,
                        service=svc,
                        address=base,
                        previous=before[svc]["base"],
                    )
        if self.ledger:
            # An optional service is down at times by design (a demo hub on a laptop): no flag.
            optional = set(host["optional"])
            self.ledger.service_flag(
                name,
                {
                    svc: detail
                    for svc, (ok, detail) in results.items()
                    if not ok and svc not in optional
                },
            )
        return results

    def _start_health(self, name: str) -> None:
        """Start this host's checks unless the last round is still running."""
        running = self._health.get(name)
        if running and not running.done():
            return

        async def guarded():
            try:
                await self.check_services(name)
            except Exception:  # noqa: BLE001  (a failed check must not end anything)
                log.exception("%s: service checks failed", name)

        self._health[name] = asyncio.create_task(guarded())

    def _health_left(self, name: str) -> bool:
        """Whether the host still has health results or a service-down flag to clear."""
        if self.store.service_health(name):
            return True
        return bool(self.ledger) and any(
            f["kind"] == "service-down" for f in self.ledger.flags(name)
        )

    async def _host_loop(self, name: str) -> None:
        failures = 0
        while not self._stop.is_set():
            host = self.store.host(name)
            if host is None:
                return
            interval = max(MIN_INTERVAL_S, host["interval_s"])
            # With no health paths left, one more round still prunes old results and clears the
            # flag; after that there is nothing to check.
            if host["health"] or self._health_left(name):
                self._start_health(name)
            try:
                await self.poll_once(name)
                failures = 0
            except Exception as e:  # noqa: BLE001  (a failed probe must not end the loop)
                failures += 1
                self.store.record_error(name, str(e))
                log.warning("%s: %s", name, e)
            delay = interval * min(10, 2 ** max(0, failures - 1)) if failures else interval
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except TimeoutError:
                pass

    async def run(self) -> None:
        """Keep one loop per registered host, picking up hosts added or removed later."""
        while not self._stop.is_set():
            self.store.expire_hosts()
            try:
                backup_if_due(self.store, self.home / "backups")
            except (OSError, Exception):  # noqa: BLE001  (a failed backup must not stop collection)
                log.exception("backup failed")
            if self.ledger:
                try:
                    self.ledger.tick()
                except Exception:  # noqa: BLE001  (keep collecting if the ledger trips)
                    log.exception("ledger tick failed")
            names = {h["name"] for h in self.store.hosts()}
            for name in names - self._tasks.keys():
                self._tasks[name] = asyncio.create_task(self._host_loop(name))
            for name in list(self._tasks):
                if self._tasks[name].done():
                    del self._tasks[name]
            try:
                await asyncio.wait_for(self._stop.wait(), 10)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        tasks = [*self._tasks.values(), *self._health.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
