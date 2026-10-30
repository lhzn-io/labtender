"""The claim ledger: who holds each resource, who waits for it, and when it should free.

Claims are advisory. labtender never starts, stops or pins work; it records claims, keeps the
queue, and reconciles claims against what the probe sees on each GPU.

A resource is a GPU (``host/gpuN``) or a whole host (``host``). A claim moves through:

    queued -> offered -> held -> released | expired
       \\---------\\----------> released (withdrawn) | skipped (offer not taken up)

* A claim is held at once when the resource has no holder and nobody waiting; otherwise it
  queues, first in first out within its tier (critical, normal, nice-to-have). A higher tier
  only moves up the queue: a holder is never displaced.
* When a resource frees, the head of the queue is offered it, and takes it up by claiming or
  heartbeating within the pickup window; otherwise it is skipped.
* A held claim stays alive while heartbeats arrive within its lease, or while a run bound to it
  is still running (so a run outlives the session that started it). Past the lease it is late;
  past twice the lease it expires.
* Sharing a held resource needs a recorded agreement (``share``); the sharer is held alongside.

Every transition is written to the audit trail.
"""

from __future__ import annotations

import json
import statistics
import time

from .db import Store, add_columns

TIERS = {"critical": 0, "normal": 1, "nice-to-have": 2}
ACTIVE = ("queued", "offered", "held")
DEFAULT_LEASE_S = 600
PICKUP_S = 900
BIND_WAIT_S = 1800  # how long a bind waits for the probe to see a new run's folder
IDLE_CLAIM_S = 1200
BUSY_UTIL_PCT = 50.0
IDLE_UTIL_PCT = 5.0
GPU_MEMORY_S = 24 * 3600  # a GPU seen within this window and gone now is "missing", not removed

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim (
    id            INTEGER PRIMARY KEY,
    resource      TEXT NOT NULL,
    host          TEXT NOT NULL,
    card          INTEGER,
    owner         TEXT NOT NULL,
    owner_kind    TEXT NOT NULL,
    tier          TEXT NOT NULL,
    state         TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    share         TEXT,
    est_hours     REAL,
    lease_s       REAL NOT NULL,
    run_key       TEXT,
    created_at    REAL NOT NULL,
    offered_at    REAL,
    held_at       REAL,
    heartbeat_at  REAL,
    ended_at      REAL,
    end_reason    TEXT,
    pickup_s      REAL
);
CREATE INDEX IF NOT EXISTS claim_active ON claim (resource, state);
CREATE TABLE IF NOT EXISTS flag (
    resource  TEXT NOT NULL,
    kind      TEXT NOT NULL,          -- unclaimed-use | idle-claim | unpinned
    since     REAL NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (resource, kind)
);
"""


class LedgerError(ValueError):
    pass


def parse_resource(resource: str) -> tuple[str, int | None]:
    host, _, rest = resource.partition("/")
    if not host:
        raise LedgerError(f"bad resource {resource!r}")
    if not rest:
        return host, None
    if not rest.startswith("gpu") or not rest[3:].isdigit():
        raise LedgerError(f"bad resource {resource!r}; use host or host/gpuN")
    return host, int(rest[3:])


class Ledger:
    def __init__(self, store: Store, clock=time.time):
        self.store = store
        self.conn = store.conn
        self.now = clock
        self.conn.executescript(SCHEMA)
        add_columns(
            self.conn,
            (
                ("claim", "pickup_s", "REAL"),
                ("claim", "pending_run", "TEXT"),
                ("claim", "pending_since", "REAL"),
                ("claim", "uuid", "TEXT"),
            ),
        )

    # ------------------------------------------------------------ queries

    def claim(self, claim_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
        return dict(r) if r else None

    def claims(
        self, resource: str | None = None, active: bool = True, owner: str | None = None
    ) -> list[dict]:
        q, args = "SELECT * FROM claim WHERE 1=1", []
        if resource:
            q += " AND resource = ?"
            args.append(resource)
        if owner:
            q += " AND owner = ?"
            args.append(owner)
        if active:
            q += f" AND state IN ({','.join('?' * len(ACTIVE))})"
            args += ACTIVE
        q += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(q, args)]

    def _queue(self, resource: str) -> list[dict]:
        rows = self.claims(resource)
        waiting = [c for c in rows if c["state"] in ("queued", "offered")]
        return sorted(waiting, key=lambda c: (c["state"] != "offered", TIERS[c["tier"]], c["id"]))

    # ------------------------------------------------------------ transitions

    def _end(self, c: dict, state: str, actor: str, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE claim SET state = ?, ended_at = ?, end_reason = ? WHERE id = ?",
            (state, self.now(), reason, c["id"]),
        )
        self.store.audit(
            actor, f"claim.{state}", c["resource"], claim=c["id"], owner=c["owner"], reason=reason
        )

    def _advance(self, resource: str) -> None:
        """Offer a free resource to the head of its queue."""
        rows = self.claims(resource)
        if any(c["state"] in ("held", "offered") for c in rows):
            return
        queue = self._queue(resource)
        if queue:
            head = queue[0]
            self.conn.execute(
                "UPDATE claim SET state = 'offered', offered_at = ? WHERE id = ?",
                (self.now(), head["id"]),
            )
            self.store.audit(
                "labtender", "claim.offered", resource, claim=head["id"], owner=head["owner"]
            )
            if head["pending_run"] or head["run_key"]:
                # A run was bound to it while it waited, so the run is starting or running
                # already: the offer is taken up on its behalf rather than left to lapse.
                self._hold(head, "labtender")

    def _hold(self, c: dict, actor: str) -> None:
        now = self.now()
        self.conn.execute(
            "UPDATE claim SET state = 'held', held_at = ?, heartbeat_at = ? WHERE id = ?",
            (now, now, c["id"]),
        )
        self.store.audit(
            actor, "claim.held", c["resource"], claim=c["id"], owner=c["owner"], share=c["share"]
        )

    def _refresh(self, host: str) -> None:
        """Recompute a host's flags from its last probe, so a claim change shows at once."""
        snap = self.store.snapshot(host)
        if snap and self.store.host(host):
            self.reconcile(host, snap)

    def request(self, resource: str, owner: str, owner_kind: str, **kwargs) -> dict:
        """Claim a resource: held now, or queued. Idempotent per owner and resource."""
        c = self._request(resource, owner, owner_kind, **kwargs)
        self._refresh(c["host"])
        return self.claim(c["id"])

    def _request(
        self,
        resource: str,
        owner: str,
        owner_kind: str,
        *,
        tier: str = "normal",
        est_hours: float | None = None,
        note: str = "",
        share: str | None = None,
        lease_s: float = DEFAULT_LEASE_S,
        pickup_s: float | None = None,
    ) -> dict:
        host_name, card = parse_resource(resource)
        if tier not in TIERS:
            raise LedgerError(f"tier must be one of {', '.join(TIERS)}")
        if not 60 <= lease_s <= 24 * 3600:
            raise LedgerError("lease_s must be between 60 s and 24 h")
        pickup_s = PICKUP_S if pickup_s is None else pickup_s
        if not 60 <= pickup_s <= 24 * 3600:
            raise LedgerError("the pickup window must be between 1 min and 24 h")
        if est_hours is not None and not 0 < est_hours <= 24 * 30:
            raise LedgerError("est_hours must be positive and at most 30 days")
        with self.store.tx():
            host = self.store.host(host_name)
            if host is None:
                raise LedgerError(f"no host {host_name}")
            if host["accepts"] and tier not in host["accepts"]:
                raise LedgerError(f"{host_name} takes only {', '.join(host['accepts'])} work")
            if card is not None:
                snap = self.store.snapshot(host_name)
                known = {g.get("index") for g in (snap or {}).get("gpus", [])}
                known |= set(self._seen_gpus(host_name))  # a GPU away for travel can be queued for
                if snap and card not in known:
                    raise LedgerError(f"{host_name} has no GPU {card}")
                uuid = self._uuid_at(host_name, card)
                gone = self._moved(uuid, host_name, card)
                if gone:
                    raise LedgerError(f"{host_name} GPU {card} has moved to {gone}")
            else:
                uuid = None
            mine = [c for c in self.claims(resource) if c["owner"] == owner]
            if mine:
                c = mine[0]
                if c["state"] == "offered":
                    self._hold(c, owner)
                return self.claim(c["id"])
            held_back = card is not None and self.store.card_at(host_name, card, uuid)
            if held_back and held_back["hold"] and owner_kind == "agent":
                raise LedgerError(
                    f"{resource} is held back from agents ({held_back['hold']});"
                    " only a person can claim it"
                )
            rows = self.claims(resource)
            holders = [c for c in rows if c["state"] in ("held", "offered")]
            waiting = [c for c in rows if c["state"] == "queued"]
            if share and not holders:
                raise LedgerError("nothing to share: the resource is free, claim it plainly")
            now = self.now()
            cur = self.conn.execute(
                "INSERT INTO claim (resource, host, card, owner, owner_kind, tier, state, note,"
                " share, est_hours, lease_s, created_at, pickup_s, uuid)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    resource,
                    host_name,
                    card,
                    owner,
                    owner_kind,
                    tier,
                    "queued",
                    note,
                    share,
                    est_hours,
                    lease_s,
                    now,
                    pickup_s,
                    uuid,
                ),
            )
            c = self.claim(cur.lastrowid)
            self.store.audit(
                owner,
                "claim.request",
                resource,
                claim=c["id"],
                tier=tier,
                est_hours=est_hours,
                share=share,
                note=note,
                pickup_s=pickup_s,
            )
            if share or (not holders and not waiting):
                self._hold(c, owner)
            return self.claim(c["id"])

    def _owned(self, claim_id: int, owner: str, force: bool) -> dict:
        c = self.claim(claim_id)
        if c is None:
            raise LedgerError(f"no claim {claim_id}")
        if c["owner"] != owner and not force:
            raise LedgerError(f"claim {claim_id} belongs to {c['owner']}")
        if c["state"] not in ACTIVE:
            raise LedgerError(f"claim {claim_id} is already {c['state']}")
        return c

    def release(self, claim_id: int, actor: str, *, reason: str = "", force: bool = False) -> dict:
        """End a claim (held, offered or queued). ``force`` lets a person end someone else's."""
        with self.store.tx():
            c = self._owned(claim_id, actor, force)
            if force and c["owner"] != actor and not reason:
                raise LedgerError("releasing someone else's claim needs a reason")
            self._end(c, "released", actor, reason)
            self._advance(c["resource"])
        self._refresh(c["host"])
        return self.claim(claim_id)

    def heartbeat(
        self,
        claim_id: int,
        actor: str,
        *,
        est_hours: float | None = None,
        lease_s: float | None = None,
    ) -> dict:
        """Keep a held claim alive, take up an offer, revise the estimate or change the lease."""
        if lease_s is not None and not 60 <= lease_s <= 24 * 3600:
            raise LedgerError("lease_s must be between 60 s and 24 h")
        if est_hours is not None and not 0 < est_hours <= 24 * 30:
            raise LedgerError("est_hours must be positive and at most 30 days")
        with self.store.tx():
            c = self._owned(claim_id, actor, False)
            if c["state"] == "offered":
                self._hold(c, actor)
            elif c["state"] == "held":
                self.conn.execute(
                    "UPDATE claim SET heartbeat_at = ? WHERE id = ?", (self.now(), claim_id)
                )
            if est_hours is not None:
                self.conn.execute(
                    "UPDATE claim SET est_hours = ? WHERE id = ?", (est_hours, claim_id)
                )
            if lease_s is not None and lease_s != c["lease_s"]:
                self.conn.execute("UPDATE claim SET lease_s = ? WHERE id = ?", (lease_s, claim_id))
                self.store.audit(
                    actor, "claim.lease", c["resource"], claim=claim_id, lease_s=lease_s
                )
        self._refresh(c["host"])
        return self.claim(claim_id)

    def _find_run(self, host: str, run_dir: str) -> str | None:
        bare = run_dir.rstrip("/")
        keys = (f"{host}|{bare}", f"{host}|{bare}/")
        row = self.conn.execute("SELECT key FROM run WHERE key IN (?, ?)", keys).fetchone()
        return row["key"] if row else None

    def bind_run(self, claim_id: int, actor: str, run_dir: str) -> dict:
        """Tie a claim to a watched run, whose liveness then counts as heartbeats.

        Binding takes up an offer, and a queued claim with a run bound to it takes up its offer
        as soon as it comes: the run has evidently started.

        A run wrapper usually binds right after starting the run, before the next probe has seen
        its folder. Such a bind waits: the claim is kept alive for up to BIND_WAIT_S while the
        folder is looked for, and attaches as soon as a probe finds it.
        """
        with self.store.tx():
            c = self._owned(claim_id, actor, False)
            if c["state"] == "offered":
                self._hold(c, actor)  # binding a run to an offered claim takes the offer up
            key = self._find_run(c["host"], run_dir)
            now = self.now()
            if key is None:
                self.conn.execute(
                    "UPDATE claim SET pending_run = ?, pending_since = ?, heartbeat_at = ?"
                    " WHERE id = ?",
                    (run_dir, now, now, claim_id),
                )
                self.store.audit(
                    actor, "claim.bind-pending", c["resource"], claim=claim_id, run_dir=run_dir
                )
            else:
                self.conn.execute(
                    "UPDATE claim SET run_key = ?, pending_run = NULL, pending_since = NULL"
                    " WHERE id = ?",
                    (key, claim_id),
                )
                self.store.audit(actor, "claim.bind", c["resource"], claim=claim_id, run=key)
        self._refresh(c["host"])
        return self.claim(claim_id)

    def _settle_pending_bind(self, c: dict, now: float) -> None:
        """Attach a waiting bind once its run appears, or give up after BIND_WAIT_S."""
        key = self._find_run(c["host"], c["pending_run"])
        if key:
            self.conn.execute(
                "UPDATE claim SET run_key = ?, pending_run = NULL, pending_since = NULL,"
                " heartbeat_at = ? WHERE id = ?",
                (key, now, c["id"]),
            )
            self.store.audit(
                "labtender", "claim.bind", c["resource"], claim=c["id"], run=key, waited=True
            )
        elif now - (c["pending_since"] or now) > BIND_WAIT_S:
            # The folder never showed up (not under a watch, or the run failed to start): the
            # claim goes back to its ordinary lease, counted from now.
            self.conn.execute(
                "UPDATE claim SET pending_run = NULL, pending_since = NULL, heartbeat_at = ?"
                " WHERE id = ?",
                (now, c["id"]),
            )
            self.store.audit(
                "labtender",
                "claim.bind-expired",
                c["resource"],
                claim=c["id"],
                run_dir=c["pending_run"],
                reason="no watched run appeared",
            )

    def tick(self) -> None:
        """Expire silent claims, skip untaken offers, end claims on departed hosts."""
        now = self.now()
        hosts = {h["name"] for h in self.store.hosts()}
        touched = set()
        with self.store.tx():
            for c in self.claims():
                if c["host"] not in hosts:
                    self._end(c, "expired", "labtender", "host left the registry")
                    touched.add(c["resource"])
                elif dest := self._moved(c["uuid"], c["host"], c["card"]):
                    # The card the claim was for is elsewhere now: whatever ran on it here ended.
                    self._end(c, "expired", "labtender", f"the card moved to {dest}")
                    touched.add(c["resource"])
                elif c["state"] in ("offered", "held") and self.frozen(c):
                    # Paused while the host or GPU is away: leases and offers restart on return.
                    col = "offered_at" if c["state"] == "offered" else "heartbeat_at"
                    self.conn.execute(f"UPDATE claim SET {col} = ? WHERE id = ?", (now, c["id"]))
                elif c["state"] == "offered" and now - c["offered_at"] > (
                    c["pickup_s"] or PICKUP_S
                ):
                    self._end(c, "skipped", "labtender", "offer not taken up")
                    touched.add(c["resource"])
                elif c["state"] == "held":
                    if c["pending_run"]:
                        self._settle_pending_bind(c, now)
                        c = self.claim(c["id"])
                    if c["pending_run"]:
                        # Waiting for the probe to see the run's folder: alive meanwhile.
                        self.conn.execute(
                            "UPDATE claim SET heartbeat_at = ? WHERE id = ?", (now, c["id"])
                        )
                    elif c["run_key"] and self._run_alive(c["run_key"]):
                        self.conn.execute(
                            "UPDATE claim SET heartbeat_at = ? WHERE id = ?", (now, c["id"])
                        )
                    elif now - c["heartbeat_at"] > 2 * c["lease_s"]:
                        self._end(c, "expired", "labtender", "no heartbeat")
                        touched.add(c["resource"])
            for resource in touched:
                self._advance(resource)
        for host in {parse_resource(r)[0] for r in touched}:
            self._refresh(host)

    def _host_offline(self, host_name: str) -> bool:
        """The host has not answered for three poll intervals (asleep, travelling, down)."""
        h = self.store.host(host_name)
        if h is None or not h["last_seen"]:
            return False
        return time.time() - h["last_seen"] > 3 * max(30, h["interval_s"])

    def _seen_gpus(self, host_name: str) -> dict[int, float]:
        """GPU index -> last time the host reported it, over the last day."""
        rows = self.conn.execute(
            "SELECT idx, MAX(t) AS t FROM gpu_sample WHERE host = ? AND t >= ? GROUP BY idx",
            (host_name, time.time() - GPU_MEMORY_S),
        )
        return {r["idx"]: r["t"] for r in rows}

    def _uuid_at(self, host_name: str, idx: int) -> str | None:
        """The card at host/gpuN: from the latest probe, else the last one seen there today."""
        snap = self.store.snapshot(host_name) or {}
        for g in snap.get("gpus", []):
            if g.get("index") == idx:
                return g.get("uuid") or None
        r = self.conn.execute(
            "SELECT uuid FROM gpu_sample WHERE host = ? AND idx = ? AND uuid IS NOT NULL"
            " AND t >= ? ORDER BY t DESC LIMIT 1",
            (host_name, idx, time.time() - GPU_MEMORY_S),
        ).fetchone()
        return r["uuid"] if r else None

    def _moved(self, uuid: str | None, host_name: str, idx: int | None) -> str | None:
        """Where a card has gone, when it was last seen somewhere other than host/gpuN."""
        card = self.store.card(uuid)
        if card is None or idx is None or (card["host"], card["idx"]) == (host_name, idx):
            return None
        return f"{card['host']}/gpu{card['idx']}"

    def missing_gpus(self, host_name: str) -> dict[int, float]:
        """GPUs the host reported within the last day but not in its latest probe.

        A card that has turned up on another host (or at another index) has moved rather than
        gone missing, and is left out.
        """
        snap = self.store.snapshot(host_name)
        if not snap:
            return {}
        now_idx = {g.get("index") for g in snap.get("gpus", [])}
        return {
            i: t
            for i, t in self._seen_gpus(host_name).items()
            if i not in now_idx and not self._moved(self._uuid_at(host_name, i), host_name, i)
        }

    def frozen(self, c: dict) -> str | None:
        """Why a claim is paused rather than timed out: its host or its GPU is away."""
        if self._host_offline(c["host"]):
            return "host unreachable"
        if c["card"] is not None and c["card"] in self.missing_gpus(c["host"]):
            return "GPU missing"
        return None

    def _run_alive(self, run_key: str) -> bool:
        r = self.conn.execute("SELECT state FROM run WHERE key = ?", (run_key,)).fetchone()
        return bool(r) and r["state"] == "running"

    # ------------------------------------------------------------ estimates

    def _owner_ratio(self, owner: str) -> float:
        """Median of actual over estimated hold time for the owner's recent finished claims."""
        rows = self.conn.execute(
            "SELECT held_at, ended_at, est_hours FROM claim WHERE owner = ? AND state = 'released'"
            " AND est_hours IS NOT NULL AND held_at IS NOT NULL ORDER BY id DESC LIMIT 10",
            (owner,),
        ).fetchall()
        ratios = [(r["ended_at"] - r["held_at"]) / (r["est_hours"] * 3600) for r in rows]
        return min(3.0, max(0.5, statistics.median(ratios))) if len(ratios) >= 3 else 1.0

    def estimate(self, c: dict) -> tuple[float | None, str | None]:
        """When a held claim should end, and where that comes from.

        The later of the bound run's estimated finish (while it runs) and the claimant's own
        estimate, scaled by their history. A claim that covers a chain of runs gives an
        estimate for the whole chain, so it is not cut short by the current run's finish.
        """
        run_end = None
        if c["run_key"]:
            r = self.conn.execute(
                "SELECT state, detail FROM run WHERE key = ?", (c["run_key"],)
            ).fetchone()
            if r and r["state"] == "running":
                run_end = json.loads(r["detail"]).get("finish_at")
        own_end, own_src = None, None
        if c["est_hours"] and c["held_at"]:
            ratio = self._owner_ratio(c["owner"])
            own_src = "estimate" if ratio == 1.0 else f"estimate x{ratio:.2f} (history)"
            own_end = c["held_at"] + c["est_hours"] * 3600 * ratio
        if run_end and (own_end is None or run_end >= own_end):
            return run_end, "run"
        if own_end:
            return own_end, own_src + (", beyond the current run" if run_end else "")
        return None, None

    # ------------------------------------------------------------ reconciliation

    def reconcile(self, host: str, doc: dict) -> None:
        """Raise or clear flags from one probe document, auditing each change."""
        now = self.now()
        gpus = doc.get("gpus", [])
        procs = doc.get("gpu_procs", [])
        unplaced = [p for p in procs if p.get("card") is None]
        want: dict[tuple[str, str], str] = {}
        for g in gpus:
            res = f"{host}/gpu{g['index']}"
            held = [c for c in self.claims(res) if c["state"] == "held"]
            mine = [p for p in procs if p.get("card") == g["index"]]
            util = g.get("util_pct") or 0.0
            if not held and (mine or util >= BUSY_UTIL_PCT):
                who = ", ".join(f"{p['comm']} {p['pid']}" for p in mine) or f"load {util:.0f} %"
                want[(res, "unclaimed-use")] = who
            if held and not mine and util < IDLE_UTIL_PCT and not unplaced:
                since = min(c["held_at"] for c in held)
                if now - since > IDLE_CLAIM_S:
                    want[(res, "idle-claim")] = ", ".join(c["owner"] for c in held)
            if unplaced and len(gpus) > 1:
                want[(res, "unpinned")] = ", ".join(f"{p['comm']} {p['pid']}" for p in unplaced)
        for idx, t in self.missing_gpus(host).items():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
            want[(f"{host}/gpu{idx}", "gpu-missing")] = f"last seen {when}"
        with self.store.tx():
            have = {
                (r["resource"], r["kind"]): r["detail"]
                for r in self.conn.execute(
                    "SELECT * FROM flag WHERE resource LIKE ?", (f"{host}/%",)
                )
            }
            for key, detail in want.items():
                if key not in have:
                    self.conn.execute(
                        "INSERT INTO flag (resource, kind, since, detail) VALUES (?, ?, ?, ?)",
                        (*key, now, detail),
                    )
                    self.store.audit("labtender", f"flag.{key[1]}", key[0], detail=detail)
                elif have[key] != detail:
                    self.conn.execute(
                        "UPDATE flag SET detail = ? WHERE resource = ? AND kind = ?", (detail, *key)
                    )
            for key in have.keys() - want.keys():
                self.conn.execute("DELETE FROM flag WHERE resource = ? AND kind = ?", key)
                self.store.audit("labtender", f"flag.{key[1]}.cleared", key[0])

    def service_flag(self, host: str, down: dict[str, str]) -> None:
        """Raise, update or clear the host's service-down flag from its latest health checks."""
        key = (host, "service-down")
        detail = "; ".join(f"{name}: {why}" for name, why in sorted(down.items()))
        with self.store.tx():
            r = self.conn.execute(
                "SELECT detail FROM flag WHERE resource = ? AND kind = ?", key
            ).fetchone()
            if down and r is None:
                self.conn.execute(
                    "INSERT INTO flag (resource, kind, since, detail) VALUES (?, ?, ?, ?)",
                    (*key, self.now(), detail),
                )
                self.store.audit("labtender", "flag.service-down", host, detail=detail)
            elif down and r["detail"] != detail:
                self.conn.execute(
                    "UPDATE flag SET detail = ? WHERE resource = ? AND kind = ?", (detail, *key)
                )
            elif not down and r is not None:
                self.conn.execute("DELETE FROM flag WHERE resource = ? AND kind = ?", key)
                self.store.audit("labtender", "flag.service-down.cleared", host)

    def host_flags(self, host: str) -> list[dict]:
        """Every flag on a host and on its GPUs, including GPUs that have gone missing."""
        rows = self.conn.execute(
            "SELECT * FROM flag WHERE resource = ? OR resource LIKE ? ORDER BY resource, kind",
            (host, f"{host}/%"),
        )
        return [dict(r) for r in rows]

    def flags(self, resource: str | None = None) -> list[dict]:
        if resource:
            rows = self.conn.execute("SELECT * FROM flag WHERE resource = ?", (resource,))
        else:
            rows = self.conn.execute("SELECT * FROM flag ORDER BY resource")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ board view

    def resource_view(self, resource: str) -> dict:
        """Holders, queue with expected start times, and flags for one resource."""
        now = self.now()
        rows = self.claims(resource)
        holders = []
        free_at, free_src = now, None
        for c in rows:
            if c["state"] != "held":
                continue
            end, src = self.estimate(c)
            frozen = self.frozen(c)
            late = not frozen and now - c["heartbeat_at"] > c["lease_s"]
            holders.append(dict(c, est_end=end, est_source=src, late=late, frozen=frozen))
            if end is None:
                free_at, free_src = None, None
            elif free_at is not None and end > free_at:
                free_at, free_src = end, src
        if not holders:
            free_at = now
        queue = []
        start = free_at
        for c in self._queue(resource):
            hours = c["est_hours"]
            if hours is not None:
                hours *= self._owner_ratio(c["owner"])
            queue.append(dict(c, est_start=start))
            start = start + hours * 3600 if (start is not None and hours) else None
        return {
            "resource": resource,
            "holders": holders,
            "queue": queue,
            "free_at": free_at,
            "free_source": free_src,
            "flags": self.flags(resource),
        }
