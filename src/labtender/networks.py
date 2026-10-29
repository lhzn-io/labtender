"""Networks a host can be reached through while away from the lab, for the roaming badge.

``$LABTENDER_HOME/networks.toml`` names them::

    [roaming]
    vpn = ["10.8.0.0/24"]           # a VPN that laptops dial in from
    hotspot = ["172.20.10.0/28"]

The probe reports the address the hub reached it at (the server side of its SSH connection).
A host reached at an address inside one of these networks is shown as roaming through it.
Without the file nothing is shown.
"""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path


def load(home: Path) -> dict[str, list]:
    """{name: [networks]} from networks.toml, or {} when there is none."""
    f = home / "networks.toml"
    if not f.exists():
        return {}
    doc = tomllib.loads(f.read_text(encoding="utf-8"))
    out = {}
    for name, nets in (doc.get("roaming") or {}).items():
        nets = [nets] if isinstance(nets, str) else nets
        out[name] = [ipaddress.ip_network(n, strict=False) for n in nets]
    return out


def roaming_via(addr: str | None, nets: dict[str, list]) -> str | None:
    """The name of the roaming network addr is in, or None."""
    if not addr or not nets:
        return None
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return None
    for name, members in nets.items():
        if any(ip in n for n in members):
            return name
    return None
