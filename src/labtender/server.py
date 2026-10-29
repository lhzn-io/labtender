"""The labtender service: HTTP API and board page, with the collector running alongside.

Reads are open to anyone who can reach the service (bind it to localhost, the LAN or the VPN
only).
Changes need a bearer token issued with ``labtender token create``; the token's identity is the
claim owner, and its roles decide what it may change: ``operator`` for claims and charter
requests, ``admin`` for the host registry, ``purser`` (people only) for approving charters and
setting budgets. Every handler is async, so all database access happens on the event-loop
thread, alongside the collector and the charter reaper.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import gpukinds, networks
from .charter import CharterError, Charters, is_purser, public
from .collector import Collector
from .db import Store, default_home
from .ledger import DEFAULT_LEASE_S, Ledger, LedgerError
from .metrics import exposition
from .prices import Prices
from .proxy import SAFE_METHODS, WRITE_HEADER, WRITE_VALUE, Proxy
from .proxy import load as load_proxy

RUN_WINDOW_S = 72 * 3600
FONT_FILE = re.compile(r"^[a-z0-9-]+\.woff2$")
LINK_FIELD = re.compile(r"\{(service:[A-Za-z0-9_-]+|name|dir|host)\}")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def merge_twins(runs: list[dict]) -> list[dict]:
    """Fold runs held by the same live process into one, for a host with overlapping watches.

    A job followed both through its wrapper's folder and its own log folder is one run, not two.
    The run whose log moved last stays; the others' folders are listed in its ``also``.
    """

    def pid(r: dict):
        return r.get("pid") if r.get("state") == "running" else None

    def moved(r: dict) -> float:
        return r.get("last_log_at") or 0

    keep: dict[int, dict] = {}
    for r in runs:
        if pid(r) is not None and (pid(r) not in keep or moved(r) > moved(keep[pid(r)])):
            keep[pid(r)] = r
    out = []
    for r in runs:
        if pid(r) is None:
            out.append(r)
        elif keep[pid(r)] is r:
            also = [x["dir"] for x in runs if x is not r and pid(x) == pid(r)]
            out.append(dict(r, also=also) if also else r)
    return out


def run_link(template: str | None, host: dict, run: dict) -> str | None:
    """Fill a host's run-link template; None when a field it names is missing."""
    if not template:
        return None
    values = {
        "name": urllib.parse.quote(run.get("name") or "", safe="~-_."),
        "dir": urllib.parse.quote(run.get("dir") or "", safe="/~-_."),
        "host": urllib.parse.quote(host["name"], safe="~-_."),
    }
    missing = False

    def fill(m: re.Match) -> str:
        nonlocal missing
        key = m.group(1)
        if key.startswith("service:"):
            url = (host.get("services") or {}).get(key.split(":", 1)[1])
            if not url:
                missing = True
                return ""
            return url.rstrip("/")
        return values[key]

    link = LINK_FIELD.sub(fill, template)
    return None if missing else link


class HostIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
    transport: str = "ssh"
    ssh: str | None = None
    lifecycle: str = "static"
    expires_in_h: float | None = None
    interval_s: float = 45
    accepts: list[str] = []
    labels: dict = {}
    services: dict[str, str] = {}
    health: dict[str, str] = {}
    fallbacks: dict[str, list[str]] = {}
    run_link: str | None = None


class HostPatch(BaseModel):
    interval_s: float | None = None
    accepts: list[str] | None = None
    ssh: str | None = None
    services: dict[str, str] | None = None
    health: dict[str, str] | None = None
    fallbacks: dict[str, list[str]] | None = None
    run_link: str | None = None


class ClaimIn(BaseModel):
    resource: str
    tier: str = "normal"
    est_hours: float | None = None
    note: str = Field(default="", max_length=500)
    share: str | None = Field(default=None, max_length=500)
    lease_s: float = DEFAULT_LEASE_S
    pickup_min: float | None = None


class HeartbeatIn(BaseModel):
    est_hours: float | None = None
    lease_min: float | None = None


class ReleaseIn(BaseModel):
    reason: str = Field(default="", max_length=500)
    force: bool = False


class BindIn(BaseModel):
    run_dir: str


class CharterIn(BaseModel):
    profile: str
    project: str | None = None
    max_hours: float | None = None
    idle_min: float | None = None
    note: str = Field(default="", max_length=500)
    approve: bool = False


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class CardIn(BaseModel):
    name: str | None = None
    hold: str | None = None
    bandwidth_gbs: float | None = None


class BudgetIn(BaseModel):
    monthly_usd: float | None = None
    per_charter_usd: float | None = None
    daily_usd: float | None = None
    weekly_usd: float | None = None
    pre_max_hours: float | None = None
    pre_idle_min: float | None = None


HOST_FIELDS = (
    "name",
    "transport",
    "lifecycle",
    "expires_at",
    "interval_s",
    "accepts",
    "labels",
    "services",
    "health",
    "fallbacks",
    "files",
    "run_link",
    "last_seen",
    "last_error",
)


def gauge_view(store: Store, ledger: Ledger, now: float, minutes: float = 60) -> list[dict]:
    """Every GPU in the lab: its latest readings, who holds it, and its history.

    GPUs are grouped by kind (discrete, integrated, Apple) and ranked by memory bandwidth within
    a group. GPUs a host reported today but not in its latest probe are listed as away (unless
    the card has turned up elsewhere, where it is listed instead).
    """
    history = store.history_all(now - 60 * max(1.0, min(minutes, 24 * 60)))
    out = []
    for h in store.hosts():
        name = h["name"]
        online = bool(h["last_seen"]) and now - h["last_seen"] < 3 * max(30, h["interval_s"])
        snap = store.snapshot(name) or {}
        procs = snap.get("gpu_procs", [])
        for g in snap.get("gpus", []):
            card = store.card_at(name, g["index"], g.get("uuid"))
            view = ledger.resource_view(f"{name}/gpu{g['index']}")
            out.append(
                {
                    "host": name,
                    "index": g["index"],
                    "uuid": g.get("uuid") or None,
                    **card_fields(card, g.get("name"), g.get("unified")),
                    "model": g.get("name"),
                    "online": online,
                    "probed_at": h["last_seen"],
                    "away": False,
                    **{
                        k: g.get(k)
                        for k in (
                            "util_pct",
                            "mem_used_mib",
                            "mem_total_mib",
                            "power_w",
                            "power_limit_w",
                            "temp_c",
                            "clock_mhz",
                            "unified",
                        )
                    },
                    "procs": sum(1 for p in procs if p.get("card") == g["index"]),
                    "holders": [c["owner"] for c in view["holders"]],
                    "queued": len(view["queue"]),
                    "history": history.get((name, g["index"]), []),
                }
            )
        for idx, seen in sorted(ledger.missing_gpus(name).items()):
            card = store.card(ledger._uuid_at(name, idx))
            out.append(
                {
                    "host": name,
                    "index": idx,
                    "uuid": card["uuid"] if card else None,
                    **card_fields(card, card["model"] if card else None, None),
                    "model": card["model"] if card else None,
                    "online": online,
                    "away": True,
                    "last_seen": seen,
                    "holders": [
                        c["owner"] for c in ledger.resource_view(f"{name}/gpu{idx}")["holders"]
                    ],
                    "history": [],
                }
            )
    return sorted(out, key=gpukinds.rank_key)


def card_fields(card: dict | None, model: str | None, unified: bool | None) -> dict:
    """A GPU's card name, kind, bandwidth and hold, for the board and the gauges."""
    bw = card.get("bandwidth_gbs") if card else None
    return {
        "card_name": card["name"] if card else None,
        "kind": gpukinds.kind(model, unified),
        "bandwidth_gbs": bw or gpukinds.bandwidth_gbs(model),
        "hold": card.get("hold") if card else None,
    }


def file_checks(h: dict, info: dict | None, now: float) -> list[dict]:
    """Each file the host is asked about: its age at the last probe and whether it is late."""
    ages = (info or {}).get("file_ages") or {}
    probed = h.get("last_seen") or now
    out = []
    for name, f in sorted(h.get("files", {}).items()):
        age = ages.get(f["path"])
        age = None if age is None else age + max(0.0, now - probed)
        out.append(
            {
                "name": name,
                "path": f["path"],
                "max_h": f["max_h"],
                "age_s": age,
                "late": age is None or age > f["max_h"] * 3600,
                "seen": f["path"] in ages,
            }
        )
    return out


def host_view(
    store: Store, ledger: Ledger, h: dict, now: float, roaming: dict | None = None
) -> dict:
    snap = store.snapshot(h["name"])
    online = bool(h["last_seen"]) and now - h["last_seen"] < 3 * max(30, h["interval_s"])
    gpus, host_info, probe_ms, unplaced, errors = [], None, None, [], []
    if snap:
        host_info, probe_ms, errors = snap.get("host"), snap.get("probe_ms"), snap.get("errors", [])
        procs = snap.get("gpu_procs", [])
        for g in snap.get("gpus", []):
            res = f"{h['name']}/gpu{g['index']}"
            card = store.card_at(h["name"], g["index"], g.get("uuid"))
            g = dict(
                g,
                **card_fields(card, g.get("name"), g.get("unified")),
                procs=[p for p in procs if p.get("card") == g["index"]],
                ledger=ledger.resource_view(res),
            )
            gpus.append(g)
        unplaced = [p for p in procs if p.get("card") is None]
    host_claims = ledger.claims(h["name"])
    active = store.active_services(h)
    runs = merge_twins(
        [
            dict(r, link=run_link(h.get("run_link"), dict(h, services=active), r))
            for r in store.runs(h["name"], since=now - RUN_WINDOW_S)
        ]
    )
    return {
        **{k: h.get(k) for k in HOST_FIELDS},
        "online": online,
        "host": host_info,
        "roaming": networks.roaming_via((host_info or {}).get("reached_at"), roaming or {}),
        "file_checks": file_checks(h, host_info, now),
        "gpus": gpus,
        "host_ledger": ledger.resource_view(h["name"]) if host_claims else None,
        "unplaced_procs": unplaced,
        "probe_ms": probe_ms,
        "probe_errors": errors,
        "runs": runs,
        "service_health": store.service_health(h["name"]),
        "services_active": active,
        "host_flags": ledger.flags(h["name"]),
    }


def create_app(
    store: Store | None = None,
    home: Path | None = None,
    collect: bool = True,
    ledger: Ledger | None = None,
    charters: Charters | None = None,
    prices: Prices | None = None,
    proxy: Proxy | None | bool = True,
) -> FastAPI:
    home = home or default_home()
    store = store or Store(home / "labtender.sqlite")
    ledger = ledger or Ledger(store)
    charters = charters or Charters(store, ledger, home)
    prices = prices or Prices(store, charters, home)
    # True reads $LABTENDER_HOME/proxy.toml (none there: no proxy); False turns it off.
    proxy = load_proxy(home, store) if proxy is True else (proxy or None)
    roaming = networks.load(home)
    collector = Collector(store, home, ledger)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = (
            [
                asyncio.create_task(collector.run()),
                asyncio.create_task(charters.run()),
                asyncio.create_task(prices.run()),
            ]
            if collect
            else []
        )
        yield
        if tasks:
            await collector.stop()
            await charters.stop()
            await prices.stop()
            for t in tasks:
                t.cancel()

    app = FastAPI(title="labtender", lifespan=lifespan)
    app.state.store = store
    app.state.ledger = ledger
    app.state.collector = collector
    app.state.charters = charters
    app.state.prices = prices

    async def actor(request: Request, authorization: str | None = Header(default=None)) -> dict:
        # A bearer token always decides when one is sent; the proxy's header is never a fallback.
        if authorization:
            if not authorization.lower().startswith("bearer "):
                raise HTTPException(401, "a bearer token is needed")
            ident = store.identity(token_hash(authorization.split(None, 1)[1].strip()))
            if ident is None:
                raise HTTPException(401, "unknown or revoked token")
            return ident
        user = request.headers.get(proxy.header) if proxy else None
        if user and proxy.trusts(request.client.host if request.client else None):
            who = proxy.identity(user)
            if who is None:
                raise HTTPException(403, f"{user} is not on this labtender's allow-list")
            if who["identity"] in store.agent_identities():
                raise HTTPException(403, f"{who['identity']} is an agent identity")
            if (
                request.method not in SAFE_METHODS
                and request.headers.get(WRITE_HEADER) != WRITE_VALUE
            ):
                raise HTTPException(403, f"a change needs the {WRITE_HEADER}: {WRITE_VALUE} header")
            return dict(who, via="proxy")
        raise HTTPException(401, "a bearer token is needed")

    def needs(role: str):
        async def check(who: dict = Depends(actor)) -> dict:
            if role not in who["roles"]:
                raise HTTPException(403, f"this needs the {role} role")
            return who

        return check

    operator = needs("operator")
    admin = needs("admin")

    async def purser(who: dict = Depends(actor)) -> dict:
        if not is_purser(who["kind"], who["roles"]):
            raise HTTPException(403, "this needs a person with the purser role")
        return who

    def ledger_call(fn, *args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except LedgerError as e:
            raise HTTPException(409, str(e)) from None

    def page(name: str) -> str:
        return resources.files("labtender").joinpath(f"board/{name}").read_text("utf-8")

    # The board: the lab's GPUs (gauges, claims, runs) and hosts, in two tabs, and the yachts.
    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def home() -> str:
        return page("home.html")

    @app.get("/board")
    async def board() -> RedirectResponse:
        return RedirectResponse("/#hosts")  # the classic board's address, kept for bookmarks

    @app.get("/auth.js")
    async def auth_js() -> Response:
        return Response(page("auth.js"), media_type="text/javascript")

    @app.get("/common.js")
    async def common_js() -> Response:
        return Response(page("common.js"), media_type="text/javascript")

    @app.get("/common.css")
    async def common_css() -> Response:
        return Response(page("common.css"), media_type="text/css")

    @app.get("/theme.js")
    async def theme_js() -> Response:
        return Response(page("theme.js"), media_type="text/javascript")

    @app.get("/gauge.js")
    async def gauge_js() -> Response:
        return Response(page("gauge.js"), media_type="text/javascript")

    @app.get("/fonts/{name}")
    async def font(name: str) -> Response:
        if not FONT_FILE.match(name):
            raise HTTPException(404, "no such font")
        try:
            data = resources.files("labtender").joinpath(f"board/fonts/{name}").read_bytes()
        except FileNotFoundError:
            raise HTTPException(404, "no such font") from None
        return Response(
            data, media_type="font/woff2", headers={"Cache-Control": "public, max-age=604800"}
        )

    @app.api_route("/compact", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def compact() -> str:
        return resources.files("labtender").joinpath("board/compact.html").read_text("utf-8")

    @app.api_route("/gauges", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def gauges_page() -> str:
        return page("home.html")  # the gauges page grew into the home page

    @app.api_route("/prices", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def prices_page() -> str:
        return resources.files("labtender").joinpath("board/prices.html").read_text("utf-8")

    @app.api_route("/purser", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def purser_page() -> str:
        return resources.files("labtender").joinpath("board/purser.html").read_text("utf-8")

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            exposition(store, ledger, charters=charters),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "time": time.time()}

    @app.get("/api/state")
    async def state() -> dict:
        now = time.time()
        return {
            "time": now,
            "hosts": [host_view(store, ledger, h, now, roaming) for h in store.hosts()],
            **charters.view(now),
            "run_rate": prices.run_rate(now),
        }

    @app.get("/api/hosts")
    async def hosts(service: str | None = None) -> list[dict]:
        rows = store.hosts()
        if service:
            rows = [h for h in rows if (h.get("services") or {}).get(service)]
        return [
            dict(
                h,
                service_health=store.service_health(h["name"]),
                services_active=store.active_services(h),
            )
            for h in rows
        ]

    @app.post("/api/hosts", status_code=201)
    async def add_host(body: HostIn, who: dict = Depends(admin)) -> dict:
        if store.host(body.name):
            raise HTTPException(409, f"host {body.name} exists")
        expires = time.time() + body.expires_in_h * 3600 if body.expires_in_h else None
        try:
            return store.add_host(
                body.name,
                actor=who["identity"],
                transport=body.transport,
                ssh=body.ssh,
                lifecycle=body.lifecycle,
                expires_at=expires,
                interval_s=body.interval_s,
                accepts=body.accepts,
                labels=body.labels,
                services=body.services,
                run_link=body.run_link,
                health=body.health,
                fallbacks=body.fallbacks,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.patch("/api/hosts/{name}")
    async def update_host(name: str, body: HostPatch, who: dict = Depends(admin)) -> dict:
        if not store.host(name):
            raise HTTPException(404, f"no host {name}")
        try:
            return store.update_host(
                name,
                actor=who["identity"],
                interval_s=body.interval_s,
                accepts=body.accepts,
                ssh=body.ssh,
                services=body.services,
                run_link=body.run_link,
                health=body.health,
                fallbacks=body.fallbacks,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.delete("/api/hosts/{name}")
    async def remove_host(name: str, who: dict = Depends(admin)) -> dict:
        if not store.remove_host(name, actor=who["identity"]):
            raise HTTPException(404, f"no host {name}")
        return {"removed": name}

    @app.get("/api/runs")
    async def runs(host: str | None = None, dir: str | None = None) -> list[dict]:
        rows = store.runs(host, since=time.time() - RUN_WINDOW_S, dir=dir)
        by_host = {h["name"]: h for h in store.hosts()}
        out = []
        for r in rows:
            h = by_host.get(r["host"])
            live = dict(h, services=store.active_services(h)) if h else None
            out.append(dict(r, link=run_link(h.get("run_link"), live, r)) if h else r)
        return out

    @app.get("/api/history/{host}/{idx}")
    async def history(host: str, idx: int, minutes: float = 60) -> list[dict]:
        return store.history(host, idx, time.time() - 60 * min(minutes, 24 * 60))

    @app.get("/api/gauges")
    async def gauges(minutes: float = 60) -> dict:
        now = time.time()
        return {
            "time": now,
            "groups": [{"kind": k, "label": label} for k, label in gpukinds.GROUPS],
            "gpus": gauge_view(store, ledger, now, minutes),
        }

    @app.get("/api/events")
    async def events(limit: int = 100) -> list[dict]:
        return store.events(min(limit, 1000))

    @app.get("/api/cards")
    async def cards() -> list[dict]:
        return store.cards()

    @app.put("/api/cards/{ref:path}")
    async def set_card(ref: str, body: CardIn, who: dict = Depends(admin)) -> dict:
        card = store.find_card(ref)
        if card is None:
            raise HTTPException(404, f"no card {ref}")
        given = body.model_fields_set or {"name"}  # an empty body gives back its own name
        try:
            if "name" in given:
                card = store.name_card(card["uuid"], body.name, actor=who["identity"])
            changes = {k: getattr(body, k) for k in ("hold", "bandwidth_gbs") if k in given}
            if changes:
                card = store.set_card(card["uuid"], actor=who["identity"], **changes)
            return card
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    # ------------------------------------------------------------ ledger

    @app.get("/api/whoami")
    async def whoami(who: dict = Depends(actor)) -> dict:
        return who

    @app.get("/api/resources/{resource:path}")
    async def resource(resource: str) -> dict:
        return ledger_call(ledger.resource_view, resource)

    @app.get("/api/claims")
    async def claims(
        resource: str | None = None, owner: str | None = None, all: bool = False
    ) -> list[dict]:
        return ledger.claims(resource, active=not all, owner=owner)

    @app.post("/api/claims", status_code=201)
    async def claim(body: ClaimIn, who: dict = Depends(operator)) -> dict:
        return ledger_call(
            ledger.request,
            body.resource,
            who["identity"],
            who["kind"],
            tier=body.tier,
            est_hours=body.est_hours,
            note=body.note,
            share=body.share,
            lease_s=body.lease_s,
            pickup_s=body.pickup_min * 60 if body.pickup_min is not None else None,
        )

    @app.post("/api/claims/{claim_id}/heartbeat")
    async def heartbeat(claim_id: int, body: HeartbeatIn, who: dict = Depends(operator)) -> dict:
        return ledger_call(
            ledger.heartbeat,
            claim_id,
            who["identity"],
            est_hours=body.est_hours,
            lease_s=body.lease_min * 60 if body.lease_min is not None else None,
        )

    @app.post("/api/claims/{claim_id}/release")
    async def release(claim_id: int, body: ReleaseIn, who: dict = Depends(operator)) -> dict:
        if body.force and who["kind"] != "person":
            raise HTTPException(403, "only a person can release someone else's claim")
        return ledger_call(
            ledger.release, claim_id, who["identity"], reason=body.reason, force=body.force
        )

    @app.post("/api/claims/{claim_id}/bind")
    async def bind(claim_id: int, body: BindIn, who: dict = Depends(operator)) -> dict:
        return ledger_call(ledger.bind_run, claim_id, who["identity"], body.run_dir)

    # ------------------------------------------------------------ charters

    def charter_or_404(charter_id: int) -> dict:
        c = charters.charter(charter_id)
        if c is None:
            raise HTTPException(404, f"no charter {charter_id}")
        return c

    @app.get("/api/charters/profiles")
    async def charter_profiles() -> list[dict]:
        return [
            {k: p[k] for k in ("name", "description", "plugin", "accelerators", "project")}
            | {
                "infra": p.get("infra", ""),
                "max_hours": p["max_hours"],
                "idle_min": p["idle_min"],
            }
            for p in charters.profiles().values()
        ]

    @app.get("/api/charters/quote")
    async def charter_quote(
        profile: str,
        project: str | None = None,
        max_hours: float | None = None,
        idle_min: float | None = None,
        who: dict = Depends(operator),
    ) -> dict:
        try:
            return await charters.quote(
                profile, project=project, max_hours=max_hours, idle_min=idle_min
            )
        except LedgerError as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/charters")
    async def charter_list(all: bool = False) -> list[dict]:
        rows = charters.charters() if all else charters.charters(since=time.time() - 24 * 3600)
        return [public(c) for c in rows]

    @app.get("/api/charters/{charter_id}")
    async def charter_get(charter_id: int) -> dict:
        return public(charter_or_404(charter_id))

    @app.post("/api/charters", status_code=201)
    async def charter_request(body: CharterIn, who: dict = Depends(operator)) -> dict:
        try:
            q = await charters.quote(
                body.profile, project=body.project, max_hours=body.max_hours, idle_min=body.idle_min
            )
            c = charters.request(
                body.profile,
                q["offers"][0],
                who["identity"],
                who["kind"],
                who["roles"],
                project=body.project,
                max_hours=body.max_hours,
                idle_min=body.idle_min,
                note=body.note,
                approve=body.approve,
            )
        except CharterError as e:
            raise HTTPException(403 if "purser" in str(e) else 409, str(e)) from None
        except LedgerError as e:
            raise HTTPException(409, str(e)) from None
        return public(c) | {"why_not": c.get("why_not", [])}

    @app.post("/api/charters/{charter_id}/approve")
    async def charter_approve(charter_id: int, who: dict = Depends(purser)) -> dict:
        charter_or_404(charter_id)
        return public(
            ledger_call(charters.approve, charter_id, who["identity"], who["kind"], who["roles"])
        )

    @app.post("/api/charters/{charter_id}/reject")
    async def charter_reject(charter_id: int, body: ReasonIn, who: dict = Depends(purser)) -> dict:
        charter_or_404(charter_id)
        return public(
            ledger_call(
                charters.reject,
                charter_id,
                who["identity"],
                who["kind"],
                who["roles"],
                reason=body.reason,
            )
        )

    @app.get("/api/charters/{charter_id}/log")
    async def charter_log(
        charter_id: int, limit: int = 8000, who: dict = Depends(operator)
    ) -> dict:
        # Operators only: set-up output can name private paths and hosts.
        charter_or_404(charter_id)
        return charters.log_tail(charter_id, max(256, min(limit, 200_000)))

    @app.post("/api/charters/{charter_id}/requote")
    async def charter_requote(charter_id: int, who: dict = Depends(operator)) -> dict:
        charter_or_404(charter_id)
        try:
            c = await charters.requote(charter_id, who["identity"], who["kind"], who["roles"])
        except LedgerError as e:
            raise HTTPException(409, str(e)) from None
        return public(c)

    @app.post("/api/charters/{charter_id}/end")
    async def charter_end(charter_id: int, body: ReasonIn, who: dict = Depends(operator)) -> dict:
        charter_or_404(charter_id)
        return public(
            ledger_call(
                charters.end,
                charter_id,
                who["identity"],
                who["kind"],
                who["roles"],
                reason=body.reason,
            )
        )

    @app.get("/api/purser")
    async def purser_view() -> dict:
        now = time.time()
        market = {g["gpu"]: g["providers"] for g in prices.view(now)["gpus"]}
        return charters.purser_view(now, market)

    @app.get("/api/budgets")
    async def budgets() -> list[dict]:
        return [charters.usage(p) for p in charters.projects()]

    @app.put("/api/budgets/{project}")
    async def set_budget(project: str, body: BudgetIn, who: dict = Depends(purser)) -> dict:
        return ledger_call(
            charters.set_limits, project, who["identity"], **body.model_dump(exclude_none=True)
        )

    # ------------------------------------------------------------ prices

    @app.get("/api/prices")
    async def price_view() -> dict:
        return prices.view()

    @app.post("/api/prices/refresh")
    async def price_refresh(who: dict = Depends(operator)) -> dict:
        try:
            return await prices.refresh()
        except LedgerError as e:
            raise HTTPException(429, str(e)) from None

    return app
