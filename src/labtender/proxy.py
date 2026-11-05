"""Sign-in through an authenticating proxy: the proxy signs people in, labtender maps them.

A proxy such as oauth2-proxy (behind Caddy's ``forward_auth``, or on its own) handles the
identity provider and HTTPS, and passes the signed-in user to labtender in a header. labtender
believes that header only from the proxy's own addresses, and only alongside a shared secret the
proxy adds to every request; it maps the user to a labtender identity and roles through an
allow-list, and never lets it reach an agent identity or override a bearer token. Configured by
``$LABTENDER_HOME/proxy.toml``; without that file nothing changes.

    header = "X-Auth-Request-Email"   # what the proxy sets
    trusted = ["127.0.0.1", "::1"]    # the proxy's addresses as labtender sees them
    secret = "..."                    # the proxy sends it as X-Labtender-Proxy-Secret

The address alone is not enough: any other process on the proxy's machine connects from the same
address, and so does a web page that rebinds its own name to 127.0.0.1. Neither knows the secret.

    [[allow]]
    user = "dana@example.org"          # the header's value
    identity = "dana"                  # the labtender identity
    roles = ["viewer", "operator", "admin", "purser"]   # default: a person's defaults
"""

from __future__ import annotations

import hmac
import ipaddress
import tomllib
from pathlib import Path

from .db import DEFAULT_ROLES, ROLES, Store

# Changes made with the proxy's identity must carry this header. A page on another site can
# make the browser send a form or a simple request, with the proxy's cookie, but cannot add a
# custom header without CORS, which labtender never grants.
WRITE_HEADER = "X-Requested-With"
WRITE_VALUE = "labtender"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SECRET_HEADER = "X-Labtender-Proxy-Secret"
MIN_SECRET = 32


class ProxyError(ValueError):
    pass


class Proxy:
    def __init__(
        self,
        header: str,
        trusted: list[str],
        allow: dict[str, dict],
        secret: str,
        secret_header: str = SECRET_HEADER,
    ):
        self.header = header
        self.trusted = [ipaddress.ip_network(t, strict=False) for t in trusted]
        self.allow = allow  # lower-cased user -> {"identity", "kind", "roles"}
        self.secret = secret
        self.secret_header = secret_header

    def trusts(self, peer: str | None, secret: str | None = None) -> bool:
        """Whether a request comes from the proxy: from one of its addresses, with its secret."""
        try:
            addr = ipaddress.ip_address(peer or "")
        except ValueError:
            return False
        if not any(addr in net for net in self.trusted):
            return False
        return hmac.compare_digest((secret or "").encode(), self.secret.encode())

    def identity(self, user: str) -> dict | None:
        return self.allow.get(user.strip().lower())


def load(home: Path, store: Store) -> Proxy | None:
    """Read ``proxy.toml``, or None when there is none. A bad file stops the service."""
    path = home / "proxy.toml"
    if not path.exists():
        return None
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProxyError(f"{path}: {e}") from None
    header = str(doc.get("header") or "").strip()
    if not header or header.lower() == "authorization":
        raise ProxyError(
            f"{path}: header names the header the proxy sets, such as X-Auth-Request-Email"
        )
    secret = str(doc.get("secret") or "")
    if len(secret) < MIN_SECRET:
        raise ProxyError(
            f"{path}: secret must be at least {MIN_SECRET} characters, which the proxy sends as"
            f" {SECRET_HEADER}; make one with: python3 -c"
            ' "import secrets; print(secrets.token_urlsafe(32))"'
        )
    secret_header = str(doc.get("secret_header") or SECRET_HEADER).strip()
    if secret_header.lower() in ("authorization", header.lower()):
        raise ProxyError(f"{path}: secret_header must differ from header and Authorization")
    trusted = doc.get("trusted", ["127.0.0.1", "::1"])
    try:
        [ipaddress.ip_network(t, strict=False) for t in trusted]
    except ValueError as e:
        raise ProxyError(f"{path}: trusted: {e}") from None
    agents = store.agent_identities()
    allow = {}
    for entry in doc.get("allow", []):
        user, ident = str(entry.get("user", "")).strip().lower(), str(entry.get("identity", ""))
        if not user or not ident:
            raise ProxyError(f"{path}: each allow entry needs a user and an identity")
        if ident in agents:
            raise ProxyError(
                f"{path}: {ident} is an agent identity; the proxy signs in people only"
            )
        roles = list(dict.fromkeys(entry.get("roles") or DEFAULT_ROLES["person"]))
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ProxyError(f"{path}: unknown role {bad[0]}; roles are {', '.join(ROLES)}")
        allow[user] = {"identity": ident, "kind": "person", "roles": roles}
    return Proxy(header, trusted, allow, secret, secret_header)
