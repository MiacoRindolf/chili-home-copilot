"""Tests for the read-only MCP status / config-sanity endpoint handler.

GET /api/brain/mcp/status reports the MCP client config without establishing
live connections, and flags any allowlisted tool the safety denylist blocks.

The handler takes no request args, so it's tested directly (fast — no full-app
boot). Route registration on the `brain` router is already covered by the W1
client test on the same router.
"""
import json
from unittest.mock import patch

from app import mcp_client as mcpc
from app.routers.brain import mcp_status, mcp_tools


def _body(resp) -> dict:
    return json.loads(resp.body)


class TestMcpStatusEndpoint:
    def test_dormant_by_default(self):
        with patch.object(mcpc.settings, "mcp_servers_json", "", create=True), \
             patch.object(mcpc.settings, "mcp_enabled", False, create=True):
            data = _body(mcp_status())
        assert data["ok"] is True
        assert data["enabled"] is False
        assert data["configured_servers"] == 0
        assert data["servers"] == []

    def test_reports_configured_servers_without_urls(self):
        cfg = json.dumps([
            {"id": "sec", "name": "SEC EDGAR", "transport": "sse",
             "url": "https://secret-internal.example/mcp",
             "allowed_tools": ["search", "get_filing"]},
        ])
        with patch.object(mcpc.settings, "mcp_servers_json", cfg, create=True), \
             patch.object(mcpc.settings, "mcp_enabled", True, create=True):
            resp = mcp_status()
        data = _body(resp)
        assert data["enabled"] is True
        assert data["configured_servers"] == 1
        srv = data["servers"][0]
        assert srv["id"] == "sec"
        assert srv["transport"] == "sse"
        assert srv["allowed_tools"] == ["search", "get_filing"]
        # The URL (potentially sensitive) must never be echoed back.
        assert "url" not in srv
        assert "secret-internal" not in resp.body.decode("utf-8")
        assert srv["allowlist_blocked_by_denylist"] == []

    def test_config_sanity_flags_dangerous_allowlisted_tool(self):
        cfg = json.dumps([
            {"id": "broker", "name": "Broker", "transport": "sse", "url": "https://x",
             "allowed_tools": ["get_quote", "place_order"]},  # place_order is dangerous
        ])
        with patch.object(mcpc.settings, "mcp_servers_json", cfg, create=True):
            srv = _body(mcp_status())["servers"][0]
        assert "place_order" in srv["allowlist_blocked_by_denylist"]
        assert "get_quote" not in srv["allowlist_blocked_by_denylist"]

    def test_bad_json_config_is_empty(self):
        with patch.object(mcpc.settings, "mcp_servers_json", "{not json", create=True):
            data = _body(mcp_status())
        assert data["configured_servers"] == 0


class TestMcpToolsEndpoint:
    """GET /api/brain/mcp/tools — read-only list of policy-permitted tools."""

    def test_empty_when_nothing_connected(self):
        with patch.object(mcpc.mcp_client, "list_tools", return_value=[]):
            data = _body(mcp_tools())
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["tools"] == []

    def test_lists_connected_tools_without_input_schema(self):
        fake = [
            {"server_id": "sec", "server_name": "SEC", "name": "search",
             "qualified_name": "mcp__sec__search", "description": "Search filings",
             "input_schema": {"type": "object", "secret": "x"}},
        ]
        with patch.object(mcpc.mcp_client, "list_tools", return_value=fake):
            resp = mcp_tools()
        data = _body(resp)
        assert data["count"] == 1
        t = data["tools"][0]
        assert t["name"] == "search"
        assert t["qualified_name"] == "mcp__sec__search"
        assert t["description"] == "Search filings"
        # The raw input_schema is not echoed to the client.
        assert "input_schema" not in t
        assert "secret" not in resp.body.decode("utf-8")

    def test_tolerates_list_tools_failure(self):
        def boom():
            raise RuntimeError("not connected")
        with patch.object(mcpc.mcp_client, "list_tools", side_effect=boom):
            data = _body(mcp_tools())
        assert data["ok"] is True
        assert data["tools"] == []
