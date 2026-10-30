"""SQLite state: host registry, run watches, latest probe documents, runs and the audit trail."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .names import card_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS host (
    name        TEXT PRIMARY KEY,
    transport   TEXT NOT NULL DEFAULT 'ssh',     -- ssh | local
    ssh         TEXT,                            -- ~/.ssh/config alias or user@host
    lifecycle   TEXT NOT NULL DEFAULT 'static',  -- static | ephemeral | charter
    expires_at  REAL,
    interval_s  REAL NOT NULL DEFAULT 45,
    accepts     TEXT NOT NULL DEFAULT '[]',      -- claim tiers the host takes; [] means all
    labels      TEXT NOT NULL DEFAULT '{}',
    services    TEXT NOT NULL DEFAULT '{}',      -- {name: url}, e.g. a dashboard on the host
    health      TEXT NOT NULL DEFAULT '{}',      -- {service name: path to GET}, optional
    fallbacks   TEXT NOT NULL DEFAULT '{}',      -- {service name: [address, ...]}, tried in order
    files       TEXT NOT NULL DEFAULT '{}',      -- {name: {path, max_h}}: files whose age to show
    optional    TEXT NOT NULL DEFAULT '[]',      -- services that are down by design at times
    run_link    TEXT,                            -- template for links from each run
    added_at    REAL NOT NULL,
    last_seen   REAL,
    last_error  TEXT
);
CREATE TABLE IF NOT EXISTS watch (
    id      INTEGER PRIMARY KEY,
    host    TEXT NOT NULL REFERENCES host(name) ON DELETE CASCADE,
    parser  TEXT NOT NULL,
    glob    TEXT NOT NULL,
    UNIQUE (host, parser, glob)
);
CREATE TABLE IF NOT EXISTS snapshot (
    host    TEXT PRIMARY KEY REFERENCES host(name) ON DELETE CASCADE,
    t       REAL NOT NULL,
    doc     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gpu_sample (
    host    TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    t       REAL NOT NULL,
    util    REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL
);
CREATE INDEX IF NOT EXISTS gpu_sample_t ON gpu_sample (host, idx, t);
CREATE TABLE IF NOT EXISTS card (
    uuid        TEXT PRIMARY KEY,                -- the GPU's own id, the same on any host
    name        TEXT UNIQUE,                     -- made from the uuid, or one a person gave
    model       TEXT,
    host        TEXT NOT NULL,                   -- where it was last seen
    idx         INTEGER NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run (
    key         TEXT PRIMARY KEY,                -- host|dir
    host        TEXT NOT NULL,
    dir         TEXT NOT NULL,
    parser      TEXT NOT NULL,
    state       TEXT NOT NULL,                   -- running | stale | exited | done | gone
    card        INTEGER,
    updated_at  REAL NOT NULL,
    detail      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    id      INTEGER PRIMARY KEY,
    t       REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    subject TEXT,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event
BEGIN SELECT RAISE(ABORT, 'the audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event
BEGIN SELECT RAISE(ABORT, 'the audit trail is append-only'); END;
CREATE TABLE IF NOT EXISTS service_health (
    host        TEXT NOT NULL REFERENCES host(name) ON DELETE CASCADE,
    service     TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    detail      TEXT NOT NULL,                   -- HTTP status, "timeout" or the error
    url         TEXT NOT NULL,
    base        TEXT,                            -- the service address that answered
    checked_at  REAL NOT NULL,
    changed_at  REAL NOT NULL,                   -- when ok last flipped
    PRIMARY KEY (host, service)
);
CREATE TABLE IF NOT EXISTS token (
    hash        TEXT PRIMARY KEY,
    identity    TEXT NOT NULL,
    kind        TEXT NOT NULL,                   -- person | agent
    roles       TEXT,                            -- JSON list; NULL means the kind's default
    created_at  REAL NOT NULL,
    revoked_at  REAL
);
"""

HISTORY_S = 24 * 3600
ROLES = ("viewer", "operator", "admin", "purser")
# purser (approving charters that cost money) is never a default: it is granted by name, and
# only to people.
DEFAULT_ROLES = {"person": ["viewer", "operator", "admin"], "agent": ["viewer", "operator"]}
# Columns added after the first release, added in place to older databases.
MIGRATIONS = (
    ("host", "services", "TEXT NOT NULL DEFAULT '{}'"),
    ("host", "run_link", "TEXT"),
    ("token", "roles", "TEXT"),
    ("host", "health", "TEXT NOT NULL DEFAULT '{}'"),
    ("host", "fallbacks", "TEXT NOT NULL DEFAULT '{}'"),
    ("service_health", "base", "TEXT"),
    ("host", "files", "TEXT NOT NULL DEFAULT '{}'"),
    ("host", "optional", "TEXT NOT NULL DEFAULT '[]'"),
    ("gpu_sample", "uuid", "TEXT"),
    ("card", "hold", "TEXT"),  # why it is held back from agents; NULL when offered
    ("card", "bandwidth_gbs", "REAL"),  # set by a person, for a model the table lacks
)
SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CARD_NAME = SERVICE_NAME


def card_key(host: str, idx: int, uuid: str | None) -> str:
    """A card's key: its UUID, or for a GPU with none (an Apple GPU) its fixed place."""
    return uuid or f"local:{host}/gpu{idx}"


def check_services(services: dict) -> dict:
    for name, url in services.items():
        if not SERVICE_NAME.match(name):
            raise ValueError(f"service name {name!r}: lower case letters, digits, - and _")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError(f"service {name}: the address must start with http:// or https://")
    return services


def check_health(health: dict, services: dict) -> dict:
    """Health paths by service name; each names a service the host has."""
    for name, path in health.items():
        if not SERVICE_NAME.match(name):
            raise ValueError(f"service name {name!r}: lower case letters, digits, - and _")
        if path and not path.startswith("/"):
            raise ValueError(f"health path for {name} must start with /, for example /healthz")
        if path and name not in services:
            raise ValueError(f"no service {name} on the host; add it with --service first")
    return health


def check_files(files: dict) -> dict:
    """{name: "PATH" or "PATH@HOURS"} as {name: {path, max_h}}; an empty value removes it.

    The probe reports each file's age; past max_h (26 h unless given) the board says it is late.
    """
    out: dict = {}
    for name, spec in files.items():
        if not SERVICE_NAME.match(name):
            raise ValueError(f"file check name {name!r}: lower case letters, digits, - and _")
        if not spec:
            out[name] = None
            continue
        path, _, hours = str(spec).rpartition("@") if "@" in str(spec) else (str(spec), "", "")
        try:
            max_h = float(hours) if hours else 26.0
        except ValueError:
            raise ValueError(f"{spec!r}: use PATH@HOURS, for example ~/backups/last@26") from None
        if not path or not 0 < max_h <= 24 * 30:
            raise ValueError(f"{spec!r}: a path, and hours between 0 and 720")
        out[name] = {"path": path, "max_h": max_h}
    return out


def check_fallbacks(fallbacks: dict, services: dict) -> dict:
    """Fallback addresses by service name: lists of http(s) addresses for services the host has."""
    for name, urls in fallbacks.items():
        if not SERVICE_NAME.match(name):
            raise ValueError(f"service name {name!r}: lower case letters, digits, - and _")
        if urls and name not in services:
            raise ValueError(f"no service {name} on the host; add it with --service first")
        for url in urls:
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"fallback for {name} must start with http:// or https://")
    return fallbacks


def health_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def add_columns(conn, migrations) -> None:
    for table, column, decl in migrations:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def default_home() -> Path:
    return Path(os.environ.get("LABTENDER_HOME", Path.home() / ".config" / "labtender"))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path, timeout=30.0, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        add_columns(self.conn, MIGRATIONS)

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ------------------------------------------------------------ audit

    def audit(self, actor: str, action: str, subject: str | None = None, **detail) -> None:
        self.conn.execute(
            "INSERT INTO event (t, actor, action, subject, detail) VALUES (?, ?, ?, ?, ?)",
            (time.time(), actor, action, subject, json.dumps(detail)),
        )

    def events(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r, detail=json.loads(r["detail"])) for r in rows]

    # ------------------------------------------------------------ hosts

    def add_host(
        self,
        name: str,
        *,
        actor: str,
        transport: str = "ssh",
        ssh: str | None = None,
        lifecycle: str = "static",
        expires_at: float | None = None,
        interval_s: float = 45,
        accepts: list[str] | None = None,
        labels: dict | None = None,
        services: dict | None = None,
        run_link: str | None = None,
        health: dict | None = None,
        fallbacks: dict | None = None,
    ) -> dict:
        services = {k: v for k, v in check_services(services or {}).items() if v}
        health = {k: v for k, v in check_health(health or {}, services).items() if v}
        fallbacks = {k: list(v) for k, v in check_fallbacks(fallbacks or {}, services).items() if v}
        if transport not in ("ssh", "local"):
            raise ValueError("transport must be ssh or local")
        if not 30 <= interval_s <= 3600:
            raise ValueError("interval_s must be between 30 and 3600 s")
        if lifecycle == "ephemeral" and expires_at is None:
            raise ValueError("an ephemeral host needs an expiry")
        with self.tx():
            self.conn.execute(
                "INSERT INTO host (name, transport, ssh, lifecycle, expires_at, interval_s,"
                " accepts, labels, services, run_link, added_at, health, fallbacks)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    transport,
                    ssh or (name if transport == "ssh" else None),
                    lifecycle,
                    expires_at,
                    interval_s,
                    json.dumps(accepts or []),
                    json.dumps(labels or {}),
                    json.dumps(services),
                    run_link or None,
                    time.time(),
                    json.dumps(health),
                    json.dumps(fallbacks),
                ),
            )
            self.audit(
                actor,
                "host.add",
                name,
                transport=transport,
                lifecycle=lifecycle,
                expires_at=expires_at,
                services=services,
                health=health,
                fallbacks=fallbacks,
            )
        return self.host(name)

    def update_host(
        self,
        name: str,
        *,
        actor: str,
        interval_s: float | None = None,
        accepts: list[str] | None = None,
        ssh: str | None = None,
        services: dict | None = None,
        run_link: str | None = None,
        labels: dict | None = None,
        transport: str | None = None,
        health: dict | None = None,
        fallbacks: dict | None = None,
        files: dict | None = None,
        optional: dict | None = None,
    ) -> dict:
        """Change a host's interval, accepted tiers, transport, ssh target, services, health
        paths, fallback addresses, optional services, run link or labels.

        ``services`` and ``health`` merge into the host's maps; a name with an empty value is
        removed, and removing a service removes its health path and fallbacks. ``fallbacks``
        replaces each named service's list; an empty list removes it. ``optional`` maps service
        names to true (down at times by design: shown as off, no flag) or false. An empty
        ``run_link`` clears it.
        """
        if interval_s is not None and not 30 <= interval_s <= 3600:
            raise ValueError("interval_s must be between 30 and 3600 s")
        if transport is not None and transport not in ("ssh", "local"):
            raise ValueError("transport must be ssh or local")
        changes = {
            k: v
            for k, v in (("interval_s", interval_s), ("ssh", ssh), ("transport", transport))
            if v is not None
        }
        if transport == "ssh" and ssh is None and not (self.host(name) or {}).get("ssh"):
            changes["ssh"] = name  # an ~/.ssh/config alias named like the host
        if accepts is not None:
            changes["accepts"] = json.dumps(accepts)
        merged = None
        if services is not None:
            current = self.host(name)
            if current is None:
                raise ValueError(f"no host {name}")
            merged = dict(current["services"])
            for k, v in check_services(services).items():
                if v:
                    merged[k] = v
                else:
                    merged.pop(k, None)
            changes["services"] = json.dumps(merged)
        merged_health = None
        if health is not None or merged is not None:
            current = self.host(name)
            if current is None:
                raise ValueError(f"no host {name}")
            services_now = merged if merged is not None else current["services"]
            merged_health = dict(current["health"])
            for k, v in check_health(health or {}, services_now).items():
                if v:
                    merged_health[k] = v
                else:
                    merged_health.pop(k, None)
            merged_health = {k: v for k, v in merged_health.items() if k in services_now}
            if merged_health != current["health"]:
                changes["health"] = json.dumps(merged_health)
        merged_fallbacks = None
        if fallbacks is not None or merged is not None:
            current = self.host(name)
            services_now = merged if merged is not None else current["services"]
            merged_fallbacks = dict(current["fallbacks"])
            for k, v in check_fallbacks(fallbacks or {}, services_now).items():
                if v:
                    merged_fallbacks[k] = list(v)
                else:
                    merged_fallbacks.pop(k, None)
            merged_fallbacks = {k: v for k, v in merged_fallbacks.items() if k in services_now}
            if merged_fallbacks != current["fallbacks"]:
                changes["fallbacks"] = json.dumps(merged_fallbacks)
        merged_optional = None
        if optional is not None or merged is not None:
            current = self.host(name)
            if current is None:
                raise ValueError(f"no host {name}")
            services_now = merged if merged is not None else current["services"]
            missing = [k for k, v in (optional or {}).items() if v and k not in services_now]
            if missing:
                raise ValueError(
                    f"no service {missing[0]} on the host; add it with --service first"
                )
            keep = set(current["optional"]) | {k for k, v in (optional or {}).items() if v}
            keep -= {k for k, v in (optional or {}).items() if not v}
            merged_optional = sorted(k for k in keep if k in services_now)
            if merged_optional != current["optional"]:
                changes["optional"] = json.dumps(merged_optional)
        if run_link is not None:
            changes["run_link"] = run_link or None
        merged_files = None
        if files is not None:
            current = self.host(name)
            if current is None:
                raise ValueError(f"no host {name}")
            merged_files = dict(current["files"])
            for k, v in check_files(files).items():
                if v:
                    merged_files[k] = v
                else:
                    merged_files.pop(k, None)
            changes["files"] = json.dumps(merged_files)
        merged_labels = None
        if labels is not None:
            current = self.host(name)
            if current is None:
                raise ValueError(f"no host {name}")
            merged_labels = {**current["labels"], **labels}
            merged_labels = {k: v for k, v in merged_labels.items() if v not in ("", None)}
            changes["labels"] = json.dumps(merged_labels)
        if not changes:
            raise ValueError("nothing to change")
        with self.tx():
            sets = ", ".join(f"{k} = ?" for k in changes)
            n = self.conn.execute(
                f"UPDATE host SET {sets} WHERE name = ?", (*changes.values(), name)
            ).rowcount
            if not n:
                raise ValueError(f"no host {name}")
            self.audit(
                actor,
                "host.update",
                name,
                **{
                    k: accepts
                    if k == "accepts"
                    else merged
                    if k == "services"
                    else merged_health
                    if k == "health"
                    else merged_fallbacks
                    if k == "fallbacks"
                    else merged_labels
                    if k == "labels"
                    else merged_files
                    if k == "files"
                    else merged_optional
                    if k == "optional"
                    else v
                    for k, v in changes.items()
                },
            )
        return self.host(name)

    def remove_host(self, name: str, *, actor: str, reason: str = "") -> bool:
        with self.tx():
            n = self.conn.execute("DELETE FROM host WHERE name = ?", (name,)).rowcount
            self.conn.execute("DELETE FROM run WHERE host = ?", (name,))
            self.conn.execute("DELETE FROM watch WHERE host = ?", (name,))
            # GPU samples stay until they age out, so a departed host's (a destroyed yacht's)
            # load and memory can still be looked at afterwards.
            self.conn.execute("DELETE FROM service_health WHERE host = ?", (name,))
            if n:
                self.audit(actor, "host.remove", name, reason=reason)
        return bool(n)

    def host(self, name: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM host WHERE name = ?", (name,)).fetchone()
        return _host(r) if r else None

    def hosts(self) -> list[dict]:
        return [_host(r) for r in self.conn.execute("SELECT * FROM host ORDER BY name")]

    def expire_hosts(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        gone = [
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM host WHERE lifecycle = 'ephemeral' AND expires_at < ?", (now,)
            )
        ]
        for name in gone:
            self.remove_host(name, actor="labtender", reason="expired")
        return gone

    # ------------------------------------------------------------ watches

    def add_watch(self, host: str, parser: str, glob: str, *, actor: str) -> int:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO watch (host, parser, glob) VALUES (?, ?, ?)", (host, parser, glob)
            )
            self.audit(actor, "watch.add", host, parser=parser, glob=glob)
        return cur.lastrowid

    def remove_watch(self, watch_id: int, *, actor: str) -> bool:
        with self.tx():
            n = self.conn.execute("DELETE FROM watch WHERE id = ?", (watch_id,)).rowcount
            if n:
                self.audit(actor, "watch.remove", str(watch_id))
        return bool(n)

    def watches(self, host: str | None = None) -> list[dict]:
        if host:
            rows = self.conn.execute("SELECT * FROM watch WHERE host = ? ORDER BY id", (host,))
        else:
            rows = self.conn.execute("SELECT * FROM watch ORDER BY id")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ observations

    def record_probe(self, host: str, doc: dict, runs: list[dict]) -> None:
        now = time.time()
        with self.tx():
            self.conn.execute(
                "INSERT INTO snapshot (host, t, doc) VALUES (?, ?, ?) ON CONFLICT (host)"
                " DO UPDATE SET t = excluded.t, doc = excluded.doc",
                (host, now, json.dumps(doc)),
            )
            self.conn.execute(
                "UPDATE host SET last_seen = ?, last_error = NULL WHERE name = ?", (now, host)
            )
            for g in doc.get("gpus", []):
                self.conn.execute(
                    "INSERT INTO gpu_sample"
                    " (host, idx, t, util, mem_used, mem_total, power, temp, uuid)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        host,
                        g.get("index", 0),
                        now,
                        g.get("util_pct"),
                        g.get("mem_used_mib"),
                        g.get("mem_total_mib"),
                        g.get("power_w"),
                        g.get("temp_c"),
                        g.get("uuid") or None,
                    ),
                )
                self._track_card(host, g, now)
            self.conn.execute("DELETE FROM gpu_sample WHERE t < ?", (now - HISTORY_S,))
            for r in runs:
                key = f"{host}|{r['dir']}"
                old = self.conn.execute("SELECT state FROM run WHERE key = ?", (key,)).fetchone()
                self.conn.execute(
                    "INSERT INTO run (key, host, dir, parser, state, card, updated_at, detail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                    " state = excluded.state, card = excluded.card,"
                    " updated_at = excluded.updated_at, detail = excluded.detail",
                    (
                        key,
                        host,
                        r["dir"],
                        r["parser"],
                        r["state"],
                        r.get("card"),
                        now,
                        json.dumps(r),
                    ),
                )
                if old is None or old["state"] != r["state"]:
                    self.audit(
                        "labtender",
                        "run." + r["state"],
                        key,
                        previous=old["state"] if old else None,
                    )
            # A live run whose folder no longer appears was removed (or cleaned up) on the node.
            seen = [f"{host}|{r['dir']}" for r in runs]
            marks = ",".join("?" * len(seen)) or "''"
            for row in self.conn.execute(
                "SELECT key, detail FROM run WHERE host = ? AND state IN ('running', 'stale')"
                f" AND key NOT IN ({marks})",
                (host, *seen),
            ).fetchall():
                detail = dict(json.loads(row["detail"]), state="gone", eta_s=None, finish_at=None)
                self.conn.execute(
                    "UPDATE run SET state = 'gone', updated_at = ?, detail = ? WHERE key = ?",
                    (now, json.dumps(detail), row["key"]),
                )
                self.audit("labtender", "run.gone", row["key"])

    def record_health(
        self,
        host: str,
        service: str,
        url: str,
        ok: bool,
        detail: str,
        now: float | None = None,
        base: str | None = None,
    ) -> bool:
        """Store one check; True when the service went up or down since the last one."""
        now = now or time.time()
        old = self.conn.execute(
            "SELECT ok, changed_at FROM service_health WHERE host = ? AND service = ?",
            (host, service),
        ).fetchone()
        flipped = old is None or bool(old["ok"]) != ok
        self.conn.execute(
            "INSERT INTO service_health"
            " (host, service, ok, detail, url, base, checked_at, changed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (host, service) DO UPDATE SET"
            " ok = excluded.ok, detail = excluded.detail, url = excluded.url,"
            " base = excluded.base, checked_at = excluded.checked_at,"
            " changed_at = excluded.changed_at",
            (
                host,
                service,
                int(ok),
                detail,
                url,
                base,
                now,
                now if flipped else old["changed_at"],
            ),
        )
        return flipped

    def prune_health(self, host: str, keep: list[str]) -> None:
        """Forget results for services that no longer have a health path."""
        marks = ",".join("?" * len(keep)) or "''"
        self.conn.execute(
            f"DELETE FROM service_health WHERE host = ? AND service NOT IN ({marks})",
            (host, *keep),
        )

    def service_health(self, host: str) -> dict:
        """{service: {ok, detail, url, checked_at, changed_at}} for one host."""
        rows = self.conn.execute(
            "SELECT * FROM service_health WHERE host = ? ORDER BY service", (host,)
        )
        return {
            r["service"]: {
                "ok": bool(r["ok"]),
                "detail": r["detail"],
                "url": r["url"],
                "base": r["base"],
                "checked_at": r["checked_at"],
                "changed_at": r["changed_at"],
            }
            for r in rows
        }

    def active_services(self, host: dict) -> dict:
        """Each service's address to use now: the one that last answered its health check, or
        the primary address when it has no check, never answered, or is down everywhere."""
        checks = self.service_health(host["name"])
        active = {}
        for name, url in host["services"].items():
            c = checks.get(name)
            active[name] = c["base"] if c and c["ok"] and c.get("base") else url
        return active

    def record_error(self, host: str, error: str) -> None:
        self.conn.execute("UPDATE host SET last_error = ? WHERE name = ?", (error[:500], host))

    # ------------------------------------------------------------ cards

    def _track_card(self, host: str, g: dict, now: float) -> None:
        """Note where a card is now, auditing a move to another host or index."""
        idx = g.get("index", 0)
        uuid = card_key(host, idx, g.get("uuid"))
        old = self.conn.execute("SELECT * FROM card WHERE uuid = ?", (uuid,)).fetchone()
        if old is None:
            self.conn.execute(
                "INSERT INTO card (uuid, name, model, host, idx, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid, self._free_name(card_name(uuid)), g.get("name"), host, idx, now, now),
            )
            return
        self.conn.execute(
            "UPDATE card SET model = COALESCE(?, model), host = ?, idx = ?, last_seen = ?"
            " WHERE uuid = ?",
            (g.get("name"), host, idx, now, uuid),
        )
        if (old["host"], old["idx"]) != (host, idx):
            self.audit(
                "labtender",
                "card.moved",
                uuid,
                name=old["name"],
                model=g.get("name") or old["model"],
                **{"from": f"{old['host']}/gpu{old['idx']}", "to": f"{host}/gpu{idx}"},
            )

    def _free_name(self, name: str) -> str:
        """name, or name-2, name-3 ... when another card already has it."""
        taken = {r["name"] for r in self.conn.execute("SELECT name FROM card")}
        n, out = 1, name
        while out in taken:
            n += 1
            out = f"{name}-{n}"
        return out

    def cards(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM card ORDER BY host, idx, uuid")
        return [dict(r) for r in rows]

    def card(self, uuid: str | None) -> dict | None:
        if not uuid:
            return None
        r = self.conn.execute("SELECT * FROM card WHERE uuid = ?", (uuid,)).fetchone()
        return dict(r) if r else None

    def find_card(self, ref: str) -> dict | None:
        """A card by name, by where it is now (host/gpuN), or by its UUID or a unique prefix."""
        cards = self.cards()
        for c in cards:
            if ref in (c["name"], c["uuid"], f"{c['host']}/gpu{c['idx']}"):
                return c
        hits = [c for c in cards if len(ref) >= 8 and c["uuid"].startswith(ref)]
        return hits[0] if len(hits) == 1 else None

    def card_at(self, host: str, idx: int, uuid: str | None = None) -> dict | None:
        """The card at host/gpuN: by its UUID, or by its place for a GPU that has none."""
        return self.card(card_key(host, idx, uuid))

    def set_card(self, uuid: str, *, actor: str, **changes) -> dict:
        """Hold a card back from agents (hold = the reason, or None to offer it again), or give
        its memory bandwidth (bandwidth_gbs, None for the published figure)."""
        unknown = set(changes) - {"hold", "bandwidth_gbs"}
        if unknown:
            raise ValueError(f"unknown card settings: {', '.join(sorted(unknown))}")
        if "hold" in changes:
            changes["hold"] = (changes["hold"] or "").strip()[:200] or None
        bw = changes.get("bandwidth_gbs")
        if bw is not None and not 0 < bw <= 100_000:
            raise ValueError("bandwidth_gbs must be positive, in GB/s")
        with self.tx():
            if not self.card(uuid):
                raise ValueError(f"no card {uuid}")
            for k, v in changes.items():
                self.conn.execute(f"UPDATE card SET {k} = ? WHERE uuid = ?", (v, uuid))
            self.audit(actor, "card.set", uuid, **changes)
        return self.card(uuid)

    def name_card(self, uuid: str, name: str | None, *, actor: str) -> dict:
        """Rename a card; the name follows it between hosts. None or "" gives back its own name."""
        if name and not CARD_NAME.match(name):
            raise ValueError(f"card name {name!r}: lower case letters, digits, - and _")
        with self.tx():
            card = self.card(uuid)
            if not card:
                raise ValueError(f"no card {uuid}")
            if not name:
                own = card_name(uuid)
                mine = card["name"] or ""
                name = mine if mine == own or mine.startswith(own + "-") else self._free_name(own)
            try:
                self.conn.execute("UPDATE card SET name = ? WHERE uuid = ?", (name, uuid))
            except sqlite3.IntegrityError:
                raise ValueError(f"another card is already called {name}") from None
            self.audit(actor, "card.named", uuid, name=name)
        return self.card(uuid)

    def snapshot(self, host: str) -> dict | None:
        r = self.conn.execute("SELECT t, doc FROM snapshot WHERE host = ?", (host,)).fetchone()
        return {"t": r["t"], **json.loads(r["doc"])} if r else None

    def runs(
        self, host: str | None = None, since: float | None = None, dir: str | None = None
    ) -> list[dict]:
        q, args = "SELECT * FROM run WHERE 1=1", []
        if host:
            q += " AND host = ?"
            args.append(host)
        if dir:
            q += " AND dir IN (?, ?)"
            args += [dir.rstrip("/"), dir.rstrip("/") + "/"]
        if since:
            q += " AND updated_at >= ?"
            args.append(since)
        q += " ORDER BY updated_at DESC"
        return [
            dict(json.loads(r["detail"]), host=r["host"], updated_at=r["updated_at"])
            for r in self.conn.execute(q, args)
        ]

    def history_all(self, since: float) -> dict[tuple[str, int], list[dict]]:
        """Every GPU's samples since a time, by (host, index), oldest first."""
        out: dict[tuple[str, int], list[dict]] = {}
        for r in self.conn.execute(
            "SELECT host, idx, t, util, mem_used, mem_total, power, temp FROM gpu_sample"
            " WHERE t >= ? ORDER BY t",
            (since,),
        ):
            row = dict(r)
            out.setdefault((row.pop("host"), row.pop("idx")), []).append(row)
        return out

    def history(self, host: str, idx: int, since: float) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t, util, mem_used, power, temp FROM gpu_sample"
            " WHERE host = ? AND idx = ? AND t >= ? ORDER BY t",
            (host, idx, since),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ tokens

    def add_token(
        self,
        token_hash: str,
        identity: str,
        kind: str,
        *,
        actor: str,
        roles: list[str] | None = None,
    ) -> None:
        if kind not in ("person", "agent"):
            raise ValueError("kind must be person or agent")
        roles = list(dict.fromkeys(roles or DEFAULT_ROLES[kind]))
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ValueError(f"unknown role {bad[0]}; roles are {', '.join(ROLES)}")
        if kind == "agent" and "purser" in roles:
            raise ValueError("only people can hold the purser role")
        with self.tx():
            self.conn.execute(
                "INSERT INTO token (hash, identity, kind, roles, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (token_hash, identity, kind, json.dumps(roles), time.time()),
            )
            self.audit(actor, "token.create", identity, kind=kind, roles=roles)

    def identity(self, token_hash: str) -> dict | None:
        """The identity, kind and roles of a live token, or None."""
        r = self.conn.execute(
            "SELECT identity, kind, roles FROM token WHERE hash = ? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if not r:
            return None
        roles = json.loads(r["roles"]) if r["roles"] else DEFAULT_ROLES[r["kind"]]
        if r["kind"] != "person":
            roles = [x for x in roles if x != "purser"]
        return {"identity": r["identity"], "kind": r["kind"], "roles": roles}

    def agent_identities(self) -> set[str]:
        """Identities that have ever held an agent token, live or revoked."""
        rows = self.conn.execute("SELECT DISTINCT identity FROM token WHERE kind = 'agent'")
        return {r[0] for r in rows}

    def grant_role(self, identity: str, role: str, *, actor: str, revoke: bool = False) -> int:
        """Add (or with ``revoke``, remove) a role on every live token of an identity."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role}; roles are {', '.join(ROLES)}")
        with self.tx():
            rows = self.conn.execute(
                "SELECT hash, kind, roles FROM token WHERE identity = ? AND revoked_at IS NULL",
                (identity,),
            ).fetchall()
            if not rows:
                raise ValueError(f"no live token for {identity}")
            for r in rows:
                if role == "purser" and r["kind"] != "person" and not revoke:
                    raise ValueError("only people can hold the purser role")
                roles = json.loads(r["roles"]) if r["roles"] else list(DEFAULT_ROLES[r["kind"]])
                roles = (
                    [x for x in roles if x != role]
                    if revoke
                    else list(dict.fromkeys([*roles, role]))
                )
                self.conn.execute(
                    "UPDATE token SET roles = ? WHERE hash = ?", (json.dumps(roles), r["hash"])
                )
            self.audit(actor, "token.revoke-role" if revoke else "token.grant", identity, role=role)
        return len(rows)

    def revoke_tokens(self, identity: str, *, actor: str) -> int:
        with self.tx():
            n = self.conn.execute(
                "UPDATE token SET revoked_at = ? WHERE identity = ? AND revoked_at IS NULL",
                (time.time(), identity),
            ).rowcount
            self.audit(actor, "token.revoke", identity, count=n)
        return n


def _host(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["accepts"] = json.loads(d["accepts"])
    d["labels"] = json.loads(d["labels"])
    d["services"] = json.loads(d.get("services") or "{}")
    d["health"] = json.loads(d.get("health") or "{}")
    d["fallbacks"] = json.loads(d.get("fallbacks") or "{}")
    d["files"] = json.loads(d.get("files") or "{}")
    d["optional"] = json.loads(d.get("optional") or "[]")
    return d
