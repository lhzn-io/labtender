"""MCP server for agent sessions: read the fleet, claim GPUs, heartbeat, bind runs, release, and
charter cloud GPUs within the limits people set (agents can never approve beyond them).

Run as a stdio server by the agent's MCP client, one process per agent session:

    claude mcp add labtender -e LABTENDER_TOKEN=<token> -- labtender mcp

It is a thin client of the labtender service's HTTP API (``LABTENDER_URL``), acting as the
identity of ``LABTENDER_TOKEN``, so agents follow exactly the rules people do. The
instructions sent to the agent live in ``prompts/mcp_instructions.txt``.
"""

from __future__ import annotations

import time
from importlib import resources

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .client import Hub, HubError


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None


def _claim(c: dict) -> dict:
    keep = (
        "id",
        "resource",
        "state",
        "owner",
        "owner_kind",
        "tier",
        "note",
        "share",
        "est_hours",
        "lease_s",
        "run_key",
        "pending_run",
    )
    out = {k: c.get(k) for k in keep}
    for k in ("held_at", "heartbeat_at", "offered_at", "est_end", "est_start", "ended_at"):
        if c.get(k):
            out[k] = _iso(c[k])
    if "late" in c:
        out["late"] = c["late"]
    if c.get("est_source"):
        out["est_source"] = c["est_source"]
    return out


def _resource(v: dict) -> dict:
    return {
        "resource": v["resource"],
        "free": not v["holders"],
        "free_at": _iso(v["free_at"]) if v["holders"] else "now",
        "free_source": v.get("free_source"),
        "holders": [_claim(c) for c in v["holders"]],
        "queue": [_claim(c) for c in v["queue"]],
        "flags": [
            {"kind": f["kind"], "detail": f["detail"], "since": _iso(f["since"])}
            for f in v["flags"]
        ],
    }


def fleet_summary(state: dict) -> dict:
    """The board, condensed for an agent: hosts, GPUs, claims, queues, flags, live runs."""
    hosts = []
    for h in state["hosts"]:
        info = h.get("host") or {}
        cards = []
        for g in h.get("gpus", []):
            led = g.get("ledger") or {
                "resource": f"{h['name']}/gpu{g['index']}",
                "holders": [],
                "queue": [],
                "flags": [],
                "free_at": None,
            }
            cards.append(
                {
                    "name": g.get("name"),
                    "util_pct": g.get("util_pct"),
                    "mem_used_gib": round(g["mem_used_mib"] / 1024, 1)
                    if g.get("mem_used_mib") is not None
                    else None,
                    "mem_total_gib": round(g["mem_total_mib"] / 1024, 1)
                    if g.get("mem_total_mib")
                    else None,
                    "unified_memory": g.get("unified"),
                    "drives_display": g.get("display_active"),
                    "processes": [f"{p['comm']} {p['pid']}" for p in g.get("procs", [])],
                    "windows_processes": [
                        f"{p['name']} ({p['kind']}) {p['util_pct']:.0f} %, {p['mem_mib']} MiB"
                        for p in (g.get("windows_procs") or [])[:6]
                    ],
                    **_resource(led),
                }
            )
        runs = [
            {
                "name": r["name"],
                "gpu": r.get("card"),
                "pct": round(r["pct"], 1) if r.get("pct") is not None else None,
                "finish_at": _iso(r.get("finish_at")),
                "dir": r["dir"],
                "link": r.get("link"),
            }
            for r in h.get("runs", [])
            if r.get("state") == "running"
        ]
        hosts.append(
            {
                "host": h["name"],
                "online": h["online"],
                "platform": info.get("platform"),
                "accepts_tiers": h.get("accepts") or "all",
                "services": h.get("services_active") or h.get("services") or {},
                "service_health": {
                    name: "ok" if c["ok"] else f"down: {c['detail']}"
                    for name, c in (h.get("service_health") or {}).items()
                },
                "gpus": cards,
                "running_runs": runs,
            }
        )
    return {"time": _iso(state["time"]), "hosts": hosts}


def build_server(hub: Hub | None = None) -> MCPServer:
    hub = hub or Hub()
    server = MCPServer(
        name="labtender",
        version=__version__,
        instructions=resources.files("labtender")
        .joinpath("prompts/mcp_instructions.txt")
        .read_text("utf-8"),
    )

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HubError as e:
            # ToolError reaches the model with its message; other exceptions arrive as a crash.
            raise ToolError(str(e)) from None

    @server.tool()
    def fleet_status() -> dict:
        """Every host and GPU: load, memory, processes, holder, queue, flags, running runs."""
        return fleet_summary(call(hub.get, "/api/state"))

    @server.tool()
    def resource(resource: str) -> dict:
        """One GPU ("host/gpuN") or host: holders with estimated return, queue, flags."""
        return _resource(call(hub.resource, resource))

    @server.tool()
    def claim(
        resource: str,
        est_hours: float,
        note: str,
        tier: str = "normal",
        lease_minutes: float = 10,
        share: str | None = None,
        pickup_minutes: float | None = None,
    ) -> dict:
        """Claim a GPU ("host/gpuN") or host, or join its queue.

        Returns the claim: state "held" means go, "queued" means wait for your turn. tier is
        critical, normal or nice-to-have. share is only for an agreement the current holder and
        the user have given: write who agreed and why. pickup_minutes is how long you have to
        take up the GPU when it is offered (default 15); raise it for unattended chains.
        """
        c = call(
            hub.claim,
            resource,
            tier=tier,
            est_hours=est_hours,
            note=note,
            share=share,
            lease_s=lease_minutes * 60,
            pickup_min=pickup_minutes,
        )
        out = _claim(c)
        if c["state"] != "held":
            view = call(hub.resource, resource)
            pos = (
                [q["id"] for q in view["queue"]].index(c["id"]) + 1
                if c["id"] in [q["id"] for q in view["queue"]]
                else None
            )
            out["queue_position"] = pos
            out["expected_start"] = next(
                (_iso(q.get("est_start")) for q in view["queue"] if q["id"] == c["id"]), None
            )
        return out

    @server.tool()
    def heartbeat(
        claim_id: int, est_hours: float | None = None, lease_minutes: float | None = None
    ) -> dict:
        """Keep a held claim alive, take up an offered GPU, revise the estimate or the lease."""
        return _claim(call(hub.heartbeat, claim_id, est_hours=est_hours, lease_min=lease_minutes))

    @server.tool()
    def bind_run(claim_id: int, run_dir: str) -> dict:
        """Tie a held claim to your run's watched log folder; the run then keeps it alive. A
        folder the probe has not seen yet is fine: the bind waits for it (up to 30 minutes),
        and the claim stays alive meanwhile."""
        return _claim(call(hub.bind, claim_id, run_dir))

    @server.tool()
    def release(claim_id: int, reason: str = "") -> dict:
        """End your claim (held, offered or queued) as soon as you are done or stop waiting."""
        return _claim(call(hub.release, claim_id, reason=reason))

    @server.tool()
    def my_claims() -> dict:
        """Your active claims, with queue positions and expected start or end times."""
        me = call(hub.whoami)
        out = []
        for c in call(hub.claims, owner=me["identity"]):
            view = call(hub.resource, c["resource"])
            match = [x for x in view["holders"] + view["queue"] if x["id"] == c["id"]]
            out.append(_claim(match[0] if match else c))
        return {"identity": me["identity"], "kind": me["kind"], "claims": out}

    # ------------------------------------------------------------ charters

    @server.tool()
    def charter_profiles() -> list[dict]:
        """Cloud GPU (yacht) profiles you can charter: GPU, plugin, time and idle limits."""
        return call(hub.charter_profiles)

    @server.tool()
    def charter_quote(
        profile: str, max_hours: float | None = None, idle_minutes: float | None = None
    ) -> dict:
        """Prices for a profile, its worst-case cost, whether it would be pre-approved (and if
        not, why), and what is left of the project's daily, weekly and monthly limits. Free."""
        return call(hub.charter_quote, profile, max_hours=max_hours, idle_min=idle_minutes)

    @server.tool()
    def charter_request(
        profile: str,
        note: str,
        max_hours: float | None = None,
        idle_minutes: float | None = None,
    ) -> dict:
        """Charter a yacht. This spends money. Within the pre-approval limits it is approved at
        once and launches within minutes; otherwise it waits for a person with the purser role.
        note says what the work is and why the lab's own GPUs will not do."""
        c = call(
            hub.charter_request,
            profile,
            note=note,
            max_hours=max_hours,
            idle_min=idle_minutes,
        )
        return _charter(c) | {"why_not_pre_approved": c.get("why_not", [])}

    @server.tool()
    def charter_status() -> dict:
        """Your charters (state, host, claims, cost so far, time limit) and the budgets."""
        me = call(hub.whoami)
        mine = [_charter(c) for c in call(hub.charters) if c["requester"] == me["identity"]]
        return {"charters": mine, "budgets": call(hub.budgets)}

    @server.tool()
    def charter_end(charter_id: int, reason: str = "") -> dict:
        """Withdraw your charter, or have its yacht landed and destroyed now. Do this as soon
        as you are done: idle time is billed."""
        return _charter(call(hub.charter_end, charter_id, reason=reason))

    return server


def _charter(c: dict) -> dict:
    keep = (
        "id",
        "profile",
        "state",
        "approval",
        "project",
        "requester",
        "note",
        "rate_usd_h",
        "max_hours",
        "idle_min",
        "worst_usd",
        "cost_so_far",
        "cost_usd",
        "host",
        "claims",
        "end_reason",
        "last_error",
    )
    out = {k: c.get(k) for k in keep if c.get(k) not in (None, [], "")}
    out["offer"] = c.get("offer")
    for k in ("ends_by", "approved_at", "live_at", "ended_at"):
        if c.get(k):
            out[k] = _iso(c[k])
    return out


def main() -> None:
    build_server().run("stdio")
