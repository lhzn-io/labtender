"""Runs inside SkyPilot's own Python, never inside labtender's.

SkyPilot brings many dependencies (and its own API server), so labtender keeps it in a separate
environment and talks to it through this script: one JSON request on stdin, one JSON reply on
stdout (``{"result": ...}`` or ``{"error": ..., "kind": ...}``). SkyPilot's log output goes to
stderr. Each operation is one SDK call or a short sequence of them.
"""

from __future__ import annotations

import contextlib
import json
import math
import sys
import time
from pathlib import Path


def quote(req: dict) -> list[dict]:
    import sky
    from sky.client import sdk

    enabled = {c.lower() for c in sky.get(sdk.enabled_clouds())}
    found = sky.list_accelerators(
        gpus_only=True,
        name_filter=req["gpu"],
        quantity_filter=req["count"],
        clouds=req["clouds"],
        all_regions=True,
        case_sensitive=False,
    )
    offers = []
    for name, rows in found.items():
        if name.lower() != req["gpu"].lower():
            continue  # the name filter matches substrings: L4 also finds L40 and L40S
        for r in rows:
            if r.accelerator_count != req["count"] or r.price is None or math.isnan(r.price):
                continue
            offers.append(
                {
                    "cloud": str(r.cloud).lower(),
                    "region": r.region,
                    "instance_type": r.instance_type,
                    "price": float(r.price),
                    "enabled": str(r.cloud).lower() in enabled,
                    "vcpus": r.cpu_count,
                    "memory_gb": r.memory,
                }
            )
    want = req.get("instance_type")
    if want:
        # The catalog lists the cheapest machine per region; price the named one in those
        # regions instead, skipping regions or clouds that do not offer it.
        priced = []
        for o in {(o["cloud"], o["region"]): o for o in offers}.values():
            try:
                r = sky.Resources(
                    cloud=sky.CLOUD_REGISTRY.from_str(o["cloud"]),
                    region=o["region"],
                    instance_type=want,
                    accelerators=f"{req['gpu']}:{req['count']}",
                )
                cost = float(r.get_cost(3600))
            except Exception:  # noqa: BLE001  (not offered there)
                continue
            priced.append(dict(o, instance_type=want, price=cost, vcpus=r.cpus, memory_gb=r.memory))
        offers = priced
    return offers


def registry_envs(image: str | None, password: str | None = None, user: str | None = None) -> dict:
    """Task settings that let a cloud machine pull a private container image.

    With a password (a short-lived token from the profile's ``registry_token_cmd``), SkyPilot
    signs in to the image's registry with it; Google Artifact Registry takes the user name
    ``oauth2accesstoken``, other registries need ``registry_user``. Without one, Google Artifact
    Registry (``<region>-docker.pkg.dev``) is read with the machine's own service account, which
    only works on Google Cloud. Other registries, and public images, need nothing here.
    """
    if not image or not image.startswith("docker:"):
        return {}
    host = image[len("docker:") :].split("/", 1)[0]
    gar = host.endswith("-docker.pkg.dev")
    if password:
        user = user or ("oauth2accesstoken" if gar else None)
        if not user:
            raise ValueError(f"a registry password for {host} needs registry_user")
        return {
            "SKYPILOT_DOCKER_USERNAME": user,
            "SKYPILOT_DOCKER_PASSWORD": password,
            "SKYPILOT_DOCKER_SERVER": host,
        }
    if not gar:
        return {}
    return {
        "SKYPILOT_DOCKER_USERNAME": "oauth2accesstoken",
        "SKYPILOT_DOCKER_PASSWORD": "",
        "SKYPILOT_DOCKER_SERVER": host,
    }


def scrub_secret(secret: str, home: Path | None = None, within_s: float = 7200) -> int:
    """Remove a registry token from SkyPilot's text logs, and keep its folders private.

    SkyPilot writes the task's settings, registry password included, into its provision log
    and generated task files. Those written in the last ``within_s`` are rewritten without it.
    Its state database keeps a copy too, which cannot be safely rewritten while it is in use,
    so ``~/.sky`` and ``~/sky_logs`` are made readable by their owner only. Returns the number
    of files scrubbed.
    """
    home = home or Path.home()
    scrubbed = 0
    now = time.time()
    for private in (home / ".sky", home / "sky_logs"):
        if private.is_dir():
            private.chmod(0o700)
    for folder, pattern in ((home / "sky_logs", "**/*.log"), (home / ".sky/generated", "*")):
        for f in folder.glob(pattern) if folder.is_dir() else ():
            try:
                if not f.is_file() or now - f.stat().st_mtime > within_s:
                    continue
                data = f.read_bytes()
                if secret.encode() in data:
                    f.write_bytes(data.replace(secret.encode(), b"[redacted]"))
                    scrubbed += 1
            except OSError:
                continue
    return scrubbed


def launch(req: dict) -> dict:
    import sky
    from sky.skylet.autostop_lib import AutostopWaitFor

    resources = sky.Resources(
        cloud=sky.CLOUD_REGISTRY.from_str(req["cloud"]),
        region=req["region"],
        instance_type=req["instance_type"],
        accelerators=req["accelerators"],
        disk_size=req.get("disk_gb") or None,
        image_id=req.get("image") or None,
        # SkyPilot itself refuses anything dearer than the approved rate.
        max_hourly_cost=req["max_hourly_cost"],
    )
    envs = registry_envs(req.get("image"), req.get("registry_password"), req.get("registry_user"))
    task = sky.Task(name=req["ref"], envs=envs)
    task.set_resources(resources)
    # The hard limit: destroyed after max_minutes on the provider's side whatever happens to
    # labtender. wait_for NONE ignores activity, so it is a deadline, not an idle timer.
    try:
        with contextlib.redirect_stdout(sys.stderr):
            sky.get(
                sky.launch(
                    task,
                    cluster_name=req["ref"],
                    retry_until_up=False,
                    idle_minutes_to_autostop=req["max_minutes"],
                    wait_for=AutostopWaitFor.NONE,
                    down=True,
                )
            )
    finally:
        if req.get("registry_password"):
            scrub_secret(req["registry_password"])
    rec = _record(req["ref"])
    if rec is None:
        raise RuntimeError("launched, but SkyPilot does not list the cluster")
    if not rec["to_down"] or rec["autostop"] is None or rec["autostop"] > req["max_minutes"]:
        raise RuntimeError(f"the provider-side time limit is not set: {rec}")
    return rec


def _record(ref: str, refresh: str = "AUTO") -> dict | None:
    import sky

    rows = sky.get(sky.status([ref], refresh=getattr(sky.StatusRefreshMode, refresh)))
    for r in rows:
        if r.name == ref:
            return {
                "name": r.name,
                "status": str(getattr(r.status, "value", r.status)).lower(),
                "autostop": r.autostop,
                "to_down": r.to_down,
                "cloud": str(r.cloud or "").lower(),
                "region": r.region,
            }
    return None


def status(req: dict) -> str:
    rec = _record(req["ref"])
    return "gone" if rec is None else rec["status"]


def down(req: dict) -> None:
    import sky

    try:
        with contextlib.redirect_stdout(sys.stderr):
            sky.get(sky.down(req["ref"]))
    except sky.exceptions.ClusterDoesNotExist:
        pass


def clusters(req: dict) -> list[str]:
    import sky

    return [r.name for r in sky.get(sky.status(refresh=sky.StatusRefreshMode.AUTO))]


def _runpod_key(home: Path | None = None) -> str | None:
    """RunPod's API key, where SkyPilot finds it: RUNPOD_API_KEY or ~/.runpod/config.toml."""
    import os
    import tomllib

    if os.environ.get("RUNPOD_API_KEY"):
        return os.environ["RUNPOD_API_KEY"]
    path = (home or Path.home()) / ".runpod" / "config.toml"
    try:
        return tomllib.loads(path.read_text())["default"]["api_key"]
    except (OSError, KeyError, ValueError):
        return None


def balances(req: dict) -> dict:
    """Prepaid balances of the providers that report one. A pod is stopped, and its disk lost,
    when the balance runs out, so labtender checks it before a launch and while yachts run.
    Only RunPod so far; a provider that cannot be read is left out."""
    out = {}
    key = _runpod_key()
    if key:
        try:
            import runpod
            from runpod.api.graphql import run_graphql_query

            runpod.api_key = key
            me = run_graphql_query("query { myself { clientBalance currentSpendPerHr } }")
            me = me["data"]["myself"]
            out["runpod"] = {
                "balance_usd": round(float(me["clientBalance"]), 2),
                "spend_usd_h": round(float(me["currentSpendPerHr"] or 0), 4),
            }
        except Exception as e:  # noqa: BLE001  (an unreadable balance is reported as missing)
            print(f"runpod balance: {e}", file=sys.stderr)
    return out


OPS = {
    "quote": quote,
    "launch": launch,
    "status": status,
    "down": down,
    "clusters": clusters,
    "balances": balances,
}


def main() -> None:
    req = json.loads(sys.stdin.read())
    try:
        out = {"result": OPS[req["op"]](req)}
    except Exception as e:  # noqa: BLE001  (every failure goes back to labtender as data)
        out = {"error": str(e)[-2000:], "kind": type(e).__name__}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
