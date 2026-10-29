"""The MCP tools, end to end against a real hub on a local port."""

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn

from labtender.client import Hub
from labtender.db import Store
from labtender.mcp_server import build_server
from labtender.server import create_app, token_hash


@pytest.fixture
def hub_url(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_host("box", actor="admin")
    store.record_probe("box", {"gpus": [{"index": 0, "name": "Example"}], "gpu_procs": []}, [])
    for name in ("agent-a", "agent-b"):
        store.add_token(token_hash("t-" + name), name, "agent", actor="admin")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(store=store, home=tmp_path, collect=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def call(server, name, args=None):
    """Call a tool as a client would: structured result, or the error text raised."""
    result = asyncio.run(server.call_tool(name, args or {}))
    if result.is_error:
        raise RuntimeError(" ".join(getattr(c, "text", "") for c in result.content))
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(result.content[0].text)


def test_tools_listed_with_instructions(hub_url):
    server = build_server(Hub(hub_url, "t-agent-a"))
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "fleet_status",
        "resource",
        "claim",
        "heartbeat",
        "bind_run",
        "release",
        "my_claims",
        "charter_profiles",
        "charter_quote",
        "charter_request",
        "charter_status",
        "charter_end",
    }
    assert "Never take a GPU someone else holds" in server.instructions
    assert "cost real money" in server.instructions


def test_two_agents_queue_through_mcp(hub_url):
    a = build_server(Hub(hub_url, "t-agent-a"))
    b = build_server(Hub(hub_url, "t-agent-b"))
    ca = call(a, "claim", {"resource": "box/gpu0", "est_hours": 2, "note": "sweep"})
    assert ca["state"] == "held"
    cb = call(b, "claim", {"resource": "box/gpu0", "est_hours": 1, "note": "eval"})
    assert cb["state"] == "queued" and cb["queue_position"] == 1 and cb["expected_start"]

    fleet = call(b, "fleet_status")
    card = fleet["hosts"][0]["gpus"][0]
    assert card["holders"][0]["owner"] == "agent-a" and card["queue"][0]["owner"] == "agent-b"

    mine = call(b, "my_claims")
    assert mine["identity"] == "agent-b" and mine["claims"][0]["state"] == "queued"

    with pytest.raises(Exception, match="belongs to"):
        call(b, "release", {"claim_id": ca["id"]})
    assert call(a, "release", {"claim_id": ca["id"], "reason": "done"})["state"] == "released"
    assert call(b, "resource", {"resource": "box/gpu0"})["queue"][0]["state"] == "offered"
    assert call(b, "heartbeat", {"claim_id": cb["id"]})["state"] == "held"
