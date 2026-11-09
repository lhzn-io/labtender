"""A small standard-library HTTP client for the labtender service, used by the CLI and preflight.

The service's address comes from ``LABTENDER_URL`` (default ``http://127.0.0.1:8795``) and the
identity from the bearer token in ``LABTENDER_TOKEN``.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8795"


class HubError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class Hub:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 10):
        self.url = (url or os.environ.get("LABTENDER_URL") or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("LABTENDER_TOKEN")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            # The status has arrived; if the error body is cut short or the connection drops
            # while reading it, report the status with its reason phrase.
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except (ValueError, OSError, http.client.HTTPException):
                detail = e.reason
            raise HubError(e.code, str(detail)) from None
        except (urllib.error.URLError, OSError) as e:
            raise HubError(None, f"labtender service unreachable at {self.url}: {e}") from None

    def get(self, path: str, **params) -> object:
        q = {k: v for k, v in params.items() if v is not None}
        return self._call("GET", path + ("?" + urllib.parse.urlencode(q) if q else ""))

    def post(self, path: str, body: dict | None = None) -> object:
        # No body for endpoints that take none: a body the server never reads can be left in
        # its socket when it refuses early and closes, and the close then resets the
        # connection, sometimes before the response is read.
        return self._call("POST", path, body)

    # ------------------------------------------------------------ ledger

    def whoami(self) -> dict:
        return self.get("/api/whoami")

    def resource(self, resource: str) -> dict:
        return self.get("/api/resources/" + urllib.parse.quote(resource))

    def claims(
        self, resource: str | None = None, owner: str | None = None, all: bool = False
    ) -> list[dict]:
        return self.get("/api/claims", resource=resource, owner=owner, all="true" if all else None)

    def claim(self, resource: str, **kw) -> dict:
        return self.post("/api/claims", {"resource": resource, **kw})

    def heartbeat(
        self, claim_id: int, est_hours: float | None = None, lease_min: float | None = None
    ) -> dict:
        return self.post(
            f"/api/claims/{claim_id}/heartbeat", {"est_hours": est_hours, "lease_min": lease_min}
        )

    def release(self, claim_id: int, reason: str = "", force: bool = False) -> dict:
        return self.post(f"/api/claims/{claim_id}/release", {"reason": reason, "force": force})

    def bind(self, claim_id: int, run_dir: str) -> dict:
        return self.post(f"/api/claims/{claim_id}/bind", {"run_dir": run_dir})

    # ------------------------------------------------------------ charters

    def charter_profiles(self) -> list[dict]:
        return self.get("/api/charters/profiles")

    def charter_quote(self, profile: str, **kw) -> dict:
        return self.get("/api/charters/quote", profile=profile, **kw)

    def charters(self, all: bool = False) -> list[dict]:
        return self.get("/api/charters", all="true" if all else None)

    def charter(self, charter_id: int) -> dict:
        return self.get(f"/api/charters/{charter_id}")

    def charter_request(self, profile: str, **kw) -> dict:
        return self.post("/api/charters", {"profile": profile, **kw})

    def charter_approve(self, charter_id: int) -> dict:
        return self.post(f"/api/charters/{charter_id}/approve")

    def charter_reject(self, charter_id: int, reason: str = "") -> dict:
        return self.post(f"/api/charters/{charter_id}/reject", {"reason": reason})

    def charter_log(self, charter_id: int, limit: int = 8000) -> dict:
        return self.get(f"/api/charters/{charter_id}/log?limit={limit}")

    def charter_requote(self, charter_id: int) -> dict:
        return self.post(f"/api/charters/{charter_id}/requote")

    def charter_end(self, charter_id: int, reason: str = "") -> dict:
        return self.post(f"/api/charters/{charter_id}/end", {"reason": reason})

    def budgets(self) -> list[dict]:
        return self.get("/api/budgets")

    def set_budget(self, project: str, **limits) -> dict:
        return self._call("PUT", f"/api/budgets/{urllib.parse.quote(project)}", limits)

    def delete_budget(self, project: str) -> dict:
        return self._call("DELETE", f"/api/budgets/{urllib.parse.quote(project)}")
