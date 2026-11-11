"""Charters: cloud GPUs ("yachts") hired by the hour, under budgets, approvals and a reaper.

A charter moves through:

    requested -> approved -> launching -> live -> landing -> destroyed
    requested -> rejected | withdrawn         launch fails -> failed
    launch fails for lack of capacity -> waiting -> launching (again, until the retry limit)
    destroy fails -> adrift (retried every minute) -> destroyed

* **Price.** Every charter is priced at its worst case, rate x ``max_hours``. That amount is
  reserved when it is approved, and replaced by the actual cost when the yacht is destroyed.
* **Pre-approval.** A charter is approved at once when its worst case is within the project's
  per-charter threshold and fits the rolling daily and weekly limits for pre-approved spend, its
  ``max_hours`` and ``idle_min`` are within the pre-approval bounds, and it fits the monthly cap.
  Anyone with the operator role may ask, agents included. Otherwise it waits for a purser.
* **Purser.** A person holding the ``purser`` role approves or rejects a waiting charter, up to
  the monthly cap, and sets the limits. Agents never hold the role.
* **Reaper.** A live yacht is landed and destroyed when nobody holds a claim on it, or its GPUs
  have been idle, for ``idle_min``; at its time limit; when its project's actual spend reaches
  the monthly cap; or when someone ends it. Plugins also set a provider-side time limit at
  create, so a yacht dies at ``max_hours`` even if labtender is down.
* **Lost.** A yacht its provider reports gone or stopped (a prepaid balance ran out) is torn
  down without landing and billed only until then. Launches are refused when a provider's
  prepaid balance cannot carry them, and profiles can copy results off while a yacht runs.
* **Waiting for capacity.** A profile with ``launch_retry_min`` keeps an approved charter whose
  launch failed for lack of capacity: its machine is destroyed, it waits in ``waiting`` (still
  approved, its worst case still reserved, billing nothing) and is launched again every
  ``launch_retry_every_min`` until ``launch_retry_min`` after the first such failure. Each try
  is re-quoted like any launch, so a price rise still goes back to a purser.
* **Adrift.** A failed destroy is retried every minute and shown loudly. A daily sweep destroys
  any machine carrying this service's name prefix that has no live charter.

Every transition is written to the audit trail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
import tomllib
from dataclasses import asdict
from pathlib import Path

from . import yachts
from .db import Store, add_columns
from .ledger import TIERS, Ledger, LedgerError

log = logging.getLogger(__name__)

ACTIVE = ("approved", "waiting", "launching", "live", "landing", "adrift")
ENDED = ("destroyed", "failed", "rejected", "withdrawn")
DEFAULT_LIMITS = {
    "monthly_usd": 100.0,
    "per_charter_usd": 5.0,
    "daily_usd": 15.0,
    "weekly_usd": 50.0,
    "pre_max_hours": 4.0,
    "pre_idle_min": 20.0,
}
IDLE_UTIL_PCT = 5.0
IDLE_MEM_MIB = 512  # GPU memory in use above this means something is loaded and waiting
RETRY_S = 60
SWEEP_S = 24 * 3600
STATUS_S = 300  # how often a live yacht's state is asked of its provider
DONE_S = 60  # how often a live yacht's done_file is looked for
BALANCE_S = 300  # how long a provider balance is reused
GONE_STATES = ("gone", "stopped", "terminated", "down")  # lost at once; anything else twice
LAND_TRIES = 3
LAND_MARGIN_S = 600  # no landing retry this close to the provider-side time limit
DESTROY_LEAD_S = 300  # time limit: teardown starts this long (plus landing) before max_hours
MAX_STEP_MIN = 180  # longest a profile's set-up, sync or landing may be given, in minutes
MAX_RETRY_MIN = 24 * 60  # longest a charter may wait for capacity, in minutes
MIN_RETRY_EVERY_MIN = 2  # each try that fails may bill a little, so not more often than this
PRICE_TOLERANCE = 0.02  # a re-quote may rise this much (plus half a cent) without asking again
PURSER_ACTIONS = (
    "charter.request",
    "charter.approve",
    "charter.reject",
    "charter.withdraw",
    "charter.requote",
    "charter.price-rose",
    "budget.set",
    "budget.delete",
    "charter.waiting",
    "provider.balance-low",
)
STEP_S = 15
DAY_S = 24 * 3600
ACCEL = re.compile(r"^(?P<gpu>[^:]+)(?::(?P<n>\d+))?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS charter (
    id              INTEGER PRIMARY KEY,
    profile         TEXT NOT NULL,
    spec            TEXT NOT NULL,           -- the profile as requested, JSON
    plugin          TEXT NOT NULL,
    project         TEXT NOT NULL,
    requester       TEXT NOT NULL,
    requester_kind  TEXT NOT NULL,
    tier            TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL,
    approval        TEXT,                    -- pre-approved | purser
    approved_by     TEXT,
    approved_at     REAL,
    offer           TEXT NOT NULL,           -- JSON: provider, region, gpu, rate_usd_h
    rate_usd_h      REAL NOT NULL,
    max_hours       REAL NOT NULL,
    idle_min        REAL NOT NULL,
    ref             TEXT,                    -- the plugin's name for the machine
    yacht           TEXT,                    -- JSON: how to reach it
    host            TEXT,                    -- registry name once live
    claims          TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL,
    launch_at       REAL,                    -- billing may start here
    live_at         REAL,
    ended_at        REAL,
    end_reason      TEXT,
    stop_reason     TEXT,                    -- teardown asked for (by a person, agent or rule)
    idle_since      REAL,
    cost_usd        REAL,                    -- final cost, once ended
    tried_at        REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS charter_state ON charter (state);
CREATE TABLE IF NOT EXISTS budget (
    project          TEXT PRIMARY KEY,
    monthly_usd      REAL NOT NULL,
    per_charter_usd  REAL NOT NULL,
    daily_usd        REAL NOT NULL,
    weekly_usd       REAL NOT NULL,
    pre_max_hours    REAL NOT NULL,
    pre_idle_min     REAL NOT NULL,
    updated_at       REAL,
    updated_by       TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


class CharterError(LedgerError):
    pass


# ------------------------------------------------------------ profiles


# Profiles and stored charters from before the rename name the results trip by its old word.
RENAMED = {"drain": "land", "drain_timeout_min": "land_timeout_min"}


def renamed(p: dict) -> dict:
    """Move keys from before the rename to their new names, in place."""
    for old, new in RENAMED.items():
        if old in p:
            p.setdefault(new, p.pop(old))
    return p


def check_profile(p: dict) -> dict:
    p = renamed(dict(p))
    if not p.get("name"):
        raise CharterError("a charter profile needs a name")
    if not p.get("plugin"):
        raise CharterError(f"profile {p['name']}: plugin is required")
    p.setdefault("description", "")
    p.setdefault("accelerators", "")
    p.setdefault("max_hours", 4.0)
    p.setdefault("idle_min", 20.0)
    p.setdefault("project", "default")
    p.setdefault("tier", "normal")
    p.setdefault("bootstrap", [])
    p.setdefault("land", [])
    p.setdefault("land_timeout_min", 15.0)
    # Copies of results taken while the yacht runs, so a yacht lost early (a provider stopping
    # it, an empty prepaid balance) costs at most the work since the last copy.
    p.setdefault("sync", [])
    p.setdefault("sync_every_min", 10.0)
    p.setdefault("sync_timeout_min", 10.0)
    # Bootstrap runs before the yacht is live, so the reaper never sees slow set-up (package
    # builds) as idle time; it still counts against the time limit.
    p.setdefault("bootstrap_timeout_min", 30.0)
    # A file the work writes on the yacht when it is over (its content, if any, an exit status).
    # labtender lands the charter as soon as it appears, so the idle limit is only a backstop.
    p.setdefault("done_file", "")
    # A launch that fails for lack of capacity is tried again every launch_retry_every_min
    # until launch_retry_min after the first such failure; 0 (the default) gives up at once.
    p.setdefault("launch_retry_min", 0.0)
    p.setdefault("launch_retry_every_min", 10.0)
    # Run folders to follow on the yacht while it is live, as a table or a list of tables.
    w = p.get("watch", [])
    p["watch"] = [w] if isinstance(w, dict) else list(w)
    for w in p["watch"]:
        if not isinstance(w, dict) or not w.get("parser") or not w.get("glob"):
            raise CharterError(f"profile {p['name']}: each watch needs a parser and a glob")
    check_bounds(float(p["max_hours"]), float(p["idle_min"]))
    for key in ("land_timeout_min", "sync_timeout_min", "bootstrap_timeout_min"):
        if not 1 <= float(p[key]) <= MAX_STEP_MIN:
            raise CharterError(f"profile {p['name']}: {key} must be between 1 and {MAX_STEP_MIN}")
    if not 1 <= float(p["sync_every_min"]) <= 24 * 60:
        raise CharterError(f"profile {p['name']}: sync_every_min must be between 1 and 1440")
    if not 0 <= float(p["launch_retry_min"]) <= MAX_RETRY_MIN:
        raise CharterError(
            f"profile {p['name']}: launch_retry_min must be between 0 and {MAX_RETRY_MIN}"
        )
    if not MIN_RETRY_EVERY_MIN <= float(p["launch_retry_every_min"]) <= MAX_STEP_MIN:
        raise CharterError(
            f"profile {p['name']}: launch_retry_every_min must be between"
            f" {MIN_RETRY_EVERY_MIN} and {MAX_STEP_MIN}"
        )
    if p["tier"] not in TIERS:
        raise CharterError(f"profile {p['name']}: tier must be one of {', '.join(TIERS)}")
    if p["accelerators"] and not ACCEL.match(p["accelerators"]):
        raise CharterError(f"profile {p['name']}: accelerators look like L4:1")
    return p


def check_bounds(max_hours: float, idle_min: float) -> None:
    if not 0 < max_hours <= 24 * 7:
        raise CharterError("max_hours must be more than 0 and at most a week")
    if not 5 <= idle_min <= 24 * 60:
        raise CharterError("idle_min must be between 5 minutes and a day")


def load_profiles(home: Path) -> dict[str, dict]:
    """Charter profiles from ``$LABTENDER_HOME/charters/*.toml``. A file that does not load is
    left out (and logged) rather than taking every other profile with it; the profiles page
    lists it with the reason."""
    out: dict[str, dict] = {}
    folder = home / "charters"
    if folder.is_dir():
        for f in sorted(folder.glob("*.toml")):
            try:
                p = tomllib.loads(f.read_text(encoding="utf-8"))
                p.setdefault("name", f.stem)
                out[p["name"]] = check_profile(p)
            except (tomllib.TOMLDecodeError, CharterError, OSError, UnicodeDecodeError) as e:
                log.warning("charter profile %s does not load: %s", f.name, e)
    return out


PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROFILE_KEEP = 20  # earlier versions of each profile kept in charters/.history
# What the open listing shows of a profile: never its commands.
PROFILE_FIELDS = (
    "description",
    "plugin",
    "accelerators",
    "project",
    "tier",
    "max_hours",
    "idle_min",
    "launch_retry_min",
)


def parse_profile(text: str, name: str) -> dict:
    """A profile's TOML, checked as the loader checks it, for a file named ``name``."""
    try:
        p = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CharterError(f"not valid TOML: {e}") from None
    p.setdefault("name", name)
    if p["name"] != name:
        raise CharterError(f"name = {p['name']!r} must match the profile's name, {name!r}")
    return check_profile(p)


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_KEEP = 6000  # characters of a provider or set-up error kept on the charter
NO_CAPACITY = re.compile(r"Failed to acquire resources in (\S+) for")
CAPACITY_WORDS = (
    "RESOURCE_POOL_EXHAUSTED",
    "Failed to provision all possible launchable resources",
)


def clean_error(text: str) -> str:
    """A plugin's error without terminal colour codes or trailing blank lines."""
    return ANSI.sub("", text).strip()


def summarize_error(text: str) -> str:
    """One line for the board: capacity shortages by zone, otherwise the first line."""
    text = clean_error(text)
    zones = list(dict.fromkeys(NO_CAPACITY.findall(text)))
    if zones or any(w in text for w in CAPACITY_WORDS):
        return "no capacity" + (f" in {', '.join(zones)}" if zones else "")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first if len(first) <= 160 else first[:157] + "..."


def is_capacity_error(text: str) -> bool:
    """Whether a launch failed only because the provider had no machine to give."""
    return summarize_error(text).startswith("no capacity")


def gpu_count(accelerators: str) -> int:
    m = ACCEL.match(accelerators or "")
    if not m:
        return 0
    return int(m.group("n") or 1)


# ------------------------------------------------------------ charters


class Charters:
    def __init__(
        self,
        store: Store,
        ledger: Ledger,
        home: Path,
        clock=time.time,
        plugins: dict[str, yachts.Plugin] | None = None,
    ):
        self.store = store
        self.conn = store.conn
        self.ledger = ledger
        self.home = home
        self.now = clock
        self.conn.executescript(SCHEMA)
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(charter)")}
        if "drain_tries" in have:  # databases from before the rename
            with self.store.tx():
                self.conn.execute("ALTER TABLE charter RENAME COLUMN drain_tries TO land_tries")
                self.conn.execute("UPDATE charter SET state = 'landing' WHERE state = 'draining'")
        add_columns(
            self.conn,
            (
                ("charter", "synced_at", "REAL"),  # last periodic copy of results
                ("charter", "status_at", "REAL"),  # last provider state check
                ("charter", "status_misses", "INTEGER NOT NULL DEFAULT 0"),
                ("charter", "lost_at", "REAL"),  # when the provider was seen to have ended it
                ("charter", "land_tries", "INTEGER NOT NULL DEFAULT 0"),
                ("charter", "quoted_at", "REAL"),  # when its rate was last quoted
                ("charter", "waiting_since", "REAL"),  # first launch that found no capacity
                ("charter", "launch_tries", "INTEGER NOT NULL DEFAULT 0"),
                ("charter", "spent_usd", "REAL NOT NULL DEFAULT 0"),  # billed by earlier tries
            ),
        )
        self.plugins: dict[str, yachts.Plugin] = dict(plugins or {})
        self._balances: dict[str, tuple[float, dict]] = {}
        self._low: set[str] = set()
        self._balance_checked = 0.0
        # The step each charter's set-up, sync or land is on, written from worker threads.
        self._progress: dict[int, dict] = {}
        self._busy: set[int] = set()
        self._done_checked: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_sweep = 0.0
        self._stop: asyncio.Event | None = None

    # ---------------------------------------------------- lookups

    @property
    def prefix(self) -> str:
        """The name prefix of this service's machines, so a sweep never touches anyone else's."""
        r = self.conn.execute("SELECT value FROM meta WHERE key = 'install'").fetchone()
        if r:
            install = r["value"]
        else:
            install = secrets.token_hex(3)
            self.conn.execute("INSERT INTO meta (key, value) VALUES ('install', ?)", (install,))
        return f"lt-{install}-"

    def profiles(self) -> dict[str, dict]:
        return load_profiles(self.home)

    def profile(self, name: str) -> dict:
        p = self.profiles().get(name)
        f = self.home / "charters" / f"{name}.toml"
        if p is None and PROFILE_NAME.match(name) and f.is_file():
            # Left out because it does not load: say why, not that it is missing.
            try:
                parse_profile(f.read_text(encoding="utf-8"), name)
            except (OSError, UnicodeDecodeError) as e:
                raise CharterError(f"profile {name} cannot be read: {e}") from None
        if p is None:
            have = ", ".join(sorted(self.profiles())) or "none yet"
            raise CharterError(f"no charter profile {name!r} (have: {have})")
        return p

    # ---------------------------------------------------- profile files
    #
    # Profiles hold commands that run on this service's machine, so reading their source and
    # changing them is for people with the admin role (the server checks); the public listing
    # leaves the commands out. A charter keeps the profile as it was requested, so editing or
    # deleting one never changes a charter already made from it.

    def _profile_path(self, name: str) -> Path:
        if not PROFILE_NAME.match(name):
            raise CharterError(
                "a profile name is 1 to 64 lowercase letters, digits, dots, dashes or"
                " underscores, starting with a letter or digit"
            )
        return self.home / "charters" / f"{name}.toml"

    def profile_files(self) -> list[dict]:
        """Every profile file, loadable or not, so a broken one can be seen and fixed."""
        out = []
        folder = self.home / "charters"
        busy = self._profiles_in_use()
        for f in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
            row: dict = {"name": f.stem, "ok": True, "error": None, "in_use": busy.get(f.stem, [])}
            try:
                p = parse_profile(f.read_text(encoding="utf-8"), f.stem)
                row |= {k: p[k] for k in PROFILE_FIELDS} | {"infra": p.get("infra", "")}
            except (CharterError, OSError, UnicodeDecodeError) as e:
                row |= {"ok": False, "error": str(e)}
            out.append(row)
        return out

    def _profiles_in_use(self) -> dict[str, list[int]]:
        busy: dict[str, list[int]] = {}
        for c in self.charters(("requested", *ACTIVE)):
            busy.setdefault(c["profile"], []).append(c["id"])
        return busy

    def profile_source(self, name: str) -> dict:
        path = self._profile_path(name)
        if not path.is_file():
            raise CharterError(f"no charter profile {name!r}")
        text = path.read_text(encoding="utf-8")
        return {"name": name, "toml": text, "sha256": text_sha(text)}

    def save_profile(self, name: str, text: str, actor: str, base_sha: str | None) -> dict:
        """Create a profile (``base_sha`` None) or replace one (``base_sha`` the hash of the
        version the editor started from, so a change made meanwhile is never overwritten).
        The text must load as the loader loads it; the old version is kept in .history."""
        path = self._profile_path(name)
        p = parse_profile(text, name)
        self.plugin(p["plugin"])  # an unknown plugin is refused now, not at the next quote
        exists = path.is_file()
        if base_sha is None and exists:
            raise CharterError(f"profile {name} already exists; open it to edit it")
        if base_sha is not None:
            if not exists:
                raise CharterError(f"profile {name} was deleted meanwhile")
            if text_sha(path.read_text(encoding="utf-8")) != base_sha:
                raise CharterError(f"profile {name} was changed meanwhile; reload it first")
        if exists:
            self._keep_version(path, "")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{name}.toml.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        self.store.audit(
            actor,
            "profile.save",
            f"profile/{name}",
            created=not exists,
            sha256=text_sha(text)[:12],
            project=p["project"],
        )
        return {"name": name, "created": not exists, "sha256": text_sha(text)}

    def delete_profile(self, name: str, actor: str) -> dict:
        """Delete a profile no waiting or active charter came from; a copy stays in .history."""
        path = self._profile_path(name)
        if not path.is_file():
            raise CharterError(f"no charter profile {name!r}")
        busy = self._profiles_in_use().get(name)
        if busy:
            raise CharterError(
                f"charters {', '.join(map(str, busy))} from profile {name} are waiting or active"
            )
        kept = self._keep_version(path, ".deleted")
        path.unlink()
        self.store.audit(actor, "profile.delete", f"profile/{name}", kept=kept.name)
        return {"name": name, "kept": f"charters/.history/{kept.name}"}

    def _keep_version(self, path: Path, suffix: str) -> Path:
        """Copy a profile into charters/.history before it changes, keeping the newest few."""
        history = path.parent / ".history"
        history.mkdir(mode=0o700, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now()))
        dest = history / f"{path.stem}.{stamp}{suffix}.toml"
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(path.read_text(encoding="utf-8"))
        old = sorted(history.glob(f"{path.stem}.[0-9]*.toml"))
        for f in old[:-PROFILE_KEEP]:
            f.unlink()
        return dest

    def plugin(self, name: str) -> yachts.Plugin:
        if name not in self.plugins:
            try:
                self.plugins[name] = yachts.load(name)
            except yachts.PluginError as e:
                raise CharterError(str(e)) from None
        return self.plugins[name]

    def charter(self, charter_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM charter WHERE id = ?", (charter_id,)).fetchone()
        return self._row(r) if r else None

    def charters(self, states: tuple[str, ...] | None = None, since: float | None = None) -> list:
        q, args = "SELECT * FROM charter WHERE 1=1", []
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args += states
        if since is not None:
            q += " AND (ended_at IS NULL OR ended_at >= ?)"
            args.append(since)
        q += " ORDER BY id"
        return [self._row(r) for r in self.conn.execute(q, args)]

    def _row(self, r) -> dict:
        c = dict(r)
        for k in ("spec", "offer", "yacht"):
            c[k] = json.loads(c[k]) if c.get(k) else None
        if c["spec"]:
            renamed(c["spec"])
        c["claims"] = json.loads(c["claims"] or "[]")
        c["cost_so_far"] = self.cost(c)
        c["worst_usd"] = round(c["rate_usd_h"] * c["max_hours"], 2)
        c["ends_by"] = c["launch_at"] + c["max_hours"] * 3600 if c["launch_at"] else None
        c["retry_at"] = c["retry_until"] = None
        if c["state"] == "waiting" and c["spec"]:
            c["retry_at"] = (c["tried_at"] or 0) + retry_every_s(c["spec"])
            c["retry_until"] = (c["waiting_since"] or 0) + retry_window_s(c["spec"])
        return c

    def _set(self, charter_id: int, **cols) -> None:
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(f"UPDATE charter SET {sets} WHERE id = ?", (*cols.values(), charter_id))

    # ---------------------------------------------------- money

    def cost(self, c: dict, now: float | None = None) -> float:
        """What the yacht has cost: final once ended, else rate x time since launch, plus what
        earlier launches that found no capacity billed."""
        if c.get("cost_usd") is not None:
            return c["cost_usd"]
        spent = c.get("spent_usd") or 0.0
        if not c.get("launch_at"):
            return round(spent, 4)
        # A yacht its provider ended (stopped, out of balance) bills no further.
        end = c.get("lost_at") or c.get("ended_at") or (now or self.now())
        return round(spent + c["rate_usd_h"] * max(0.0, end - c["launch_at"]) / 3600, 4)

    def exposure(self, c: dict, now: float) -> float:
        """Reserved spend: the worst case while active, the actual cost once ended."""
        if c["state"] in ("requested", "rejected", "withdrawn"):
            return 0.0
        if c["state"] in ACTIVE:
            worst = (c.get("spent_usd") or 0.0) + c["rate_usd_h"] * c["max_hours"]
            return max(self.cost(c, now), worst)
        return self.cost(c, now)

    def balance(self, plugin_name: str, provider: str, fresh: bool = False) -> dict | None:
        """A provider's prepaid balance as its plugin reports it, reused for BALANCE_S; None
        when the provider has no balance to report. Blocking: call it from a thread."""
        now = self.now()
        hit = self._balances.get(plugin_name)
        if fresh or hit is None or now - hit[0] >= BALANCE_S:
            try:
                hit = (now, self.plugin(plugin_name).balances() or {})
            except Exception as e:  # noqa: BLE001  (a balance that cannot be read never blocks)
                log.warning("balance: %s: %s", plugin_name, e)
                hit = (now, {})
            self._balances[plugin_name] = hit
        return hit[1].get(provider)

    def committed(self, provider: str, now: float, skip: int | None = None) -> float:
        """What a provider may still bill for this service's active yachts: each one's rate
        for the time left to its limit (its whole limit while it is still being created)."""
        total = 0.0
        for c in self.charters(("launching", "live", "landing")):
            if c["id"] == skip or (c["offer"] or {}).get("provider") != provider:
                continue
            end = c["ends_by"] or now + c["max_hours"] * 3600
            total += c["rate_usd_h"] * max(0.0, end - now) / 3600
        return round(total, 2)

    def providers(self, now: float) -> list[dict]:
        """Cached balances against what the active yachts may still bill; ``low`` when the
        balance would run out first, which stops the provider's machines and loses their disks."""
        out = []
        for name, (t, rows) in sorted(self._balances.items()):
            for provider, b in sorted(rows.items()):
                need = self.committed(provider, now)
                out.append(
                    {
                        "plugin": name,
                        "provider": provider,
                        "balance_usd": b["balance_usd"],
                        "spend_usd_h": b.get("spend_usd_h"),
                        "committed_usd": need,
                        "low": b["balance_usd"] < need,
                        "checked_at": t,
                    }
                )
        return out

    async def _watch_balances(self) -> None:
        """Refresh the balances of providers with active yachts, and raise the alarm once when
        one would run out before those yachts' time limits."""
        now = self.now()
        active = self.charters(("launching", "live", "landing"))
        for name in sorted({c["plugin"] for c in active}):
            await asyncio.to_thread(self.balance, name, "", True)
        for p in self.providers(now):
            key = f"{p['plugin']}/{p['provider']}"
            if p["low"] and key not in self._low:
                self._low.add(key)
                self.store.audit(
                    "labtender",
                    "provider.balance-low",
                    key,
                    balance_usd=p["balance_usd"],
                    committed_usd=p["committed_usd"],
                )
            elif not p["low"]:
                self._low.discard(key)

    def limits(self, project: str) -> dict:
        r = self.conn.execute("SELECT * FROM budget WHERE project = ?", (project,)).fetchone()
        base = dict(DEFAULT_LIMITS)
        if r:
            base.update({k: r[k] for k in DEFAULT_LIMITS})
        return base

    def set_limits(self, project: str, actor: str, **changes) -> dict:
        changes = {k: float(v) for k, v in changes.items() if v is not None}
        bad = [k for k in changes if k not in DEFAULT_LIMITS]
        if bad:
            raise CharterError(f"unknown limit {bad[0]}")
        # NaN compares false against everything, so a NaN cap would let every charter through.
        if any(not math.isfinite(v) or v < 0 for v in changes.values()):
            raise CharterError("limits must be finite and not negative")
        if not changes:
            raise CharterError("nothing to change")
        new = {**self.limits(project), **changes}
        check_bounds(new["pre_max_hours"], max(5.0, new["pre_idle_min"]))
        with self.store.tx():
            self.conn.execute(
                "INSERT INTO budget (project, monthly_usd, per_charter_usd, daily_usd, weekly_usd,"
                " pre_max_hours, pre_idle_min, updated_at, updated_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (project) DO UPDATE SET"
                " monthly_usd = excluded.monthly_usd, per_charter_usd = excluded.per_charter_usd,"
                " daily_usd = excluded.daily_usd, weekly_usd = excluded.weekly_usd,"
                " pre_max_hours = excluded.pre_max_hours, pre_idle_min = excluded.pre_idle_min,"
                " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (project, *(new[k] for k in DEFAULT_LIMITS), self.now(), actor),
            )
            self.store.audit(actor, "budget.set", project, **changes)
        return self.usage(project)

    def _month_start(self, now: float) -> float:
        t = time.localtime(now)
        return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))

    def usage(self, project: str, now: float | None = None) -> dict:
        """Reserved and actual spend against each limit, and what is left."""
        now = now or self.now()
        month0 = self._month_start(now)
        rows = [
            self._row(r)
            for r in self.conn.execute(
                "SELECT * FROM charter WHERE project = ? AND approved_at IS NOT NULL"
                " AND (approved_at >= ? OR state IN (" + ",".join("?" * len(ACTIVE)) + "))",
                (project, min(month0, now - 7 * DAY_S), *ACTIVE),
            )
        ]

        def counted(c: dict, since: float) -> bool:
            return c["approved_at"] >= since or c["state"] in ACTIVE

        month = [c for c in rows if counted(c, month0)]
        pre = [c for c in rows if c["approval"] == "pre-approved"]
        lim = self.limits(project)
        used = {
            "monthly_usd": sum(self.exposure(c, now) for c in month),
            "daily_usd": sum(self.exposure(c, now) for c in pre if counted(c, now - DAY_S)),
            "weekly_usd": sum(self.exposure(c, now) for c in pre if counted(c, now - 7 * DAY_S)),
        }
        return {
            "project": project,
            "limits": lim,
            "reserved": {k: round(v, 2) for k, v in used.items()},
            "left": {k: round(max(0.0, lim[k] - v), 2) for k, v in used.items()},
            "month_actual_usd": round(sum(self.cost(c, now) for c in month), 2),
            "active": sum(1 for c in rows if c["state"] in ACTIVE),
        }

    def projects(self) -> list[str]:
        """Projects with limits set, a profile charging them, or charters this month."""
        names = {r["project"] for r in self.conn.execute("SELECT project FROM budget")}
        names |= {
            r["project"]
            for r in self.conn.execute(
                "SELECT DISTINCT project FROM charter WHERE created_at >= ? OR state IN ("
                + ",".join("?" * (len(ACTIVE) + 1))
                + ")",
                (self._month_start(self.now()), "requested", *ACTIVE),
            )
        }
        names |= {p["project"] for p in self.profiles().values()}
        return sorted(names)

    def delete_limits(self, project: str, actor: str) -> dict:
        """Delete a project's budget. Refused while a charter of the project is waiting or
        active, or a profile charges it: its charters would then fall back to the default
        limits, which may be looser than the ones set."""
        with self.store.tx():
            busy = [
                c["id"] for c in self.charters(("requested", *ACTIVE)) if c["project"] == project
            ]
            if busy:
                raise CharterError(
                    f"project {project} has charters waiting or active"
                    f" ({', '.join(map(str, busy))})"
                )
            named = sorted(n for n, p in self.profiles().items() if p["project"] == project)
            if named:
                raise CharterError(
                    f"profile{'s' if len(named) > 1 else ''} {', '.join(named)} charge"
                    f"{'' if len(named) > 1 else 's'} project {project}; change or remove"
                    f" {'them' if len(named) > 1 else 'it'} first"
                )
            if project not in self.projects():
                raise CharterError(f"no budget for project {project}")
            old = self.limits(project)
            self.conn.execute("DELETE FROM budget WHERE project = ?", (project,))
            self.store.audit(actor, "budget.delete", project, **old)
        return {"project": project, "listed": project in self.projects()}

    def precheck(
        self, project: str, rate: float, max_hours: float, idle_min: float
    ) -> tuple[bool, list[str]]:
        """Whether a charter would be pre-approved, and if not, why."""
        lim = self.limits(project)
        use = self.usage(project)
        worst = rate * max_hours
        why = []
        if use["reserved"]["monthly_usd"] + worst > lim["monthly_usd"] + 1e-9:
            why.append(f"over the monthly cap (${use['left']['monthly_usd']:.2f} left)")
        if worst > lim["per_charter_usd"] + 1e-9:
            why.append(
                f"worst case ${worst:.2f} is over the ${lim['per_charter_usd']:.2f} pre-approval"
                " threshold"
            )
        if use["reserved"]["daily_usd"] + worst > lim["daily_usd"] + 1e-9:
            why.append(f"over the daily limit (${use['left']['daily_usd']:.2f} left)")
        if use["reserved"]["weekly_usd"] + worst > lim["weekly_usd"] + 1e-9:
            why.append(f"over the weekly limit (${use['left']['weekly_usd']:.2f} left)")
        if max_hours > lim["pre_max_hours"] + 1e-9:
            why.append(f"max_hours over the pre-approved {lim['pre_max_hours']:g} h")
        if idle_min > lim["pre_idle_min"] + 1e-9:
            why.append(f"idle_min over the pre-approved {lim['pre_idle_min']:g} min")
        return not why, why

    # ---------------------------------------------------- requests and approvals

    async def quote(
        self,
        profile_name: str,
        *,
        project: str | None = None,
        max_hours: float | None = None,
        idle_min: float | None = None,
    ) -> dict:
        """Offers for a profile, the worst case of the cheapest, and whether it is pre-approved."""
        p = self.profile(profile_name)
        offers = await asyncio.to_thread(self.plugin(p["plugin"]).quote, p)
        if not offers:
            raise CharterError(f"no offers for {profile_name}")
        # Offers from clouds this machine can launch on come first; the rest are for planning.
        offers = sorted(offers, key=lambda o: (o.detail.get("enabled") is False, o.rate_usd_h))
        project = project or p["project"]
        hours = float(max_hours or p["max_hours"])
        idle = float(idle_min or p["idle_min"])
        check_bounds(hours, idle)
        ok, why = self.precheck(project, offers[0].rate_usd_h, hours, idle)
        return {
            "profile": profile_name,
            "project": project,
            "max_hours": hours,
            "idle_min": idle,
            "offers": [asdict(o) for o in offers],
            "worst_usd": round(offers[0].rate_usd_h * hours, 2),
            "launchable": offers[0].detail.get("enabled") is not False,
            "pre_approved": ok,
            "why_not": why,
            "usage": self.usage(project),
        }

    def request(
        self,
        profile_name: str,
        offer: dict,
        requester: str,
        kind: str,
        roles: list[str],
        *,
        project: str | None = None,
        max_hours: float | None = None,
        idle_min: float | None = None,
        note: str = "",
        approve: bool = False,
    ) -> dict:
        """Record a charter: approved at once (pre-approved, or by a purser asking), or waiting."""
        p = self.profile(profile_name)
        # The budget a charter draws on comes from its profile. Only a purser, who sets budgets
        # anyway, may charge another project: otherwise each new name would bring a fresh set
        # of default limits, and pre-approval would have no ceiling.
        if project and project != p["project"] and not is_purser(kind, roles):
            raise CharterError(
                f"profile {profile_name} charges project {p['project']!r}; only a purser may"
                " charge another project"
            )
        project = project or p["project"]
        hours = float(max_hours or p["max_hours"])
        idle = float(idle_min or p["idle_min"])
        check_bounds(hours, idle)
        if approve and not is_purser(kind, roles):
            raise CharterError("approving a charter needs a person with the purser role")
        if (offer.get("detail") or {}).get("enabled") is False:
            raise CharterError(
                f"no cloud set up on the service's machine offers this; the cheapest"
                f" ({offer['provider']}, ${offer['rate_usd_h']:.2f}/h) needs credentials first"
            )
        rate = float(offer["rate_usd_h"])
        now = self.now()
        with self.store.tx():
            use = self.usage(project, now)
            if (
                use["reserved"]["monthly_usd"] + rate * hours
                > self.limits(project)["monthly_usd"] + 1e-9
            ):
                raise CharterError(
                    f"worst case ${rate * hours:.2f} does not fit the monthly cap"
                    f" (${use['left']['monthly_usd']:.2f} left)"
                )
            ok, why = self.precheck(project, rate, hours, idle)
            if approve:
                state, approval, by = "approved", "purser", requester
            elif ok:
                state, approval, by = "approved", "pre-approved", "labtender"
            else:
                state, approval, by = "requested", None, None
            cur = self.conn.execute(
                "INSERT INTO charter (profile, spec, plugin, project, requester, requester_kind,"
                " tier, note, state, approval, approved_by, approved_at, offer, rate_usd_h,"
                " max_hours, idle_min, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile_name,
                    json.dumps(p),
                    p["plugin"],
                    project,
                    requester,
                    kind,
                    p["tier"],
                    note,
                    state,
                    approval,
                    by,
                    now if approval else None,
                    json.dumps(offer),
                    rate,
                    hours,
                    idle,
                    now,
                ),
            )
            cid = cur.lastrowid
            self.store.audit(
                requester,
                "charter.request",
                f"charter/{cid}",
                profile=profile_name,
                project=project,
                offer=offer,
                worst_usd=round(rate * hours, 2),
                approval=approval,
                why_not=why,
            )
            if approval:
                self.store.audit(by, "charter.approve", f"charter/{cid}", approval=approval)
        c = self.charter(cid)
        c["why_not"] = [] if approval else why
        return c

    def approve(self, charter_id: int, actor: str, kind: str, roles: list[str]) -> dict:
        if not is_purser(kind, roles):
            raise CharterError("approving a charter needs a person with the purser role")
        with self.store.tx():
            c = self._get(charter_id, ("requested",))
            use = self.usage(c["project"])
            worst = c["rate_usd_h"] * c["max_hours"]
            if (
                use["reserved"]["monthly_usd"] + worst
                > self.limits(c["project"])["monthly_usd"] + 1e-9
            ):
                raise CharterError(
                    f"worst case ${worst:.2f} does not fit the monthly cap"
                    f" (${use['left']['monthly_usd']:.2f} left)"
                )
            self._set(
                charter_id,
                state="approved",
                approval="purser",
                approved_by=actor,
                approved_at=self.now(),
            )
            self.store.audit(actor, "charter.approve", f"charter/{charter_id}", approval="purser")
        return self.charter(charter_id)

    def reject(
        self, charter_id: int, actor: str, kind: str, roles: list[str], reason: str = ""
    ) -> dict:
        if not is_purser(kind, roles):
            raise CharterError("rejecting a charter needs a person with the purser role")
        with self.store.tx():
            self._get(charter_id, ("requested",))
            self._set(
                charter_id, state="rejected", ended_at=self.now(), end_reason=reason or "rejected"
            )
            self.store.audit(actor, "charter.reject", f"charter/{charter_id}", reason=reason)
        return self.charter(charter_id)

    def end(
        self, charter_id: int, actor: str, kind: str, roles: list[str], reason: str = ""
    ) -> dict:
        """Withdraw a waiting charter, or ask for a live one to be landed and destroyed."""
        with self.store.tx():
            c = self._get(charter_id, ("requested", *ACTIVE))
            if c["requester"] != actor and not is_purser(kind, roles):
                raise CharterError(f"charter {charter_id} belongs to {c['requester']}")
            reason = reason or f"ended by {actor}"
            if c["state"] in ("requested", "approved", "waiting") and charter_id not in self._busy:
                self._set(charter_id, state="withdrawn", ended_at=self.now(), end_reason=reason)
                self.store.audit(actor, "charter.withdraw", f"charter/{charter_id}", reason=reason)
            else:
                self._set(charter_id, stop_reason=reason)
                self.store.audit(actor, "charter.end", f"charter/{charter_id}", reason=reason)
        return self.charter(charter_id)

    def _get(self, charter_id: int, states: tuple[str, ...]) -> dict:
        c = self.charter(charter_id)
        if c is None:
            raise CharterError(f"no charter {charter_id}")
        if c["state"] not in states:
            raise CharterError(f"charter {charter_id} is {c['state']}")
        return c

    # ---------------------------------------------------- lifecycle

    async def run(self) -> None:
        self._stop = asyncio.Event()
        while not self._stop.is_set():
            try:
                await self.step()
            except Exception:  # noqa: BLE001  (the reaper must keep running)
                log.exception("charter step failed")
            try:
                await asyncio.wait_for(self._stop.wait(), STEP_S)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        if self._stop:
            self._stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def step(self, wait: bool = False) -> None:
        """Launch approved charters, reap live ones, retry adrift ones, sweep for orphans."""
        now = self.now()
        started = []
        for c in self.charters(ACTIVE):
            if c["id"] in self._busy:
                continue
            if c["state"] == "approved" or (c["state"] == "waiting" and now >= c["retry_at"]):
                started.append(self._spawn(c["id"], self._launch(c)))
            elif c["state"] == "launching":
                # Only after a restart mid-launch: the machine may exist, so tear it down.
                self._begin_teardown(c, "the service restarted during launch")
                started.append(self._spawn(c["id"], self._teardown(self.charter(c["id"]))))
            elif c["state"] == "live":
                self._keep_claims(c)
                reason = self._reap_reason(c, now)
                if reason:
                    self._begin_teardown(c, reason)
                    started.append(self._spawn(c["id"], self._teardown(self.charter(c["id"]))))
                elif now - (c["status_at"] or c["live_at"] or now) >= STATUS_S:
                    started.append(self._spawn(c["id"], self._check_alive(c)))
                elif (
                    c["spec"].get("done_file")
                    and now - self._done_checked.get(c["id"], c["live_at"] or now) >= DONE_S
                ):
                    started.append(self._spawn(c["id"], self._check_done(c)))
                elif c["spec"].get("sync") and now - (c["synced_at"] or c["live_at"] or now) >= (
                    float(c["spec"].get("sync_every_min", 10.0)) * 60
                ):
                    started.append(self._spawn(c["id"], self._sync(c)))
            elif c["state"] == "landing" and now - (c["tried_at"] or 0) >= RETRY_S:
                started.append(self._spawn(c["id"], self._teardown(c)))
            elif c["state"] == "adrift" and now - (c["tried_at"] or 0) >= RETRY_S:
                started.append(self._spawn(c["id"], self._destroy(c)))
        if now - self._last_sweep >= SWEEP_S:
            self._last_sweep = now
            started.append(self._spawn(None, self.sweep()))
        if now - self._balance_checked >= BALANCE_S and self.charters(("live", "landing")):
            self._balance_checked = now
            started.append(self._spawn(None, self._watch_balances()))
        if wait and started:
            await asyncio.gather(*started)

    def _spawn(self, charter_id: int | None, coro) -> asyncio.Task:
        if charter_id is not None:
            self._busy.add(charter_id)

        async def wrapped():
            try:
                await coro
            except Exception:  # noqa: BLE001
                log.exception("charter %s task failed", charter_id)
            finally:
                if charter_id is not None:
                    self._busy.discard(charter_id)

        task = asyncio.create_task(wrapped())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _begin_teardown(self, c: dict, reason: str) -> None:
        with self.store.tx():
            self._set(c["id"], state="landing", end_reason=reason)
            self.store.audit("labtender", "charter.reap", f"charter/{c['id']}", reason=reason)

    async def _launch(self, c: dict) -> None:
        cid, spec = c["id"], c["spec"]
        plugin = self.plugin(c["plugin"])
        # Prices move between approval and launch: launch only at the approved rate or better.
        if not await self._fresh_quote(c):
            return
        c = self.charter(cid)
        ref = f"{self.prefix}{cid}"
        with self.store.tx():
            self._set(
                cid,
                state="launching",
                ref=ref,
                launch_at=self.now(),
                launch_tries=c["launch_tries"] + 1,
            )
            tries = c["launch_tries"] + 1
            self.store.audit("labtender", "charter.launch", f"charter/{cid}", ref=ref, tries=tries)
        offer = yachts.Offer(**c["offer"])
        host = f"yacht-{cid}"
        try:
            # A prepaid provider stops every machine when the balance runs out, losing their
            # disks, so never launch more than the balance can carry to the time limits.
            bal = await asyncio.to_thread(self.balance, c["plugin"], offer.provider, True)
            if bal is not None:
                need = self.committed(offer.provider, self.now(), skip=cid) + round(
                    c["rate_usd_h"] * c["max_hours"], 2
                )
                if bal["balance_usd"] < need:
                    raise CharterError(
                        f"{offer.provider} balance ${bal['balance_usd']:.2f} is below the"
                        f" ${need:.2f} this charter and the active ones could cost; top it up"
                    )
            yacht = await asyncio.to_thread(plugin.create, ref, spec, offer, c["max_hours"])
            with self.store.tx():
                self._set(cid, yacht=json.dumps(asdict(yacht)))
            await asyncio.to_thread(plugin.wait_ready, yacht)
            launch_at = self.charter(cid)["launch_at"]
            self.store.add_host(
                host,
                actor="labtender",
                transport=yacht.transport,
                ssh=yacht.ssh,
                lifecycle="charter",
                expires_at=launch_at + c["max_hours"] * 3600,
                labels={
                    "charter": str(cid),
                    "provider": yacht.provider,
                    "region": yacht.region,
                    "rate_usd_h": str(c["rate_usd_h"]),
                },
            )
            # Watched before set-up, so a run that set-up starts is seen from its first line.
            for w in spec.get("watch", []):
                self.store.add_watch(host, w["parser"], w["glob"], actor="labtender")
            if spec.get("bootstrap"):
                try:
                    await asyncio.to_thread(
                        plugin.bootstrap,
                        yacht,
                        spec["bootstrap"],
                        float(spec.get("bootstrap_timeout_min", 30.0)) * 60,
                        log=self.log_path(cid),
                        progress=self._tracker(cid, "set-up"),
                    )
                finally:
                    self._progress.pop(cid, None)
            n = gpu_count(spec.get("accelerators", ""))
            resources = [f"{host}/gpu{i}" for i in range(n)] or [host]
            claims = []
            for res in resources:
                claim = self.ledger.request(
                    res,
                    c["requester"],
                    c["requester_kind"],
                    tier=c["tier"],
                    est_hours=c["max_hours"],
                    note=f"charter {cid}",
                    lease_s=max(600.0, c["idle_min"] * 60),
                )
                claims.append(claim["id"])
            with self.store.tx():
                self._set(
                    cid,
                    state="live",
                    live_at=self.now(),
                    host=host,
                    claims=json.dumps(claims),
                    last_error=None,
                )
                self.store.audit(
                    "labtender", "charter.live", f"charter/{cid}", host=host, claims=claims
                )
        except Exception as e:  # noqa: BLE001  (any failure must still destroy the machine)
            log.warning("charter %s launch failed: %s", cid, e)
            full = clean_error(str(e))
            # The audit trail is open to read, so it gets one line; the whole error goes to the
            # charter's log, which only operators can read.
            try:
                path = self.log_path(cid)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"\n== launch failed ({time.strftime('%H:%M:%S')})\n{full}\n")
            except OSError as le:
                log.warning("charter %s: could not log the launch error: %s", cid, le)
            # No capacity is worth another try, within the profile's retry limit; anything else
            # (a set-up step, a balance, a bad image) would fail the same way again.
            cur = self.charter(cid)
            now = self.now()
            capacity = is_capacity_error(full) and retry_window_s(spec) > 0
            first = (cur["waiting_since"] or now) if capacity else cur["waiting_since"]
            retry = (
                capacity
                and not cur["stop_reason"]
                and now + retry_every_s(spec) <= first + retry_window_s(spec)
            )
            reason = f"launch failed: {summarize_error(full)}"
            if cur["launch_tries"] > 1:
                reason += f" ({cur['launch_tries']} tries over {(now - first) / 60:.0f} min)"
            with self.store.tx():
                self._set(
                    cid,
                    last_error=full[:ERROR_KEEP],
                    end_reason=None if retry else reason,
                    waiting_since=first,
                )
                self.store.audit(
                    "labtender",
                    "charter.launch-failed",
                    f"charter/{cid}",
                    error=summarize_error(full),
                    tries=cur["launch_tries"],
                    retry_at=now + retry_every_s(spec) if retry else None,
                )
            await self._destroy(self.charter(cid), failed=True, retry=retry)

    def _keep_claims(self, c: dict) -> None:
        """Heartbeat the claims a live charter created: the charter itself keeps them alive.

        The requester pays for the yacht, so it should not also have to heartbeat it. A claim
        bound to a run is left to the run; a claim the requester releases stays released, and
        the reaper then counts the yacht as idle.
        """
        for claim_id in c["claims"]:
            claim = self.ledger.claim(claim_id)
            if not claim or claim["state"] != "held" or claim["owner"] != c["requester"]:
                continue
            if claim["run_key"]:
                continue  # bound to a watched run, whose life keeps it alive
            if self.ledger.now() - (claim["heartbeat_at"] or 0) < claim["lease_s"] / 3:
                continue
            try:
                self.ledger.heartbeat(claim_id, c["requester"])
            except LedgerError as e:
                log.warning("charter %s could not keep claim %s: %s", c["id"], claim_id, e)

    def _reap_reason(self, c: dict, now: float) -> str | None:
        if c["stop_reason"]:
            return c["stop_reason"]
        if c["ends_by"] and now >= c["ends_by"] - land_lead(c):
            return "time limit"
        use = self.usage(c["project"], now)
        if use["month_actual_usd"] >= use["limits"]["monthly_usd"]:
            return "monthly cap reached"
        idle = self._idle(c)
        if not idle:
            if c["idle_since"]:
                self._set(c["id"], idle_since=None)
            return None
        if not c["idle_since"]:
            self._set(c["id"], idle_since=now)
            return None
        if now - c["idle_since"] >= c["idle_min"] * 60:
            return f"idle for {c['idle_min']:g} min ({idle})"
        return None

    def _idle(self, c: dict) -> str | None:
        """Why the yacht counts as idle now, or None while it is in use."""
        host = c["host"]
        resources = {f"{host}/gpu{i}" for i in range(gpu_count(c["spec"].get("accelerators", "")))}
        resources = resources or {host}
        held = [
            x for x in self.ledger.claims() if x["resource"] in resources and x["state"] == "held"
        ]
        if not held:
            return "no claim held"
        if any(r.get("state") == "running" for r in self.store.runs(host)):
            return None  # a watched run is going, even if it is between GPU phases
        snap = self.store.snapshot(host)
        if not snap or not snap.get("gpus"):
            return None  # not probed yet: cannot tell
        # Load and memory decide, not the process list: cloud images keep daemons that load
        # the CUDA library without using the GPU (SkyPilot's Ray runtime does), and counting
        # them kept a finished yacht alive for an hour.
        for g in snap["gpus"]:
            if (g.get("util_pct") or 0) >= IDLE_UTIL_PCT:
                return None
            if (g.get("mem_used_mib") or 0) >= IDLE_MEM_MIB:
                return None
        return "GPU idle"

    async def _status(self, c: dict) -> str:
        try:
            return str(await asyncio.to_thread(self.plugin(c["plugin"]).status, c["ref"])).lower()
        except Exception as e:  # noqa: BLE001  (an unanswered question is not an answer)
            log.warning("charter %s status: %s", c["id"], e)
            return "unknown"

    async def _check_alive(self, c: dict) -> None:
        """Ask the provider whether a live yacht still exists. One that is gone or stopped (a
        prepaid balance ran out, the provider reclaimed it) is lost: its meter stops there and
        it is torn down. Any other state but up is lost only when seen twice in a row."""
        cid = c["id"]
        status = await self._status(c)
        now = self.now()
        misses = 0 if status in ("up", "unknown") else c["status_misses"] + 1
        lost = status in GONE_STATES or misses >= 2
        with self.store.tx():
            self._set(cid, status_at=now, status_misses=misses)
            if lost:
                self._set(cid, lost_at=now, stop_reason=f"lost: the provider reports it {status}")
                self.store.audit("labtender", "charter.lost", f"charter/{cid}", status=status)

    async def _check_done(self, c: dict) -> None:
        """Land the charter once its work has written the profile's done_file on the yacht."""
        cid = c["id"]
        self._done_checked[cid] = self.now()
        try:
            text = await asyncio.to_thread(
                self.plugin(c["plugin"]).read_file,
                yachts.Yacht(**c["yacht"]),
                c["spec"]["done_file"],
            )
        except Exception as e:  # noqa: BLE001  (an unanswered question is not an answer)
            log.info("charter %s: done_file not read: %s", cid, e)
            return
        if text is None:
            return
        result = text.strip()[:200]
        c = self.charter(cid)
        if c["state"] != "live":
            return
        ok = result in ("", "0")
        self.store.audit("labtender", "charter.done", f"charter/{cid}", result=result, ok=ok)
        self._done_checked.pop(cid, None)
        self._begin_teardown(c, "work done" if ok else f"work done, exit {result}")

    async def _sync(self, c: dict) -> None:
        """Copy results off a live yacht with the profile's sync commands; best effort."""
        cid, spec = c["id"], c["spec"]
        with self.store.tx():
            self._set(cid, synced_at=self.now())
        try:
            await asyncio.to_thread(
                self.plugin(c["plugin"]).land,
                yachts.Yacht(**c["yacht"]),
                spec["sync"],
                float(spec.get("sync_timeout_min", 10.0)) * 60,
                log=self.log_path(cid),
                progress=self._tracker(cid, "sync"),
            )
        except Exception as e:  # noqa: BLE001  (a missed copy is retried at the next interval)
            self.store.audit(
                "labtender", "charter.sync-failed", f"charter/{cid}", error=str(e)[:500]
            )
        finally:
            self._progress.pop(cid, None)

    async def _teardown(self, c: dict) -> None:
        cid = c["id"]
        plugin = self.plugin(c["plugin"])
        spec = c["spec"]
        if c["yacht"] and spec.get("land") and not c["lost_at"]:
            yacht = yachts.Yacht(**c["yacht"])
            try:
                await asyncio.to_thread(
                    plugin.land,
                    yacht,
                    spec["land"],
                    float(spec["land_timeout_min"]) * 60,
                    log=self.log_path(cid),
                    progress=self._tracker(cid, "landing"),
                    values={
                        "charter": cid,
                        "cost_usd": f"{self.cost(c):.2f}",
                        "rate_usd_h": f"{c['rate_usd_h']:.4g}",
                    },
                )
                self.store.audit("labtender", "charter.landed", f"charter/{cid}")
            except Exception as e:  # noqa: BLE001
                tries = c["land_tries"] + 1
                self.store.audit(
                    "labtender",
                    "charter.land-failed",
                    f"charter/{cid}",
                    error=str(e)[:500],
                    attempt=tries,
                )
                # Results matter, but money first: retry only while the yacht is still up, a
                # few times, and never close to the provider-side time limit.
                now = self.now()
                if (
                    tries < LAND_TRIES
                    and (not c["ends_by"] or now < c["ends_by"] - LAND_MARGIN_S)
                    and await self._status(c) == "up"
                ):
                    with self.store.tx():
                        self._set(cid, land_tries=tries, tried_at=now)
                    return
        await self._destroy(self.charter(cid))

    async def _destroy(self, c: dict, failed: bool = False, retry: bool = False) -> None:
        """Destroy a charter's machine and end it, or with ``retry`` (a launch that found no
        capacity, inside its retry limit) put it back to wait for the next try."""
        cid = c["id"]
        plugin = self.plugin(c["plugin"])
        now = self.now()
        self._progress.pop(cid, None)
        try:
            if c["ref"]:
                await asyncio.to_thread(plugin.destroy, c["ref"])
                status = await asyncio.to_thread(plugin.status, c["ref"])
                if status not in ("gone", "terminated", "down"):
                    raise yachts.PluginError(f"still {status} after destroy")
        except Exception as e:  # noqa: BLE001
            with self.store.tx():
                self._set(
                    cid,
                    state="adrift",
                    tried_at=now,
                    attempts=c["attempts"] + 1,
                    last_error=clean_error(str(e))[:ERROR_KEEP],
                )
                self.store.audit(
                    "labtender", "charter.adrift", f"charter/{cid}", error=clean_error(str(e))[:500]
                )
            return
        for claim_id in c["claims"]:
            claim = self.ledger.claim(claim_id)
            if claim and claim["state"] in ("queued", "offered", "held"):
                self.ledger.release(
                    claim_id, "labtender", reason="the yacht was destroyed", force=True
                )
        if c["host"] or self.store.host(f"yacht-{cid}"):
            self.store.remove_host(f"yacht-{cid}", actor="labtender", reason="charter ended")
        if retry:
            # Still approved and its worst case still reserved; this try's few seconds of
            # billing carry over, and the meter starts again at the next launch.
            with self.store.tx():
                self._set(
                    cid,
                    state="waiting",
                    ref=None,
                    yacht=None,
                    host=None,
                    claims="[]",
                    launch_at=None,
                    spent_usd=self.cost(dict(c, ended_at=now, cost_usd=None), now),
                    tried_at=now,
                )
                w = self.charter(cid)
                self.store.audit(
                    "labtender",
                    "charter.waiting",
                    f"charter/{cid}",
                    retry_at=w["retry_at"],
                    retry_until=w["retry_until"],
                    spent_usd=w["spent_usd"],
                )
            return
        state = "failed" if failed else "destroyed"
        with self.store.tx():
            ended = dict(c, ended_at=now, cost_usd=None)
            self._set(
                cid,
                state=state,
                ended_at=now,
                cost_usd=self.cost(ended, now),
                end_reason=c["end_reason"] or ("launch failed" if failed else "ended"),
            )
            self.store.audit(
                "labtender",
                f"charter.{state}",
                f"charter/{cid}",
                cost_usd=self.cost(ended, now),
                reason=c["end_reason"],
            )

    async def sweep(self) -> list[str]:
        """Destroy machines with this service's prefix that have no active charter."""
        known = {c["ref"] for c in self.charters(ACTIVE) if c["ref"]}
        names = {c["plugin"] for c in self.charters()} | {
            p["plugin"] for p in self.profiles().values()
        }
        destroyed = []
        for name in sorted(names):
            try:
                plugin = self.plugin(name)
                refs = await asyncio.to_thread(plugin.clusters)
            except Exception as e:  # noqa: BLE001
                log.warning("sweep: %s: %s", name, e)
                continue
            for ref in refs:
                if ref.startswith(self.prefix) and ref not in known:
                    try:
                        await asyncio.to_thread(plugin.destroy, ref)
                        self.store.audit("labtender", "charter.orphan-destroyed", ref, plugin=name)
                        destroyed.append(ref)
                    except Exception as e:  # noqa: BLE001
                        self.store.audit(
                            "labtender", "charter.orphan-adrift", ref, error=str(e)[:500]
                        )
        return destroyed

    # ---------------------------------------------------- progress and logs

    def log_path(self, charter_id: int) -> Path:
        """Where a charter's set-up, sync and landing output is kept (``charter-logs/<id>.log``)."""
        return self.home / "charter-logs" / f"{charter_id}.log"

    def _tracker(self, charter_id: int, phase: str):
        """A progress callback for one phase: the board shows the step, its label and times."""
        phase_at = self.now()

        def progress(step: int, steps: int, label: str | None) -> None:
            self._progress[charter_id] = {
                "phase": phase,
                "step": step,
                "steps": steps,
                "label": label,
                "step_at": self.now(),
                "phase_at": phase_at,
            }

        return progress

    def log_tail(self, charter_id: int, limit: int = 8000) -> dict:
        """The end of a charter's log, with the step it is on now."""
        path = self.log_path(charter_id)
        text = ""
        if path.exists():
            with open(path, "rb") as f:
                f.seek(max(0, path.stat().st_size - limit))
                text = f.read().decode("utf-8", "replace")
        return {"progress": self._progress.get(charter_id), "tail": text}

    # ---------------------------------------------------- fresh quotes

    @staticmethod
    def _same_offer(offers: list[yachts.Offer], old: dict) -> yachts.Offer | None:
        """The offer for the machine a charter was quoted: same provider, region and, when
        known, instance type."""
        same = [o for o in offers if (o.provider, o.region) == (old["provider"], old["region"])]
        kind = (old.get("detail") or {}).get("instance_type")
        exact = [o for o in same if not kind or o.detail.get("instance_type") == kind]
        return min(exact or same, key=lambda o: o.rate_usd_h, default=None)

    def _reprice(self, c: dict, offer: yachts.Offer, actor: str, at_launch: bool) -> dict:
        """Put a waiting or approved charter on a fresh quote. A rate that rose past the
        tolerance sends an approved charter back to a purser, unless pre-approval still
        covers it (and it was pre-approved in the first place)."""
        cid, old = c["id"], c["rate_usd_h"]
        new = offer.rate_usd_h
        rose = new > old * (1 + PRICE_TOLERANCE) + 0.005
        state, approval = c["state"], c["approval"]
        why: list[str] = []
        if c["state"] in ("approved", "waiting") and rose:
            ok, why = self.precheck(c["project"], new, c["max_hours"], c["idle_min"])
            if c["approval"] != "pre-approved" or not ok:
                state, approval = "requested", None
        with self.store.tx():
            self._set(
                cid,
                offer=json.dumps(asdict(offer)),
                rate_usd_h=new,
                quoted_at=self.now(),
                state=state,
                approval=approval,
                approved_by=c["approved_by"] if approval else None,
                approved_at=c["approved_at"] if approval else None,
            )
            self.store.audit(
                actor,
                "charter.price-rose" if state != c["state"] else "charter.requote",
                f"charter/{cid}",
                old_rate_usd_h=old,
                new_rate_usd_h=new,
                provider=offer.provider,
                region=offer.region,
                at_launch=at_launch,
                why_not=why,
            )
        return self.charter(cid)

    async def requote(self, charter_id: int, actor: str, kind: str, roles: list[str]) -> dict:
        """Quote a waiting (or approved, not yet launching) charter again, for its machine."""
        c = self._get(charter_id, ("requested", "approved", "waiting"))
        if c["requester"] != actor and not is_purser(kind, roles):
            raise CharterError(f"charter {charter_id} belongs to {c['requester']}")
        if charter_id in self._busy:
            raise CharterError(f"charter {charter_id} is launching")
        try:
            offers = await asyncio.to_thread(self.plugin(c["plugin"]).quote, c["spec"])
        except yachts.PluginError as e:
            raise CharterError(f"quote failed: {summarize_error(str(e))}") from None
        offer = self._same_offer(offers, c["offer"])
        if offer is None:
            raise CharterError(
                f"{c['offer']['provider']} {c['offer']['region']} no longer offers this machine;"
                " withdraw the charter and request it again"
            )
        return self._reprice(self.charter(charter_id), offer, actor, at_launch=False)

    async def _fresh_quote(self, c: dict) -> bool:
        """Re-quote just before launch. False when the rate rose and the charter went back to
        a purser. A quote that fails or no longer lists the machine lets the launch go ahead:
        plugins cap the price at the approved rate themselves (SkyPilot's max_hourly_cost)."""
        try:
            offers = await asyncio.to_thread(self.plugin(c["plugin"]).quote, c["spec"])
        except Exception as e:  # noqa: BLE001
            log.warning("charter %s: fresh quote failed: %s", c["id"], e)
            return True
        offer = self._same_offer(offers, c["offer"])
        if offer is None:
            return True
        return self._reprice(c, offer, "labtender", at_launch=True)["state"] != "requested"

    # ---------------------------------------------------- views

    def purser_view(self, now: float, market: dict[str, list] | None = None) -> dict:
        """What a purser decides on: waiting charters with their quote's age and the current
        market for their GPU, every budget, provider balances, and recent money events."""
        market = market or {}
        waiting = []
        for c in self.charters(("requested",)):
            _, why = self.precheck(c["project"], c["rate_usd_h"], c["max_hours"], c["idle_min"])
            gpu = (c["offer"] or {}).get("gpu")
            waiting.append(
                public(c)
                | {
                    "why_not": why,
                    "quoted_at": c["quoted_at"] or c["created_at"],
                    "market": market.get(gpu, []),
                }
            )
        history = [e for e in self.store.events(1000) if e["action"] in PURSER_ACTIONS]
        return {
            "time": now,
            "waiting": waiting,
            "budgets": [self.usage(p, now) for p in self.projects()],
            "defaults": dict(DEFAULT_LIMITS),
            "providers": self.providers(now),
            "history": history[:80],
        }

    def view(self, now: float | None = None) -> dict:
        now = now or self.now()
        rows = self.charters(since=now - DAY_S)
        return {
            "charters": [
                public(c) | {"progress": self._progress.get(c["id"])} for c in reversed(rows)
            ],
            "budgets": [self.usage(p, now) for p in self.projects()],
            "providers": self.providers(now),
        }


def is_purser(kind: str, roles: list[str]) -> bool:
    return kind == "person" and "purser" in roles


def retry_window_s(spec: dict) -> float:
    """How long after its first capacity failure a charter keeps trying to launch."""
    return float(spec.get("launch_retry_min", 0.0)) * 60


def retry_every_s(spec: dict) -> float:
    return float(spec.get("launch_retry_every_min", 10.0)) * 60


def land_lead(c: dict) -> float:
    """How long before its time limit a charter starts landing, so that landing and destroy fit
    inside ``max_hours`` and the actual cost stays within the reserved worst case. Never more
    than half the charter, so a short one is not landed as soon as it is live."""
    spec = c.get("spec") or {}
    lead = DESTROY_LEAD_S
    if spec.get("land") and not c.get("lost_at"):
        lead += float(spec.get("land_timeout_min", 15.0)) * 60
    return min(lead, c["max_hours"] * 3600 / 2)


def public(c: dict) -> dict:
    """A charter as anyone may read it: without its profile commands (they may name private
    paths) and with its error cut to one line, since a set-up step's output can name private
    paths and hosts or print its environment. The whole text is in the charter's log."""
    out = {k: v for k, v in c.items() if k != "spec"}
    out["accelerators"] = (c.get("spec") or {}).get("accelerators")
    if c.get("last_error"):
        out["last_error"] = summarize_error(c["last_error"])
    return out
