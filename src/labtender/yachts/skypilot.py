"""Yachts through SkyPilot: Google Cloud, AWS and GPU clouds such as Lambda, RunPod and Vast.

SkyPilot (Apache-2.0) is installed in its own environment, for example
``uv tool install "skypilot[gcp,lambda,runpod,vast]"``, and signed in to each cloud there
(``sky check`` shows which are enabled). labtender runs ``sky_helper.py`` with SkyPilot's
Python, found from ``LABTENDER_SKY_PYTHON``, the uv tool folder, or the ``sky`` script on the
PATH.

Quotes come from SkyPilot's price catalog, so they work before any cloud is set up; offers from
clouds without credentials are marked and cannot be chartered. A launch pins the exact cloud,
region and instance type that was quoted, caps the hourly cost at the quoted rate, and sets a
provider-side hard time limit (autostop with ``down=True``, ``wait_for="none"``).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from . import Offer, Plugin, PluginError, Yacht

DEFAULT_CLOUDS = ["gcp", "lambda", "runpod", "vast", "do"]
TIMEOUTS = {
    "quote": 300,
    "launch": 3600,
    "status": 300,
    "down": 1800,
    "clusters": 300,
    "balances": 60,
}


def sky_python() -> str:
    env = os.environ.get("LABTENDER_SKY_PYTHON")
    if env:
        return env
    tool = Path.home() / ".local/share/uv/tools/skypilot/bin/python"
    if tool.exists():
        return str(tool)
    sky = shutil.which("sky")
    if sky:
        first = Path(sky).read_text(errors="replace").splitlines()[0]
        if first.startswith("#!") and "python" in first:
            return first[2:].strip().split()[0]
    raise PluginError(
        "SkyPilot is not installed: uv tool install 'skypilot[gcp,lambda,runpod,vast]',"
        " or set LABTENDER_SKY_PYTHON to its python"
    )


def parse_accelerators(spec: str) -> tuple[str, int]:
    gpu, _, n = (spec or "").partition(":")
    if not gpu:
        raise PluginError("a SkyPilot profile needs accelerators, for example L4:1")
    return gpu, int(n or 1)


def registry_token(spec: dict) -> str | None:
    """Run the profile's ``registry_token_cmd`` (no shell) and return what it prints: a
    short-lived registry password for machines that cannot sign in to the registry themselves,
    such as RunPod pods pulling from Google Artifact Registry. The token is never logged."""
    cmd = spec.get("registry_token_cmd")
    if not cmd:
        return None
    argv = shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PluginError(f"registry_token_cmd failed: {e}") from None
    token = r.stdout.strip()
    if r.returncode or not token:
        raise PluginError(
            f"registry_token_cmd failed (exit {r.returncode}): {r.stderr.strip()[-300:]}"
        )
    return token


class SkyPilotPlugin(Plugin):
    name = "skypilot"

    def __init__(self, runner=None) -> None:
        self._runner = runner or self._run

    def _run(self, request: dict) -> object:
        helper = resources.files("labtender.yachts").joinpath("sky_helper.py")
        try:
            r = subprocess.run(
                [sky_python(), "-I", str(helper)],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=TIMEOUTS[request["op"]],
            )
        except subprocess.TimeoutExpired:
            raise PluginError(f"SkyPilot {request['op']} timed out") from None
        try:
            reply = json.loads(r.stdout or "{}")
        except ValueError:
            reply = {}
        if "error" in reply:
            raise PluginError(f"SkyPilot {request['op']}: {reply['error']}")
        if "result" not in reply:
            raise PluginError(
                f"SkyPilot {request['op']} gave no answer (exit {r.returncode}):"
                f" {r.stderr.strip()[-500:]}"
            )
        return reply["result"]

    def call(self, op: str, **kw) -> object:
        return self._runner({"op": op, **kw})

    def quote(self, spec: dict) -> list[Offer]:
        gpu, count = parse_accelerators(spec.get("accelerators", ""))
        clouds = spec.get("clouds") or DEFAULT_CLOUDS
        infra = (spec.get("infra") or "").strip()
        if infra:
            clouds = [infra.split("/")[0]]
        # The catalog lists the cheapest machine for the GPU in each region; a profile that
        # needs a bigger one names it, and the helper prices that machine in the same regions.
        rows = self.call(
            "quote",
            gpu=gpu,
            count=count,
            clouds=clouds,
            instance_type=spec.get("instance_type") or None,
        )
        if "/" in infra:
            region = infra.split("/", 1)[1]
            rows = [r for r in rows if r["region"] == region]
        # The catalog prices the machine only; disks and addresses go in extra_usd_h, so the
        # worst case covers them too.
        extra = float(spec.get("extra_usd_h", 0))
        offers = [
            Offer(
                provider=r["cloud"],
                region=r["region"],
                gpu=f"{gpu}:{count}",
                rate_usd_h=round(r["price"] + extra, 4),
                detail={
                    "instance_type": r["instance_type"],
                    "instance_usd_h": r["price"],
                    "enabled": r["enabled"],
                    "vcpus": r.get("vcpus"),
                    "memory_gb": r.get("memory_gb"),
                },
            )
            for r in rows
        ]
        return sorted(offers, key=lambda o: (not o.detail["enabled"], o.rate_usd_h))

    def create(self, ref: str, spec: dict, offer: Offer, max_hours: float) -> Yacht:
        if not offer.detail.get("enabled"):
            raise PluginError(f"{offer.provider} has no credentials on this machine")
        token = registry_token(spec)
        try:
            rec = self.call(
                "launch",
                ref=ref,
                cloud=offer.provider,
                region=offer.region,
                instance_type=offer.detail["instance_type"],
                accelerators=offer.gpu,
                max_hourly_cost=offer.detail.get("instance_usd_h", offer.rate_usd_h),
                max_minutes=max(1, int(max_hours * 60)),
                disk_gb=spec.get("disk_gb"),
                image=spec.get("image"),
                registry_password=token,
                registry_user=spec.get("registry_user"),
            )
        except PluginError as e:
            if token and token in str(e):
                raise PluginError(str(e).replace(token, "[registry token]")) from None
            raise
        return Yacht(ref, ref, "ssh", offer.provider, offer.region, {"record": rec})

    def destroy(self, ref: str) -> None:
        self.call("down", ref=ref)

    def status(self, ref: str) -> str:
        return self.call("status", ref=ref)

    def clusters(self) -> list[str]:
        return self.call("clusters")

    def balances(self) -> dict[str, dict]:
        return self.call("balances")
