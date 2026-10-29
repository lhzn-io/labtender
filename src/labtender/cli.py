"""Command line: run the labtender service, manage the registry, claim GPUs, and read the board.

Registry, watch and token commands write the labtender service's SQLite file directly, so they
run on the service's machine. Claim, charter and budget commands and preflight go through the
service's HTTP API (``LABTENDER_URL``) as the identity of ``LABTENDER_TOKEN``, the same way
remote people and agents do.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
import time

from . import __version__, gpukinds, parsers
from .client import Hub, HubError
from .collector import Collector
from .db import Store, default_home
from .ledger import Ledger
from .server import token_hash


def _store() -> Store:
    return Store(default_home() / "labtender.sqlite")


def _actor() -> str:
    return os.environ.get("LABTENDER_ACTOR") or getpass.getuser()


def _age(t: float | None) -> str:
    if not t:
        return "never"
    s = time.time() - t
    return (
        f"{s:.0f} s ago"
        if s < 120
        else f"{s / 60:.0f} min ago"
        if s < 7200
        else f"{s / 3600:.1f} h ago"
    )


def _dur(s: float | None) -> str:
    if s is None:
        return "-"
    h, m = divmod(int(s // 60), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def cmd_serve(a) -> None:
    import uvicorn

    from .server import create_app

    # The client address must be the real peer, never one taken from X-Forwarded-For: proxy
    # sign-in trusts its header only from the proxy's own address.
    uvicorn.run(
        create_app(collect=not a.no_collect),
        host=a.bind,
        port=a.port,
        log_level="info",
        proxy_headers=False,
    )


def _services(pairs: list[str] | None) -> dict | None:
    """NAME=URL pairs from --service; an empty URL removes the service."""
    if not pairs:
        return None
    out = {}
    for pair in pairs:
        name, sep, url = pair.partition("=")
        if not sep:
            raise ValueError(f"{pair!r}: use NAME=VALUE (NAME= removes it)")
        out[name.strip()] = url.strip()
    return out


def _fallbacks(pairs: list[str] | None) -> dict | None:
    """NAME=URL pairs from --fallback, in order; NAME= clears that service's fallbacks."""
    if not pairs:
        return None
    out: dict[str, list[str]] = {}
    for name, url in ((n.strip(), u.strip()) for n, u in (_pair(p) for p in pairs)):
        out.setdefault(name, [])
        if url:
            out[name].append(url)
    return out


def _pair(pair: str) -> tuple[str, str]:
    name, sep, value = pair.partition("=")
    if not sep:
        raise ValueError(f"{pair!r}: use NAME=VALUE (NAME= removes it)")
    return name, value


def cmd_host_add(a) -> None:
    expires = time.time() + a.expires_in * 3600 if a.expires_in else None
    h = _store().add_host(
        a.name,
        actor=_actor(),
        transport="local" if a.local else "ssh",
        ssh=a.ssh,
        lifecycle="ephemeral" if expires else "static",
        expires_at=expires,
        interval_s=a.interval,
        accepts=[t for t in (a.accepts or "").split(",") if t],
        services=_services(a.service),
        run_link=a.run_link,
        health=_services(a.health),
        fallbacks=_fallbacks(a.fallback),
    )
    print(f"added {h['name']} ({h['transport']}{', ' + h['ssh'] if h['ssh'] else ''})")


def cmd_host_set(a) -> None:
    accepts = None if a.accepts is None else [t for t in a.accepts.split(",") if t]
    h = _store().update_host(
        a.name,
        actor=_actor(),
        interval_s=a.interval,
        accepts=accepts,
        ssh=a.ssh,
        services=_services(a.service),
        run_link=a.run_link,
        labels=_services(a.label),
        transport=a.transport,
        health=_services(a.health),
        fallbacks=_fallbacks(a.fallback),
        files=_services(a.file),
    )
    tiers = ", ".join(h["accepts"]) or "all tiers"
    print(
        f"{h['name']}: {h['transport']}, every {h['interval_s']:.0f} s, takes {tiers},"
        f" ssh {h['ssh'] or '-'}"
    )
    for name, url in sorted(h["services"].items()):
        health = f" (health {h['health'][name]})" if name in h["health"] else ""
        print(f"  service {name}: {url}{health}")
        for fb in h["fallbacks"].get(name, []):
            print(f"    fallback: {fb}")
    if h["run_link"]:
        print(f"  run link: {h['run_link']}")
    for k, v in sorted(h["labels"].items()):
        print(f"  label {k}={v}")
    for k, f in sorted(h["files"].items()):
        print(f"  file {k}: {f['path']}, late after {f['max_h']:g} h")


def cmd_host_rm(a) -> None:
    if not _store().remove_host(a.name, actor=_actor(), reason=a.reason or ""):
        sys.exit(f"no host {a.name}")
    print(f"removed {a.name}")


def cmd_host_ls(a) -> None:
    store = _store()
    for h in store.hosts():
        exp = f" expires in {_dur(h['expires_at'] - time.time())}" if h["expires_at"] else ""
        err = f"  error: {h['last_error']}" if h["last_error"] else ""
        print(
            f"{h['name']:<12} {h['transport']:<6} {h['ssh'] or '-':<14}"
            f" every {h['interval_s']:.0f} s  seen {_age(h['last_seen'])}{exp}{err}"
        )
        checks = store.service_health(h["name"])
        for name, url in sorted(h["services"].items()):
            c = checks.get(name)
            via = f" via {c['base']}" if c and c["ok"] and c.get("base") not in (None, url) else ""
            state = (
                f"  {'up' if c['ok'] else 'DOWN'}{via}"
                f" ({c['detail']}, checked {_age(c['checked_at'])})"
                if c
                else ""
            )
            print(f"{'':<12} service {name}: {url}{state}")
            for fb in h["fallbacks"].get(name, []):
                print(f"{'':<12}   fallback: {fb}")


def cmd_card_ls(a) -> None:
    for c in _store().cards():
        where = f"{c['host']}/gpu{c['idx']}"
        bw = c["bandwidth_gbs"] or gpukinds.bandwidth_gbs(c["model"])
        print(
            f"{c['name'] or '-':<16} {where:<18} {c['model'] or '':<36}"
            f" {f'{bw:g} GB/s' if bw else '':<10} seen {_age(c['last_seen'])}  {c['uuid']}"
            + (f"\n{'':<16} held back from agents: {c['hold']}" if c["hold"] else "")
        )


def _set_card(ref: str, **changes) -> dict:
    store = _store()
    card = store.find_card(ref)
    if card is None:
        sys.exit(f"no card {ref}; see labtender card ls")
    try:
        return store.set_card(card["uuid"], actor=_actor(), **changes)
    except ValueError as e:
        sys.exit(str(e))


def cmd_card_hold(a) -> None:
    c = _set_card(a.card, hold=a.reason)
    print(f"{c['name']} is held back from agents: {c['hold']}")


def cmd_card_offer(a) -> None:
    c = _set_card(a.card, hold=None)
    print(f"{c['name']} is offered to agents again")


def cmd_card_bandwidth(a) -> None:
    c = _set_card(a.card, bandwidth_gbs=a.gbs)
    bw = c["bandwidth_gbs"] or gpukinds.bandwidth_gbs(c["model"])
    print(f"{c['name']}: {f'{bw:g} GB/s' if bw else 'bandwidth unknown'}")


def cmd_card_name(a) -> None:
    store = _store()
    card = store.find_card(a.card)
    if card is None:
        sys.exit(f"no card {a.card}; see labtender card ls")
    try:
        c = store.name_card(card["uuid"], a.name, actor=_actor())
    except ValueError as e:
        sys.exit(str(e))
    print(f"{c['uuid']} is now {c['name'] or 'unnamed'}")


def cmd_watch_add(a) -> None:
    specs = parsers.load_specs(default_home())
    if a.parser not in specs:
        sys.exit(f"no parser {a.parser}; have {', '.join(sorted(specs))}")
    store = _store()
    if not store.host(a.host):
        sys.exit(f"no host {a.host}")
    print(f"watch {store.add_watch(a.host, a.parser, a.glob, actor=_actor())} added")


def cmd_watch_rm(a) -> None:
    if not _store().remove_watch(a.id, actor=_actor()):
        sys.exit(f"no watch {a.id}")


def cmd_watch_ls(a) -> None:
    for w in _store().watches():
        print(f"{w['id']:>3}  {w['host']:<12} {w['parser']:<14} {w['glob']}")


def cmd_parsers(a) -> None:
    for name, spec in sorted(parsers.load_specs(default_home()).items()):
        print(f"{name:<16} {spec.get('description', '')}")


def cmd_probe(a) -> None:
    """Probe one registered host now and print what the board would show."""
    store = _store()
    col = Collector(store, default_home(), Ledger(store))
    doc = asyncio.run(col.poll_once(a.name))
    if a.json:
        print(json.dumps(doc, indent=2))
        return
    _print_host(store, a.name)


def _print_host(store: Store, name: str) -> None:
    h = store.host(name)
    snap = store.snapshot(name) or {}
    info = snap.get("host") or {}
    print(
        f"{name}: {info.get('platform', '?')}, {info.get('cpu_model') or '?'}, "
        f"CPU {info.get('cpu_busy_pct', '?')} %, seen {_age(h['last_seen'])}"
        + (f", error: {h['last_error']}" if h["last_error"] else "")
    )
    for g in snap.get("gpus", []):
        mem = (
            f"{(g['mem_used_mib'] or 0) / 1024:.1f}/{g['mem_total_mib'] / 1024:.0f} GiB"
            if g.get("mem_total_mib")
            else "mem n/a"
        )
        print(
            f"  GPU {g['index']} {g['name']}: {g.get('util_pct')} %, {mem}"
            f"{' unified' if g.get('unified') else ''}, {g.get('power_w')} W, {g.get('temp_c')} C"
        )
        for p in snap.get("gpu_procs", []):
            if p.get("card") == g["index"]:
                print(f"    pid {p['pid']} {p['comm']} ({_dur(p.get('elapsed_s'))})")
    for p in snap.get("gpu_procs", []):
        if p.get("card") is None:
            print(f"  unplaced pid {p['pid']} {p['comm']}: {p.get('note')}")
    for r in store.runs(name, since=time.time() - 72 * 3600):
        pct = f"{r['pct']:.0f} %" if r.get("pct") is not None else "?"
        eta = f", ETA {_dur(r['eta_s'])}" if r.get("eta_s") is not None else ""
        print(f"  run {r['name']}: {r['state']} {pct}{eta}, log {_age(r.get('last_log_at'))}")


def cmd_status(a) -> None:
    store = _store()
    for h in store.hosts():
        _print_host(store, h["name"])


def cmd_token_create(a) -> None:
    token = "lt_" + secrets.token_urlsafe(24)
    _store().add_token(token_hash(token), a.identity, a.kind, actor=_actor(), roles=a.role)
    print(token)
    print("shown once; keep it in the client's environment as LABTENDER_TOKEN", file=sys.stderr)


def cmd_token_revoke(a) -> None:
    print(f"revoked {_store().revoke_tokens(a.identity, actor=_actor())} token(s)")


def cmd_token_grant(a) -> None:
    n = _store().grant_role(a.identity, a.role, actor=_actor(), revoke=a.remove)
    print(f"{'removed' if a.remove else 'granted'} {a.role} on {n} token(s) of {a.identity}")


def _money(v: float | None) -> str:
    return "-" if v is None else f"${v:,.2f}"


def _charter_line(c: dict) -> str:
    o = c.get("offer") or {}
    where = f"{o.get('provider', '?')} {o.get('region', '')}".strip()
    extra = []
    if c["state"] in ("live", "landing", "adrift", "launching"):
        extra.append(f"{_money(c['cost_so_far'])} so far")
        if c.get("ends_by"):
            extra.append(f"ends by {_clock(c['ends_by'])}")
    elif c.get("cost_usd") is not None:
        extra.append(f"cost {_money(c['cost_usd'])}")
    if c.get("host"):
        extra.append(f"host {c['host']}")
    if c.get("end_reason") and c["state"] not in ("live", "launching"):
        extra.append(c["end_reason"])
    return (
        f"charter {c['id']} {c['profile']}: {c['state']}"
        f"{' (' + c['approval'] + ')' if c.get('approval') else ''}, {where},"
        f" {_money(c['rate_usd_h'])}/h, up to {c['max_hours']:g} h (worst {_money(c['worst_usd'])})"
        f", idle {c['idle_min']:g} min, for {c['requester']}"
        + (f"; {'; '.join(extra)}" if extra else "")
    )


def _usage_lines(u: dict) -> list[str]:
    lim, res, left = u["limits"], u["reserved"], u["left"]
    return [
        f"{u['project']}: month {_money(res['monthly_usd'])} of {_money(lim['monthly_usd'])}"
        f" reserved ({_money(u['month_actual_usd'])} spent, {_money(left['monthly_usd'])} left)",
        f"  pre-approved: up to {_money(lim['per_charter_usd'])} each,"
        f" {lim['pre_max_hours']:g} h, idle {lim['pre_idle_min']:g} min;"
        f" today {_money(res['daily_usd'])} of {_money(lim['daily_usd'])},"
        f" this week {_money(res['weekly_usd'])} of {_money(lim['weekly_usd'])}",
    ]


def cmd_charter_profiles(a) -> None:
    for p in Hub().charter_profiles():
        infra = f" on {p['infra']}" if p.get("infra") else ""
        print(
            f"{p['name']:<16} {p['plugin']:<9} {p['accelerators'] or 'no GPU'}{infra},"
            f" up to {p['max_hours']:g} h, idle {p['idle_min']:g} min  {p['description']}"
        )


def cmd_charter_quote(a) -> None:
    q = Hub().charter_quote(a.profile, project=a.project, max_hours=a.hours, idle_min=a.idle_min)
    for i, o in enumerate(q["offers"][: a.n]):
        mark = "*" if i == 0 else " "
        rate = _money(o["rate_usd_h"])
        off = "  (not set up)" if (o.get("detail") or {}).get("enabled") is False else ""
        print(f"{mark} {o['provider']:<12} {o['region']:<20} {o['gpu']:<10} {rate}/h{off}")
    if not q.get("launchable", True):
        print("no cloud on the service's machine has credentials for this; prices are for planning")
    print(
        f"worst case {_money(q['worst_usd'])} ({q['max_hours']:g} h at the first offer);"
        + (" pre-approved" if q["pre_approved"] else " needs a purser: " + "; ".join(q["why_not"]))
    )
    for line in _usage_lines(q["usage"]):
        print(line)


def cmd_charter_request(a) -> None:
    c = Hub().charter_request(
        a.profile,
        project=a.project,
        max_hours=a.hours,
        idle_min=a.idle_min,
        note=a.note or "",
        approve=a.approve,
    )
    print(_charter_line(c))
    if c["state"] == "requested":
        print("  waiting for a purser: " + "; ".join(c.get("why_not") or []))
        print(f"  a purser approves it with: labtender charter approve {c['id']}")


def cmd_charter_approve(a) -> None:
    print(_charter_line(Hub().charter_approve(a.id)))


def cmd_charter_reject(a) -> None:
    print(_charter_line(Hub().charter_reject(a.id, a.reason or "")))


def cmd_charter_log(a) -> None:
    r = Hub().charter_log(a.id, a.bytes)
    p = r.get("progress")
    if p:
        label = f": {p['label']}" if p.get("label") else ""
        print(f"# {p['phase']} step {p['step']} of {p['steps']}{label}")
    print(r["tail"], end="" if r["tail"].endswith("\n") else "\n")


def cmd_charter_requote(a) -> None:
    c = Hub().charter_requote(a.id)
    print(_charter_line(c))
    if c["state"] == "requested":
        print(f"  waiting for a purser: labtender charter approve {c['id']}")


def cmd_charter_end(a) -> None:
    print(_charter_line(Hub().charter_end(a.id, a.reason or "")))


def cmd_charter_ls(a) -> None:
    rows = Hub().charters(all=a.all)
    for c in rows:
        print(_charter_line(c))
    if not rows:
        print("no charters" + ("" if a.all else " in the last day (--all for every one)"))


def cmd_budget_ls(a) -> None:
    for u in Hub().budgets():
        for line in _usage_lines(u):
            print(line)


def cmd_budget_set(a) -> None:
    u = Hub().set_budget(
        a.project,
        monthly_usd=a.monthly,
        per_charter_usd=a.per_charter,
        daily_usd=a.daily,
        weekly_usd=a.weekly,
        pre_max_hours=a.pre_max_hours,
        pre_idle_min=a.pre_idle_min,
    )
    for line in _usage_lines(u):
        print(line)


def cmd_backup(a) -> None:
    from pathlib import Path

    from .backup import backup

    dest = (
        Path(a.to)
        if a.to
        else default_home() / "backups" / time.strftime("labtender-%Y%m%d-%H%M%S.sqlite")
    )
    print(f"backed up to {backup(_store(), dest)}")


def cmd_events(a) -> None:
    for e in reversed(_store().events(a.n)):
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"]))
        print(
            f"{t}  {e['actor']:<12} {e['action']:<14} {e['subject'] or ''}  "
            f"{json.dumps(e['detail']) if e['detail'] else ''}"
        )


def _clock(t: float | None) -> str:
    return time.strftime("%a %H:%M", time.localtime(t)) if t else "?"


def _claim_line(c: dict) -> str:
    est = f", about {c['est_hours']:g} h" if c.get("est_hours") else ""
    share = f", shared: {c['share']}" if c.get("share") else ""
    return (
        f"claim {c['id']} {c['resource']}: {c['state']} for {c['owner']} ({c['tier']}{est}{share})"
    )


def cmd_claim(a) -> None:
    c = Hub().claim(
        a.resource,
        tier=a.tier,
        est_hours=a.hours,
        note=a.note or "",
        share=a.share,
        lease_s=a.lease_min * 60,
        pickup_min=a.pickup_min,
    )
    print(_claim_line(c))
    if c["state"] != "held":
        view = Hub().resource(a.resource)
        mine = [(i, q) for i, q in enumerate(view["queue"], 1) if q["id"] == c["id"]]
        if mine:
            pos, q = mine[0]
            print(f"  number {pos} in the queue; expected start {_clock(q['est_start'])}")
    else:
        print(
            f"  keep it alive: labtender heartbeat {c['id']} (lease {a.lease_min:g} min), "
            f"or bind it to a run: labtender bind {c['id']} <run folder>"
        )


def cmd_release(a) -> None:
    print(_claim_line(Hub().release(a.id, reason=a.reason or "", force=a.force)))


def cmd_heartbeat(a) -> None:
    print(_claim_line(Hub().heartbeat(a.id, est_hours=a.hours, lease_min=a.lease_min)))


def cmd_bind(a) -> None:
    c = Hub().bind(a.id, a.run_dir)
    if c.get("pending_run"):
        print(_claim_line(c) + f", will bind to {c['pending_run']} once a probe sees it")
    else:
        print(_claim_line(c) + f", bound to {c['run_key']}")


def cmd_claims(a) -> None:
    hub = Hub()
    if a.resource:
        v = hub.resource(a.resource)
        free = "now" if not v["holders"] else _clock(v["free_at"])
        print(
            f"{a.resource}: frees {free}" + (f" ({v['free_source']})" if v["free_source"] else "")
        )
        for c in v["holders"]:
            late = " LATE" if c["late"] else ""
            src = c["est_source"] or "no estimate"
            print(f"  holds  {c['owner']:<24} until {_clock(c['est_end'])} ({src}){late}")
        for c in v["queue"]:
            start = _clock(c["est_start"])
            print(f"  waits  {c['owner']:<24} {c['state']:<8} from {start} ({c['tier']})")
        for f in v["flags"]:
            print(f"  flag   {f['kind']}: {f['detail']}")
        return
    for c in hub.claims(all=a.all):
        print(_claim_line(c))


def cmd_whoami(a) -> None:
    w = Hub().whoami()
    print(f"{w['identity']} ({w['kind']})")


def cmd_preflight(a) -> None:
    from . import preflight

    sys.exit(
        preflight.run(
            a.card,
            a.gib,
            host=a.host,
            claim=a.claim,
            tier=a.tier,
            est_hours=a.hours,
            note=a.note or "",
            ignore=tuple(a.ignore or ()),
        )
    )


def cmd_mcp(a) -> None:
    from .mcp_server import main as mcp_main

    mcp_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labtender", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"labtender {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the labtender service (collector, API and board)")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8795)
    s.add_argument("--no-collect", action="store_true", help="serve without polling hosts")
    s.set_defaults(fn=cmd_serve)

    host = sub.add_parser("host", help="the host registry").add_subparsers(
        dest="sub", required=True
    )
    s = host.add_parser("add")
    s.add_argument("name")
    s.add_argument("--ssh", help="ssh target; defaults to the name (an ~/.ssh/config alias)")
    s.add_argument("--local", action="store_true", help="probe this machine without ssh")
    s.add_argument("--expires-in", type=float, metavar="HOURS", help="make it ephemeral")
    s.add_argument("--interval", type=float, default=45, metavar="SECONDS")
    s.add_argument("--accepts", help="claim tiers it takes, comma separated (default all)")
    s.add_argument(
        "--service",
        action="append",
        metavar="NAME=URL",
        help="a service the host offers, for example dashboard=http://host:9000",
    )
    s.add_argument(
        "--run-link",
        metavar="TEMPLATE",
        help="link from each run: {service:NAME}, {name}, {dir}, {host}",
    )
    s.add_argument(
        "--health",
        action="append",
        metavar="NAME=PATH",
        help="check a service: GET its address plus PATH each poll, for example dashboard=/healthz",
    )
    s.add_argument(
        "--fallback",
        action="append",
        metavar="NAME=URL",
        help="another address for a service, tried in order when its own does not answer;"
        " repeat for several",
    )
    s.set_defaults(fn=cmd_host_add)
    s = host.add_parser("set", help="change a host's interval, accepted tiers or ssh target")
    s.add_argument("name")
    s.add_argument("--interval", type=float, metavar="SECONDS")
    s.add_argument("--accepts", help="claim tiers it takes, comma separated; '' for all")
    s.add_argument("--ssh")
    s.add_argument(
        "--transport",
        choices=["ssh", "local"],
        help="local: probe the service's own machine; ssh: probe over ssh",
    )
    s.add_argument(
        "--service",
        action="append",
        metavar="NAME=URL",
        help="add or change a service; NAME= removes it",
    )
    s.add_argument("--run-link", metavar="TEMPLATE", help="'' clears it")
    s.add_argument(
        "--health",
        action="append",
        metavar="NAME=PATH",
        help="check a service by GETting its address plus PATH each poll; NAME= stops checking",
    )
    s.add_argument(
        "--fallback",
        action="append",
        metavar="NAME=URL",
        help="replace a service's fallback addresses, tried in order (repeat for several);"
        " NAME= removes them",
    )
    s.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="set a label; KEY= removes it (windows_gpu=off stops Windows sampling)",
    )
    s.add_argument(
        "--file",
        action="append",
        metavar="NAME=PATH[@HOURS]",
        help="show a file's age on the board, late after HOURS (default 26), for example"
        " backup=~/backups/last-ok@26; NAME= removes it",
    )
    s.set_defaults(fn=cmd_host_set)
    s = host.add_parser("rm")
    s.add_argument("name")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_host_rm)
    host.add_parser("ls").set_defaults(fn=cmd_host_ls)

    card = sub.add_parser("card", help="GPUs by their own id, wherever they are").add_subparsers(
        dest="sub", required=True
    )
    card.add_parser("ls").set_defaults(fn=cmd_card_ls)
    s = card.add_parser("name", help="rename a card; the name follows it between hosts")
    s.add_argument("card", help="its name, where it is now (host/gpuN), or its UUID (or prefix)")
    s.add_argument("name", nargs="?", help="the new name; leave out for its own name back")
    s.set_defaults(fn=cmd_card_name)
    s = card.add_parser("hold", help="hold a card back from agents; people can still claim it")
    s.add_argument("card")
    s.add_argument("reason", help="why, shown to agents, for example 'slow for simulations'")
    s.set_defaults(fn=cmd_card_hold)
    s = card.add_parser("offer", help="offer a held-back card to agents again")
    s.add_argument("card")
    s.set_defaults(fn=cmd_card_offer)
    s = card.add_parser("bandwidth", help="a card's memory bandwidth, for ranking it")
    s.add_argument("card")
    s.add_argument("gbs", type=float, nargs="?", help="GB/s; leave out for the published figure")
    s.set_defaults(fn=cmd_card_bandwidth)

    watch = sub.add_parser("watch", help="run folders to follow").add_subparsers(
        dest="sub", required=True
    )
    s = watch.add_parser("add")
    s.add_argument("host")
    s.add_argument("parser")
    s.add_argument("glob", help="run folders on the host, for example '~/runs/*/'")
    s.set_defaults(fn=cmd_watch_add)
    s = watch.add_parser("rm")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_watch_rm)
    watch.add_parser("ls").set_defaults(fn=cmd_watch_ls)

    sub.add_parser("parsers", help="list run-log parser specs").set_defaults(fn=cmd_parsers)

    s = sub.add_parser("probe", help="probe one host now")
    s.add_argument("name")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_probe)

    sub.add_parser("status", help="what the board shows, as text").set_defaults(fn=cmd_status)

    tok = sub.add_parser("token", help="API tokens").add_subparsers(dest="sub", required=True)
    s = tok.add_parser("create")
    s.add_argument("identity", help="a person, or an agent session")
    s.add_argument("--kind", choices=["person", "agent"], default="person")
    s.add_argument(
        "--role",
        action="append",
        choices=["viewer", "operator", "admin", "purser"],
        help="repeat for several (default: a person gets viewer, operator and admin, an agent"
        " viewer and operator; purser, for approving charters, is only ever granted by name)",
    )
    s.set_defaults(fn=cmd_token_create)
    s = tok.add_parser("revoke")
    s.add_argument("identity")
    s.set_defaults(fn=cmd_token_revoke)
    s = tok.add_parser("grant", help="add a role to an identity's live tokens")
    s.add_argument("identity")
    s.add_argument("role", choices=["viewer", "operator", "admin", "purser"])
    s.add_argument("--remove", action="store_true", help="take the role away instead")
    s.set_defaults(fn=cmd_token_grant)

    ch = sub.add_parser(
        "charter", help="cloud GPUs (yachts): quote, request, approve, end"
    ).add_subparsers(dest="sub", required=True)
    ch.add_parser("profiles", help="charter profiles the service offers").set_defaults(
        fn=cmd_charter_profiles
    )
    for name, fn, text in (
        ("quote", cmd_charter_quote, "prices for a profile, and whether it would be pre-approved"),
        ("request", cmd_charter_request, "charter a yacht (pre-approved, or waits for a purser)"),
    ):
        s = ch.add_parser(name, help=text)
        s.add_argument("profile")
        s.add_argument("--project")
        s.add_argument("--hours", type=float, help="time limit (default: the profile's)")
        s.add_argument("--idle-min", type=float, help="idle limit (default: the profile's)")
        if name == "quote":
            s.add_argument("-n", type=int, default=8, help="offers to show")
        else:
            s.add_argument("--note")
            s.add_argument("--approve", action="store_true", help="approve it at once (purser)")
        s.set_defaults(fn=fn)
    s = ch.add_parser("approve", help="approve a waiting charter (purser)")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_charter_approve)
    s = ch.add_parser("reject", help="reject a waiting charter (purser)")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_charter_reject)
    s = ch.add_parser("log", help="the end of a charter's set-up, sync and landing output")
    s.add_argument("id", type=int)
    s.add_argument("--bytes", type=int, default=8000, help="how much of the end to show")
    s.set_defaults(fn=cmd_charter_log)
    s = ch.add_parser("requote", help="quote a waiting charter again (its requester or a purser)")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_charter_requote)
    s = ch.add_parser("end", help="withdraw a charter, or land and destroy its yacht now")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_charter_end)
    s = ch.add_parser("ls", help="charters from the last day")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_charter_ls)

    bud = sub.add_parser("budget", help="charter budgets and limits").add_subparsers(
        dest="sub", required=True
    )
    bud.add_parser("ls", help="spend and what is left, per project").set_defaults(fn=cmd_budget_ls)
    s = bud.add_parser("set", help="change a project's limits (purser)")
    s.add_argument("project")
    s.add_argument("--monthly", type=float, metavar="USD", help="hard cap per calendar month")
    s.add_argument("--per-charter", type=float, metavar="USD", help="pre-approval threshold")
    s.add_argument("--daily", type=float, metavar="USD", help="pre-approved, rolling 24 h")
    s.add_argument("--weekly", type=float, metavar="USD", help="pre-approved, rolling 7 days")
    s.add_argument("--pre-max-hours", type=float, help="longest pre-approved time limit")
    s.add_argument("--pre-idle-min", type=float, help="longest pre-approved idle limit")
    s.set_defaults(fn=cmd_budget_set)

    s = sub.add_parser("backup", help="copy the state database now (it is also backed up daily)")
    s.add_argument("--to", help="file to write (default: $LABTENDER_HOME/backups/...)")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("events", help="the audit trail")
    s.add_argument("-n", type=int, default=30)
    s.set_defaults(fn=cmd_events)

    s = sub.add_parser("claim", help="claim a GPU (host/gpuN) or a host, or join its queue")
    s.add_argument("resource")
    s.add_argument("--tier", choices=["critical", "normal", "nice-to-have"], default="normal")
    s.add_argument("--hours", type=float, help="how long you expect to hold it")
    s.add_argument("--note")
    s.add_argument(
        "--share", metavar="AGREEMENT", help="share a held resource: who agreed and why (recorded)"
    )
    s.add_argument("--lease-min", type=float, default=10, help="heartbeat lease (default 10)")
    s.add_argument(
        "--pickup-min",
        type=float,
        help="minutes to take up an offer when the GPU frees (default 15)",
    )
    s.set_defaults(fn=cmd_claim)
    s = sub.add_parser("release", help="end a claim (held or queued)")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.add_argument("--force", action="store_true", help="a person ending someone else's claim")
    s.set_defaults(fn=cmd_release)
    s = sub.add_parser("heartbeat", help="keep a claim alive or take up an offer")
    s.add_argument("id", type=int)
    s.add_argument("--hours", type=float, help="revise the estimate")
    s.add_argument("--lease-min", type=float, help="change the claim's lease")
    s.set_defaults(fn=cmd_heartbeat)
    s = sub.add_parser("bind", help="tie a claim to a watched run; the run's life keeps it alive")
    s.add_argument("id", type=int)
    s.add_argument("run_dir")
    s.set_defaults(fn=cmd_bind)
    s = sub.add_parser("claims", help="active claims, or one resource's holders and queue")
    s.add_argument("resource", nargs="?")
    s.add_argument("--all", action="store_true", help="include ended claims")
    s.set_defaults(fn=cmd_claims)
    sub.add_parser("whoami", help="the identity of LABTENDER_TOKEN").set_defaults(fn=cmd_whoami)
    sub.add_parser(
        "mcp", help="MCP server on stdio for an agent session (uses LABTENDER_TOKEN)"
    ).set_defaults(fn=cmd_mcp)

    s = sub.add_parser(
        "preflight",
        help="check a GPU before a run (exit 0 go, 2 missing, 3 occupied, 4 short, 5 not yours)",
    )
    s.add_argument("card", type=int, help="nvidia-smi index")
    s.add_argument("gib", type=float, help="memory the run needs, GiB")
    s.add_argument("--host", help="this host's name in the registry (default: hostname)")
    s.add_argument("--claim", action="store_true", help="claim the GPU, or queue for it")
    s.add_argument("--tier", choices=["critical", "normal", "nice-to-have"], default="normal")
    s.add_argument("--hours", type=float)
    s.add_argument("--note")
    s.add_argument(
        "--ignore",
        action="append",
        metavar="TEXT",
        help="command-line text of shared services not to count as runs",
    )
    s.set_defaults(fn=cmd_preflight)
    return p


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    try:
        a.fn(a)
    except HubError as e:
        sys.exit(f"labtender: {e}")
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
