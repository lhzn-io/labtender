"""A memorable name for each GPU, made from its UUID, so a card is known by the same name wherever
it is plugged in, and in any lab's labtender."""

import hashlib

ADJECTIVES = (
    "amber", "azure", "bold", "brisk", "bright", "calm", "cedar", "clear",
    "coral", "crisp", "deep", "eager", "early", "fair", "fleet", "gentle",
    "glad", "golden", "grand", "green", "hardy", "hazel", "honest", "jade",
    "keen", "kind", "lively", "lucid", "mellow", "merry", "misty", "noble",
    "north", "oaken", "olive", "quick", "quiet", "rapid", "rosy", "ruddy",
    "rustic", "sable", "salty", "sandy", "silver", "sleek", "snowy", "solid",
    "south", "spry", "steady", "stout", "sunny", "swift", "tawny", "tidal",
    "true", "vivid", "warm", "west", "windy", "wise", "young", "zesty",
)  # fmt: skip
NOUNS = (
    "albatross", "anchor", "auk", "beacon", "bluff", "breaker", "buoy", "cape",
    "cormorant", "cove", "crane", "current", "curlew", "dolphin", "dune", "eddy",
    "egret", "fathom", "fjord", "gannet", "grebe", "gull", "harbor", "heron",
    "inlet", "isle", "jetty", "kelp", "keel", "kittiwake", "lagoon", "lantern",
    "loon", "marlin", "narwhal", "orca", "osprey", "otter", "oyster", "pelican",
    "petrel", "pier", "plover", "puffin", "reef", "sandpiper", "schooner", "seal",
    "shoal", "skiff", "sloop", "spray", "squall", "starling", "swell", "tern",
    "walrus", "wave", "whale", "wharf", "willet", "wren", "yawl", "zephyr",
)  # fmt: skip


def card_name(uuid: str) -> str:
    """An adjective and a noun picked by the UUID's hash: one card, one pair, always."""
    h = hashlib.sha256(uuid.strip().lower().encode()).digest()
    return f"{ADJECTIVES[h[0] % len(ADJECTIVES)]}-{NOUNS[h[1] % len(NOUNS)]}"
