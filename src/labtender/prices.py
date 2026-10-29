"""Prices: what the GPUs on the watch list cost now, how that has moved, and the run rate.

The service samples quotes from the yacht plugins every few hours and keeps the cheapest
offer per GPU, provider and region, so the board can show current prices and a trend without
asking a cloud on every page load. Quotes never spend money.

The watch list is every GPU a charter profile names, plus ``$LABTENDER_HOME/prices.toml``::

    plugin = "skypilot"
    gpus = ["L4:1", "RTXPRO6000:1", "H100:1", "H200:1"]
    clouds = ["gcp", "runpod", "lambda"]   # optional

The run rate is what live yachts cost per hour right now, with the month projected at it.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
import tomllib
from pathlib import Path

from .charter import ACTIVE, CharterError, Charters

log = logging.getLogger(__name__)

SAMPLE_S = 7 * 3600  # matches the refresh of the price catalog the SkyPilot plugin reads
REFRESH_MIN_S = 600  # a manual refresh at most every 10 minutes
KEEP_S = 90 * 24 * 3600
HISTORY_S = 30 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_sample (
    t              REAL NOT NULL,
    plugin         TEXT NOT NULL,
    gpu            TEXT NOT NULL,
    provider       TEXT NOT NULL,
    region         TEXT NOT NULL,
    instance_type  TEXT NOT NULL DEFAULT '',
    rate_usd_h     REAL NOT NULL,
    launchable     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS price_sample_gpu ON price_sample (gpu, t);
"""


def load_watch(home: Path) -> dict:
    f = home / "prices.toml"
    return tomllib.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


class Prices:
    def __init__(self, store, charters: Charters, home: Path, clock=time.time):
        self.store = store
        self.conn = store.conn
        self.charters = charters
        self.home = home
        self.now = clock
        self.conn.executescript(SCHEMA)
        self._stop: asyncio.Event | None = None
        self._sampling = False

    # ---------------------------------------------------- what to watch

    def watchlist(self) -> list[dict]:
        """One quote spec per GPU: from the charter profiles, then prices.toml."""
        specs: dict[tuple[str, str], dict] = {}
        for p in self.charters.profiles().values():
            if p.get("accelerators"):
                key = (p["plugin"], p["accelerators"])
                spec = {"plugin": p["plugin"], "accelerators": p["accelerators"]}
                if p["plugin"] == "fake":
                    spec["fake_rate_usd_h"] = p.get("fake_rate_usd_h", 0.5)
                if p.get("clouds"):
                    spec["clouds"] = p["clouds"]
                specs.setdefault(key, spec)
        watch = load_watch(self.home)
        plugin = watch.get("plugin", "skypilot")
        for gpu in watch.get("gpus", []):
            spec = {"plugin": plugin, "accelerators": gpu}
            if watch.get("clouds"):
                spec["clouds"] = watch["clouds"]
            specs[(plugin, gpu)] = spec
        return list(specs.values())

    # ---------------------------------------------------- sampling

    def last_sample_at(self) -> float | None:
        r = self.conn.execute("SELECT MAX(t) AS t FROM price_sample").fetchone()
        return r["t"] if r and r["t"] else None

    async def sample(self) -> int:
        """Quote every watched GPU and keep the cheapest offer per provider and region."""
        if self._sampling:
            return 0
        self._sampling = True
        try:
            now = self.now()
            rows = []
            for spec in self.watchlist():
                try:
                    plugin = self.charters.plugin(spec["plugin"])
                    offers = await asyncio.to_thread(plugin.quote, spec)
                except Exception as e:  # noqa: BLE001  (one GPU must not stop the rest)
                    log.warning("price sample %s: %s", spec["accelerators"], e)
                    continue
                best: dict[tuple[str, str], object] = {}
                for o in offers:
                    k = (o.provider, o.region)
                    if k not in best or o.rate_usd_h < best[k].rate_usd_h:
                        best[k] = o
                for o in best.values():
                    rows.append(
                        (
                            now,
                            spec["plugin"],
                            spec["accelerators"],
                            o.provider,
                            o.region,
                            o.detail.get("instance_type", ""),
                            o.rate_usd_h,
                            0 if o.detail.get("enabled") is False else 1,
                        )
                    )
            with self.store.tx():
                self.conn.executemany(
                    "INSERT INTO price_sample (t, plugin, gpu, provider, region, instance_type,"
                    " rate_usd_h, launchable) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                self.conn.execute("DELETE FROM price_sample WHERE t < ?", (now - KEEP_S,))
            return len(rows)
        finally:
            self._sampling = False

    async def refresh(self) -> dict:
        last = self.last_sample_at()
        if last and self.now() - last < REFRESH_MIN_S:
            raise CharterError(
                f"prices were sampled {int(self.now() - last)} s ago; refresh at most every"
                f" {REFRESH_MIN_S // 60} min"
            )
        n = await self.sample()
        return {"sampled": n, "at": self.now()}

    async def run(self) -> None:
        self._stop = asyncio.Event()
        while not self._stop.is_set():
            last = self.last_sample_at()
            if last is None or self.now() - last >= SAMPLE_S:
                try:
                    await self.sample()
                except Exception:  # noqa: BLE001  (sampling must never stop the service)
                    log.exception("price sample failed")
            try:
                await asyncio.wait_for(self._stop.wait(), 300)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        if self._stop:
            self._stop.set()

    # ---------------------------------------------------- views

    def run_rate(self, now: float | None = None) -> dict:
        """What live yachts cost per hour now, and the month projected at that rate."""
        now = now or self.now()
        live = [c for c in self.charters.charters(ACTIVE) if c["launch_at"]]
        rate = sum(c["rate_usd_h"] for c in live)
        t = time.localtime(now)
        days = calendar.monthrange(t.tm_year, t.tm_mon)[1]
        month_end = time.mktime((t.tm_year, t.tm_mon, days, 23, 59, 59, 0, 0, -1))
        spent = sum(
            self.charters.usage(p, now)["month_actual_usd"] for p in self.charters.projects()
        )
        # The most the live yachts can still add this month: each runs, at worst, to its own
        # time limit (or month end, if sooner). The reaper usually ends them well before.
        still = sum(
            c["rate_usd_h"] * max(0.0, min(c["ends_by"] or now, month_end) - now) / 3600
            for c in live
        )
        return {
            "rate_usd_h": round(rate, 4),
            "yachts": len(live),
            "month_spent_usd": round(spent, 2),
            "month_worst_case_usd": round(spent + still, 2),
        }

    def view(self, now: float | None = None) -> dict:
        now = now or self.now()
        last = self.last_sample_at()
        gpus = []
        for spec in self.watchlist():
            gpu = spec["accelerators"]
            latest = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT provider, region, instance_type, rate_usd_h, launchable"
                    " FROM price_sample WHERE gpu = ? AND plugin = ? AND t = ("
                    "SELECT MAX(t) FROM price_sample WHERE gpu = ? AND plugin = ?)"
                    " ORDER BY launchable DESC, rate_usd_h",
                    (gpu, spec["plugin"], gpu, spec["plugin"]),
                )
            ]
            # Cheapest per provider at each sample: one trend line per provider.
            trend: dict[str, list] = {}
            for r in self.conn.execute(
                "SELECT t, provider, MIN(rate_usd_h) AS rate FROM price_sample"
                " WHERE gpu = ? AND plugin = ? AND t >= ? GROUP BY t, provider ORDER BY t",
                (gpu, spec["plugin"], now - HISTORY_S),
            ):
                trend.setdefault(r["provider"], []).append([r["t"], r["rate"]])
            providers = []
            for prov in sorted({r["provider"] for r in latest}):
                mine = [r for r in latest if r["provider"] == prov]
                providers.append(
                    {
                        "provider": prov,
                        "cheapest": mine[0],
                        "regions": len(mine),
                        "launchable": any(r["launchable"] for r in mine),
                        "trend": trend.get(prov, []),
                    }
                )
            providers.sort(key=lambda p: (not p["launchable"], p["cheapest"]["rate_usd_h"]))
            gpus.append({"gpu": gpu, "plugin": spec["plugin"], "providers": providers})
        return {"time": now, "sampled_at": last, "run_rate": self.run_rate(now), "gpus": gpus}
