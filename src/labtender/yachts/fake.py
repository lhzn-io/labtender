"""A yacht plugin with no cloud, for tests, demos and practice.

Its "yachts" are the labtender service's own machine, probed locally, and its prices come from
the profile (``fake_rate_usd_h``, default 0.50). Nothing is created and nothing is billed.
Setting ``fail_create`` or ``fail_destroy`` on an instance makes those calls fail, to rehearse
the failure paths.
"""

from __future__ import annotations

from . import Offer, Plugin, PluginError, Yacht


class FakePlugin(Plugin):
    name = "fake"

    def __init__(self) -> None:
        self.machines: dict[str, dict] = {}
        self.fail_create = False
        self.fail_destroy = False
        self.balance_usd: float | None = None
        self.price_factor = 1.0  # moves every quote, to rehearse prices changing

    def quote(self, spec: dict) -> list[Offer]:
        rate = round(float(spec.get("fake_rate_usd_h", 0.5)) * self.price_factor, 4)
        gpu = spec.get("accelerators") or "none"
        return [
            Offer("fake", "here", gpu, rate),
            Offer("fake", "elsewhere", gpu, round(rate * 1.4, 4)),
        ]

    def create(self, ref: str, spec: dict, offer: Offer, max_hours: float) -> Yacht:
        if self.fail_create:
            # A real provider can fail after the machine exists, so record it first.
            self.machines[ref] = {"status": "up", "max_hours": max_hours}
            raise PluginError("fake create failure")
        self.machines[ref] = {"status": "up", "max_hours": max_hours}
        return Yacht(ref, None, "local", offer.provider, offer.region)

    def destroy(self, ref: str) -> None:
        if self.fail_destroy:
            raise PluginError("fake destroy failure")
        self.machines.pop(ref, None)

    def status(self, ref: str) -> str:
        return self.machines.get(ref, {}).get("status", "gone")

    def clusters(self) -> list[str]:
        return list(self.machines)

    def balances(self) -> dict[str, dict]:
        # None means the fake provider has no prepaid balance, like most clouds.
        return {} if self.balance_usd is None else {"fake": {"balance_usd": self.balance_usd}}
