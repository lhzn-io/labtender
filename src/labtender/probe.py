"""One-shot fleet probe: prints one JSON document describing this node, then exits.

The labtender service pipes this file to ``python3 -I -`` over SSH, so it must stay a single
standard-library-only script that runs on Python 3.8 or later and leaves nothing
behind. Arguments arrive as one base64-encoded JSON object in ``sys.argv[1]``.

It reports:

* the host: platform (``wsl``, ``jetson``, ``linux``, ``macos``), CPU load, memory, free disk
  in the home folder, and on Linux the network rate (bytes per second in and out);
* each GPU, from ``nvidia-smi`` where it works, completed on Jetson from sysfs,
  thermal zones and one ``tegrastats`` line, and on macOS from ``ioreg``;
* each process using a GPU (mapping ``libcuda.so`` or holding ``/dev/dxg``) with
  its ``CUDA_VISIBLE_DEVICES`` and ``CUDA_DEVICE_ORDER``, so the labtender service can place it;
* the tails of requested run logs and their small marker files.

Only processes owned by the probing user can be inspected; others are counted.
"""

import base64
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

SCHEMA = 1
NVSMI_FIELDS = [
    "index",
    "uuid",
    "name",
    "pci.bus_id",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "power.draw",
    "enforced.power.limit",
    "temperature.gpu",
    "clocks.gr",
    "clocks.max.gr",
    "display_active",
]
NVSMI_MIN_FIELDS = ["index", "uuid", "name", "utilization.gpu", "memory.used", "memory.total"]
JETSON_LOAD_PATHS = [
    "/sys/devices/platform/gpu.0/load",
    "/sys/devices/platform/bus@0/17000000.gpu/load",
    "/sys/devices/gpu.0/load",
]


def read(path, mode="r", limit=None):
    try:
        with open(path, mode) as f:
            return f.read(limit) if limit else f.read()
    except (OSError, ValueError):
        return None


def run(cmd, timeout=10):
    try:
        out = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def num(v):
    v = (v or "").strip().strip("[]")
    try:
        return float(v)
    except ValueError:
        return None


def detect_platform():
    if sys.platform == "darwin":
        return "macos"
    if os.path.exists("/etc/nv_tegra_release"):
        return "jetson"
    if "microsoft" in (read("/proc/version") or "").lower():
        return "wsl"
    return "linux"


# ---------------------------------------------------------------- host


VIRTUAL_NICS = ("lo", "docker", "veth", "br-", "virbr", "cni", "flannel", "tailscale")


def linux_net_bytes():
    """Bytes received and sent so far on the physical-looking interfaces, from /proc/net/dev."""
    rx = tx = 0
    for line in (read("/proc/net/dev") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        f = rest.split()
        if len(f) < 9 or name.startswith(VIRTUAL_NICS):
            continue
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


def linux_window(interval=0.5):
    """CPU busy percentage and network bytes per second over one short window."""

    def sample():
        line = (read("/proc/stat") or "cpu 0 0 0 0").splitlines()[0]
        v = [int(x) for x in line.split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v[:8]) - idle, idle

    b0, i0 = sample()
    r0, t0 = linux_net_bytes()
    start = time.time()
    time.sleep(interval)
    b1, i1 = sample()
    r1, t1 = linux_net_bytes()
    dt = max(1e-3, time.time() - start)
    total = (b1 - b0) + (i1 - i0)
    busy = round(100.0 * (b1 - b0) / total, 1) if total > 0 else None
    return busy, round((r1 - r0) / dt), round((t1 - t0) / dt)


def disk_free(path=None):
    """Free and total bytes on the file system holding the home folder (where work lands)."""
    try:
        u = shutil.disk_usage(path or os.path.expanduser("~"))
    except OSError:
        return None, None
    return u.free, u.total


def linux_meminfo():
    m = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            m[k] = int(parts[0])
    return m.get("MemTotal"), m.get("MemAvailable")


def host_linux(plat):
    total, avail = linux_meminfo()
    up = num((read("/proc/uptime") or "").split(" ")[0])
    model = None
    for line in (read("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name") or line.startswith("Model"):
            model = line.split(":", 1)[1].strip()
            break
    if model is None:
        model = (read("/proc/device-tree/model") or "").strip("\0 \n") or None
    busy, rx_bps, tx_bps = linux_window()
    free, size = disk_free()
    return {
        "hostname": platform.node(),
        "platform": plat,
        "os": platform.platform(),
        "arch": platform.machine(),
        "cpu_model": model,
        "ncpu": os.cpu_count(),
        "loadavg": list(os.getloadavg()),
        "cpu_busy_pct": busy,
        "mem_total_kb": total,
        "mem_avail_kb": avail,
        "uptime_s": up,
        "net_rx_bps": rx_bps,
        "net_tx_bps": tx_bps,
        "disk_free_b": free,
        "disk_total_b": size,
    }


def sysctl(name):
    out = run(["sysctl", "-n", name])
    return out.strip() if out else None


def host_macos():
    total = num(sysctl("hw.memsize"))
    page = num(sysctl("hw.pagesize")) or 16384.0
    avail = None
    vm = run(["vm_stat"])
    if vm:
        pages = {}
        for line in vm.splitlines():
            k, _, v = line.partition(":")
            v = v.strip().rstrip(".")
            if v.isdigit():
                pages[k.strip()] = int(v)
        free = sum(
            pages.get(k, 0)
            for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
        )
        avail = int(free * page / 1024)
    ncpu = os.cpu_count() or 1
    busy = None
    ps = run(["ps", "-A", "-o", "%cpu="])
    if ps:
        busy = round(min(100.0, sum(num(x) or 0.0 for x in ps.split()) / ncpu), 1)
    boot = sysctl("kern.boottime")
    up = None
    if boot:
        m = re.search(r"sec = (\d+)", boot)
        if m:
            up = time.time() - int(m.group(1))
    return {
        "hostname": platform.node(),
        "platform": "macos",
        "os": platform.platform(),
        "arch": platform.machine(),
        "cpu_model": sysctl("machdep.cpu.brand_string"),
        "ncpu": ncpu,
        "loadavg": list(os.getloadavg()),
        "cpu_busy_pct": busy,
        "mem_total_kb": int(total / 1024) if total else None,
        "mem_avail_kb": avail,
        "uptime_s": up,
        "disk_free_b": disk_free()[0],
        "disk_total_b": disk_free()[1],
    }


# ---------------------------------------------------------------- GPUs


def parse_nvsmi_csv(text, fields):
    """Rows of ``nvidia-smi --query-gpu`` CSV (noheader, nounits) as dicts of raw strings."""
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(fields):
            continue
        rows.append(dict(zip(fields, cells)))
    return rows


def gpu_from_nvsmi(row):
    disp = row.get("display_active")
    return {
        "index": int(num(row.get("index")) or 0),
        "uuid": row.get("uuid"),
        "name": row.get("name"),
        "bus_id": row.get("pci.bus_id"),
        "util_pct": num(row.get("utilization.gpu")),
        "mem_used_mib": num(row.get("memory.used")),
        "mem_total_mib": num(row.get("memory.total")),
        "power_w": num(row.get("power.draw")),
        "power_limit_w": num(row.get("enforced.power.limit")),
        "temp_c": num(row.get("temperature.gpu")),
        "clock_mhz": num(row.get("clocks.gr")),
        "clock_max_mhz": num(row.get("clocks.max.gr")),
        "display_active": (disp == "Enabled") if disp and "N/A" not in disp else None,
        "unified": False,
        "source": "nvidia-smi",
    }


def nvidia_gpus():
    if not shutil.which("nvidia-smi"):
        return []
    for fields in (NVSMI_FIELDS, NVSMI_MIN_FIELDS):
        out = run(
            ["nvidia-smi", "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"]
        )
        if out is not None:
            return [gpu_from_nvsmi(r) for r in parse_nvsmi_csv(out, fields)]
    return []


def gpus_from_allsmi(doc, unified=False):
    """GPU rows from ``all-smi snapshot --format json`` (schema 1)."""
    out = []
    for i, g in enumerate((doc or {}).get("gpus") or []):
        if (g.get("device_type") or "GPU").upper() != "GPU":
            continue
        used, total = g.get("used_memory"), g.get("total_memory")
        index = g.get("index", g.get("gpu_index", i))
        out.append(
            {
                "index": int(index) if str(index).isdigit() else i,
                "uuid": g.get("uuid"),
                "name": g.get("name"),
                "bus_id": None,
                "util_pct": g.get("utilization"),
                "mem_used_mib": round(used / 1048576.0) if isinstance(used, (int, float)) else None,
                "mem_total_mib": round(total / 1048576.0)
                if isinstance(total, (int, float)) and total
                else None,
                "power_w": g.get("power_consumption"),
                "power_limit_w": None,
                "temp_c": g.get("temperature"),
                "clock_mhz": g.get("frequency"),
                "clock_max_mhz": None,
                "display_active": None,
                "unified": unified,
                "source": "all-smi",
            }
        )
    return out


def allsmi_gpus(unified=False):
    if not shutil.which("all-smi"):
        return []
    out = run(
        [
            "all-smi",
            "snapshot",
            "--format",
            "json",
            "--pretty=false",
            "--include",
            "gpu",
            "--timeout-ms",
            "5000",
        ],
        timeout=15,
    )
    if not out:
        return []
    try:
        return gpus_from_allsmi(json.loads(out), unified)
    except ValueError:
        return []


def fill_missing(gpus, extra):
    """Fill empty fields of ``gpus`` from ``extra`` rows with the same index."""
    by_index = {g["index"]: g for g in extra}
    for g in gpus:
        other = by_index.get(g["index"])
        if not other:
            continue
        for k, v in other.items():
            if g.get(k) is None and v is not None and k != "source":
                g[k] = v
    return gpus


def parse_tegrastats(line):
    """GPU load, power and temperature from one ``tegrastats`` line."""
    out = {}
    m = re.search(r"GR3D_FREQ (\d+)%", line)
    if m:
        out["util_pct"] = float(m.group(1))
    m = re.search(r"\bVDD_GPU(?:_SOC)? (\d+)mW", line)
    if m:
        out["power_w"] = int(m.group(1)) / 1000.0
    m = re.search(r"\bgpu@(-?[\d.]+)C", line)
    if m:
        out["temp_c"] = float(m.group(1))
    m = re.search(r"\bRAM (\d+)/(\d+)MB", line)
    if m:
        out["ram_used_mib"] = float(m.group(1))
        out["ram_total_mib"] = float(m.group(2))
    return out


def tegrastats_line(timeout=3.0):
    if not shutil.which("tegrastats"):
        return None
    try:
        p = subprocess.Popen(
            ["tegrastats", "--interval", "500"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        return None
    line = None
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = p.stdout.readline()
            if raw:
                line = raw.decode("utf-8", "replace").strip()
                break
    finally:
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
    return line


def thermal_zone(kind):
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        if (read(zone + "/type") or "").strip().lower() == kind:
            v = num(read(zone + "/temp"))
            if v is not None:
                return v / 1000.0
    return None


def jetson_gpus(gpus, host):
    """Complete (or create) the single Jetson GPU from sysfs, thermal zones and tegrastats."""
    g = (
        gpus[0]
        if gpus
        else {
            "index": 0,
            "uuid": None,
            "name": "Jetson GPU",
            "bus_id": None,
            "util_pct": None,
            "mem_used_mib": None,
            "mem_total_mib": None,
            "power_w": None,
            "power_limit_w": None,
            "temp_c": None,
            "clock_mhz": None,
            "clock_max_mhz": None,
            "display_active": None,
            "source": "sysfs",
        }
    )
    g["unified"] = True
    model = (read("/proc/device-tree/model") or "").strip("\0 \n")
    if model and (not g.get("name") or "nvgpu" in g["name"]):
        g["name"] = model
    if g.get("util_pct") is None:
        for path in JETSON_LOAD_PATHS:
            v = num(read(path))
            if v is not None:
                g["util_pct"] = round(v / 10.0, 1)  # per mille
                break
    if g.get("temp_c") is None:
        g["temp_c"] = thermal_zone("gpu-thermal")
    t = parse_tegrastats(tegrastats_line() or "")
    for k in ("util_pct", "power_w", "temp_c"):
        if g.get(k) is None and t.get(k) is not None:
            g[k] = t[k]
    # Unified memory: the GPU's pool is system RAM.
    if host.get("mem_total_kb"):
        g["mem_total_mib"] = round(host["mem_total_kb"] / 1024.0)
        g["mem_used_mib"] = round((host["mem_total_kb"] - (host.get("mem_avail_kb") or 0)) / 1024.0)
    return [g]


def parse_ioreg(text):
    out = {}
    for key, name in (
        ("Device Utilization %", "util_pct"),
        ("In use system memory", "in_use_bytes"),
        ("Alloc system memory", "alloc_bytes"),
    ):
        m = re.search(r'"' + re.escape(key) + r'"=(\d+)', text or "")
        if m:
            out[name] = float(m.group(1))
    m = re.search(r'"model"\s*=\s*"([^"]+)"', text or "")
    if m:
        out["model"] = m.group(1)
    return out


def macos_gpus(host):
    text = run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])
    if not text:
        return []
    io = parse_ioreg(text)
    total = host.get("mem_total_kb")
    used = io.get("in_use_bytes")
    return [
        {
            "index": 0,
            "uuid": None,
            "name": io.get("model") or host.get("cpu_model") or "Apple GPU",
            "bus_id": None,
            "util_pct": io.get("util_pct"),
            "mem_used_mib": round(used / 1048576.0) if used is not None else None,
            "mem_total_mib": round(total / 1024.0) if total else None,
            "power_w": None,
            "power_limit_w": None,
            "temp_c": None,
            "clock_mhz": None,
            "clock_max_mhz": None,
            "display_active": None,
            "unified": True,
            "source": "ioreg",
        }
    ]


# ---------------------------------------------------------------- GPU processes


def open_logs(pid, lognames):
    """Paths of the files named in ``lognames`` that the process holds open."""
    found = []
    if not lognames:
        return found
    try:
        fds = os.listdir("/proc/%d/fd" % pid)
    except OSError:
        return found
    for fd in fds:
        try:
            path = os.readlink("/proc/%d/fd/%s" % (pid, fd))
        except OSError:
            continue
        if os.path.basename(path) in lognames and path not in found:
            found.append(path)
            if len(found) >= 8:
                break
    return found


def gpu_procs_linux(lognames=()):
    """Processes that map libcuda.so or hold /dev/dxg, with their CUDA pins and open run logs."""
    clk = os.sysconf("SC_CLK_TCK")
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    up = num((read("/proc/uptime") or "0").split(" ")[0]) or 0.0
    me = os.getpid()
    procs, unreadable = [], 0
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        pid = int(name)
        via = None
        maps = read("/proc/%d/maps" % pid)
        if maps is None:
            unreadable += 1
            continue
        if "libcuda.so" in maps:
            via = "libcuda"
        else:
            try:
                for fd in os.listdir("/proc/%d/fd" % pid):
                    try:
                        if os.readlink("/proc/%d/fd/%s" % (pid, fd)) == "/dev/dxg":
                            via = "dxg"
                            break
                    except OSError:
                        continue
            except OSError:
                pass
        if not via:
            continue
        env_raw = read("/proc/%d/environ" % pid, "rb")
        env = {}
        for kv in (env_raw or b"").split(b"\0"):
            if kv.startswith(b"CUDA_VISIBLE_DEVICES=") or kv.startswith(b"CUDA_DEVICE_ORDER="):
                k, _, v = kv.decode("utf-8", "replace").partition("=")
                env[k] = v
        cmd = (read("/proc/%d/cmdline" % pid, "rb") or b"").replace(b"\0", b" ")
        stat = read("/proc/%d/stat" % pid) or ""
        elapsed = cpu_s = rss_kb = None
        try:
            f = stat.rsplit(")", 1)[1].split()
            cpu_s = (int(f[11]) + int(f[12])) / float(clk)
            elapsed = up - int(f[19]) / float(clk)
            rss_kb = int(f[21]) * page_kb
        except (IndexError, ValueError):
            pass
        procs.append(
            {
                "pid": pid,
                "comm": (read("/proc/%d/comm" % pid) or "?").strip(),
                "cmdline": cmd.decode("utf-8", "replace").strip()[:300],
                "via": via,
                "open_logs": open_logs(pid, lognames),
                "env_readable": env_raw is not None,
                "cuda_visible_devices": env.get("CUDA_VISIBLE_DEVICES"),
                "cuda_device_order": env.get("CUDA_DEVICE_ORDER"),
                "elapsed_s": elapsed,
                "cpu_s": cpu_s,
                "rss_kb": rss_kb,
            }
        )
    return procs, unreadable


# ---------------------------------------------------------------- run logs


def tail(path, nbytes, head_bytes=0):
    """The last ``nbytes`` of a file, and its first ``head_bytes`` when asked."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(head_bytes) if head_bytes else b""
            if size > nbytes:
                f.seek(size - nbytes)
            else:
                f.seek(0)
            data = f.read(nbytes)
        out = {
            "size": size,
            "mtime": os.path.getmtime(path),
            "tail": data.decode("utf-8", "replace"),
        }
        if head_bytes:
            out["head"] = head.decode("utf-8", "replace")
        return out
    except OSError:
        return None


def collect_runs(specs, now):
    out = []
    for i, spec in enumerate(specs or []):
        pattern = os.path.expanduser(spec.get("glob", ""))
        files = spec.get("files") or []
        markers = spec.get("markers") or []
        max_age = float(spec.get("max_age_h", 72)) * 3600.0
        found = []
        for d in glob.glob(pattern):
            if not os.path.isdir(d):
                continue
            # Recency comes from the watched files; other tools may touch the folder itself.
            mt = []
            for name in files + markers:
                p = os.path.join(d, name)
                if os.path.exists(p):
                    mt.append(os.path.getmtime(p))
            last = max(mt) if mt else os.path.getmtime(d)
            if now - last <= max_age:
                found.append((last, d))
        found.sort(reverse=True)
        for last, d in found[: int(spec.get("max_dirs", 8))]:
            entry = {"spec": i, "dir": d, "mtime": last, "files": {}, "markers": {}}
            for name in files:
                entry["files"][name] = tail(
                    os.path.join(d, name),
                    int(spec.get("tail_bytes", 65536)),
                    int(spec.get("head_bytes", 0)),
                )
            for name in markers:
                txt = read(os.path.join(d, name), limit=4096)
                if txt is not None:
                    entry["markers"][name] = txt
            out.append(entry)
    return out


# ---------------------------------------------------------------- main


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = {}
    if argv:
        args = json.loads(base64.b64decode(argv[0]).decode("utf-8"))
    t0 = time.time()
    plat = detect_platform()
    errors = []
    host = host_macos() if plat == "macos" else host_linux(plat)
    # Where the hub reached this host: the server side of the SSH connection, if any.
    ssh = (os.environ.get("SSH_CONNECTION") or "").split()
    host["reached_at"] = ssh[2] if len(ssh) == 4 else None
    # Ages of the files the hub asked about (a nightly job's marker, say), on this host's clock.
    ages = {}
    for path in args.get("files") or []:
        try:
            ages[path] = max(0.0, t0 - os.stat(os.path.expanduser(path)).st_mtime)
        except OSError:
            ages[path] = None
    if ages:
        host["file_ages"] = ages
    procs, unreadable = [], 0
    if plat == "macos":
        # all-smi reads power and temperature without sudo; ioreg is the fallback.
        gpus = allsmi_gpus(unified=True) or macos_gpus(host)
    else:
        gpus = nvidia_gpus()
        if plat == "jetson":
            gpus = jetson_gpus(fill_missing(gpus, allsmi_gpus(unified=True)), host)
        elif not gpus:
            gpus = allsmi_gpus()
        lognames = set(f for r in args.get("runs") or [] for f in r.get("files") or [])
        procs, unreadable = gpu_procs_linux(lognames)
    if not gpus:
        errors.append("no GPU source answered")
    doc = {
        "schema": SCHEMA,
        "time": time.time(),
        "host": host,
        "gpus": gpus,
        "gpu_procs": procs,
        "procs_unreadable": unreadable,
        "runs": collect_runs(args.get("runs"), time.time()),
        "probe_ms": None,
        "errors": errors,
    }
    doc["probe_ms"] = round((time.time() - t0) * 1000)
    sys.stdout.write(json.dumps(doc))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
