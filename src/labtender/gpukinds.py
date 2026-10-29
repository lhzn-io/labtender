"""What kind of GPU a card is, and its memory bandwidth, for grouping and ranking cards.

Memory size alone ranks GPUs badly: a system-on-chip whose GPU shares the host's memory reports
all of it, so a 128 GB integrated GPU would outrank a 16 GB card several times faster. Cards are
grouped first (discrete, integrated, Apple) and ranked within a group by memory bandwidth, the
figure that bounds most simulation and training work, taken from the published specifications
below. A card the table does not know can be given its figure with ``labtender card bandwidth``.
"""

GROUPS = (
    ("discrete", "Discrete GPUs"),
    ("integrated", "Integrated GPUs (memory shared with the host)"),
    ("apple", "Apple GPUs"),
)

# Published peak memory bandwidth in GB/s, matched as a lower-case substring of the model name
# (the longest match wins). Laptop parts differ from the desktop ones of the same name and are
# matched only by entries that say "laptop".
BANDWIDTH_GBS = {
    # GeForce, desktop
    "rtx 5090": 1792,
    "rtx 5080": 960,
    "rtx 5070 ti": 896,
    "rtx 5070": 672,
    "rtx 4090": 1008,
    "rtx 4080 super": 736,
    "rtx 4080": 717,
    "rtx 4070 ti super": 672,
    "rtx 4070": 504,
    "rtx 3090 ti": 1008,
    "rtx 3090": 936,
    "rtx 3080": 760,
    # Workstation and data centre
    "rtx pro 6000": 1792,
    "rtx 6000 ada": 960,
    "rtx a6000": 768,
    "l40s": 864,
    "l40": 864,
    "l4": 300,
    "a10g": 600,
    "a10": 600,
    "t4": 320,
    "v100": 900,
    "a100-sxm4-80gb": 2039,
    "a100 80gb pcie": 1935,
    "a100": 1555,
    "h100 80gb hbm3": 3350,
    "h100 nvl": 3900,
    "h100 pcie": 2000,
    "h200": 4800,
    "b200": 8000,
    # Integrated
    "thor": 273,
    "agx orin": 204.8,
    "orin nx": 102.4,
    "gb10": 273,
    # Apple (the full chip; cut-down versions of some have less)
    "m4 max": 546,
    "m4 pro": 273,
    "m3 ultra": 819,
    "m3 max": 400,
    "m2 ultra": 800,
    "m2 max": 400,
    "m1 ultra": 800,
    "m1 max": 400,
}
_BY_LENGTH = sorted(BANDWIDTH_GBS, key=len, reverse=True)
_MOBILE = ("laptop", "mobile", "max-q")


def kind(model: str | None, unified: bool | None) -> str:
    """discrete, integrated (an NVIDIA or other GPU sharing the host's memory) or apple."""
    m = (model or "").lower()
    if m.startswith("apple") or " apple " in f" {m} ":
        return "apple"
    return "integrated" if unified else "discrete"


def bandwidth_gbs(model: str | None) -> float | None:
    """Peak memory bandwidth from the table, or None for a model it does not know."""
    m = (model or "").lower()
    mobile = any(w in m for w in _MOBILE)
    for key in _BY_LENGTH:
        if key in m and (not mobile or any(w in key for w in _MOBILE)):
            return float(BANDWIDTH_GBS[key])
    return None


def rank_key(g: dict) -> tuple:
    """Sort key: group order, then bandwidth (unknown last), then memory, then place."""
    order = [k for k, _ in GROUPS]
    bw = g.get("bandwidth_gbs")
    return (
        order.index(g["kind"]) if g.get("kind") in order else len(order),
        -(bw or 0),
        -(g.get("mem_total_mib") or 0),
        g.get("host") or "",
        g.get("index") or 0,
    )
