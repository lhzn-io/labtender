"""Prices: the watch list, sampling, trends, the refresh limit and the run rate."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from labtender.charter import CharterError, Charters
from labtender.db import Store
from labtender.ledger import Ledger
from labtender.prices import Prices
from labtender.server import create_app, token_hash
from labtender.yachts.fake import FakePlugin


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def setup(tmp_path, watch=""):
    (tmp_path / "charters").mkdir()
    (tmp_path / "charters" / "l4.toml").write_text(
        'name = "l4"\nplugin = "fake"\naccelerators = "L4:1"\nfake_rate_usd_h = 0.5\n'
    )
    if watch:
        (tmp_path / "prices.toml").write_text(watch)
    store = Store(tmp_path / "t.sqlite")
    ledger = Ledger(store)
    clock = Clock()
    ch = Charters(store, ledger, tmp_path, clock=clock, plugins={"fake": FakePlugin()})
    return store, ledger, ch, Prices(store, ch, tmp_path, clock=clock), clock


def test_watchlist_joins_profiles_and_prices_toml(tmp_path):
    *_, prices, _ = setup(tmp_path, 'plugin = "fake"\ngpus = ["H100:1", "L4:1"]\n')
    assert sorted(w["accelerators"] for w in prices.watchlist()) == ["H100:1", "L4:1"]


def test_sampling_keeps_cheapest_per_region_and_builds_a_trend(tmp_path):
    store, ledger, ch, prices, clock = setup(tmp_path)
    assert asyncio.run(prices.sample()) == 2  # the fake plugin offers two regions
    clock.t += 7 * 3600
    asyncio.run(prices.sample())
    v = prices.view()
    (g,) = v["gpus"]
    (p,) = g["providers"]
    assert p["cheapest"]["region"] == "here" and p["cheapest"]["rate_usd_h"] == 0.5
    assert p["regions"] == 2 and p["launchable"] is True
    assert [pt[1] for pt in p["trend"]] == [0.5, 0.5]
    assert v["sampled_at"] == clock.t


def test_refresh_is_rate_limited(tmp_path):
    *_, prices, clock = setup(tmp_path)
    asyncio.run(prices.refresh())
    with pytest.raises(CharterError):
        asyncio.run(prices.refresh())
    clock.t += 601
    assert asyncio.run(prices.refresh())["sampled"] == 2


def test_run_rate_counts_live_yachts(tmp_path):
    store, ledger, ch, prices, clock = setup(tmp_path)
    assert prices.run_rate()["rate_usd_h"] == 0
    q = asyncio.run(ch.quote("l4"))
    ch.request("l4", q["offers"][0], "agent", "agent", ["operator"])
    asyncio.run(ch.step(wait=True))
    r = prices.run_rate()
    assert r["rate_usd_h"] == 0.5 and r["yachts"] == 1
    # The worst case adds at most the yacht's own time limit (4 h at $0.50/h).
    assert r["month_spent_usd"] <= r["month_worst_case_usd"] <= r["month_spent_usd"] + 2.0 + 1e-6


def test_prices_api_and_page(tmp_path):
    store, ledger, ch, prices, clock = setup(tmp_path)
    store.add_token(token_hash("agent"), "agent", "agent", actor="a")
    app = create_app(
        store=store, home=tmp_path, collect=False, ledger=ledger, charters=ch, prices=prices
    )
    with TestClient(app) as http:
        assert http.post("/api/prices/refresh").status_code == 401
        r = http.post("/api/prices/refresh", headers={"Authorization": "Bearer agent"})
        assert r.status_code == 200 and r.json()["sampled"] == 2
        again = http.post("/api/prices/refresh", headers={"Authorization": "Bearer agent"})
        assert again.status_code == 429
        v = http.get("/api/prices").json()
        assert v["gpus"][0]["gpu"] == "L4:1" and "run_rate" in v
        assert "run_rate" in http.get("/api/state").json()
        assert "labtender prices" in http.get("/prices").text
