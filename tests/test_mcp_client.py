"""Tests for the external MCP client (salvaged/adapted from odysseus, MIT).

The load-bearing concern is the SAFETY GATE: CHILI must never let an external
MCP server place orders or move money. These tests pin the denylist + allowlist
policy and prove it is re-applied at call time, plus the config parsing, tool
listing, and dormant-by-default behavior. The `mcp` SDK is mocked — no live
server required.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import mcp_client as mc
from app.mcp_client import (
    MCPClient,
    is_dangerous_tool,
    tool_permitted,
    _load_server_configs,
)


# ---------------------------------------------------------------------------
# Safety gate — the part that protects real money
# ---------------------------------------------------------------------------

class TestDangerousToolDenylist:
    @pytest.mark.parametrize("name", [
        "place_order", "placeOrder", "submit_order", "cancel_order", "modify_order",
        "order", "trade", "execute_trade", "buy", "sell", "short_sell",
        "withdraw", "deposit", "transfer_funds", "wire", "payout", "pay",
        "send_money", "fund_account", "liquidate", "close_position", "open_position",
        "approve", "sign_transaction", "broadcast_tx",
    ])
    def test_dangerous_names_blocked(self, name):
        assert is_dangerous_tool(name) is True
        # And the full gate blocks it regardless of allowlist.
        assert tool_permitted({"allowed_tools": [name]}, name) is False

    @pytest.mark.parametrize("name", [
        "search", "get_filing", "list_news", "fetch_quote", "lookup_ticker",
        "get_company_facts", "read_document", "summarize",
    ])
    def test_benign_names_allowed(self, name):
        assert is_dangerous_tool(name) is False
        assert tool_permitted({}, name) is True  # empty allowlist => benign allowed


class TestAllowlist:
    def test_allowlist_blocks_unlisted(self):
        cfg = {"allowed_tools": ["search", "get_filing"]}
        assert tool_permitted(cfg, "search") is True
        assert tool_permitted(cfg, "delete_everything") is False

    def test_empty_allowlist_allows_all_benign(self):
        assert tool_permitted({"allowed_tools": []}, "anything_benign") is True

    def test_dangerous_blocked_even_if_allowlisted(self):
        # The denylist wins over the allowlist — defense in depth.
        cfg = {"allowed_tools": ["place_order"]}
        assert tool_permitted(cfg, "place_order") is False

    def test_empty_name_rejected(self):
        assert tool_permitted({}, "") is False


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestLoadServerConfigs:
    def _with_json(self, value):
        return patch.object(mc.settings, "mcp_servers_json", value, create=True)

    def test_empty_returns_empty(self):
        with self._with_json(""):
            assert _load_server_configs() == []

    def test_invalid_json_returns_empty(self):
        with self._with_json("{not json"):
            assert _load_server_configs() == []

    def test_non_array_returns_empty(self):
        with self._with_json('{"id": "x"}'):
            assert _load_server_configs() == []

    def test_valid_entries_parsed(self):
        cfg = json.dumps([
            {"id": "sec", "transport": "sse", "url": "https://x"},
            {"id": "docs", "transport": "stdio", "command": "python", "args": ["s.py"]},
        ])
        with self._with_json(cfg):
            out = _load_server_configs()
        assert {e["id"] for e in out} == {"sec", "docs"}

    def test_bad_entries_dropped(self):
        cfg = json.dumps([
            {"id": "", "transport": "sse"},                 # no id
            {"id": "bad__id", "transport": "sse"},          # id breaks naming scheme
            {"id": "ok", "transport": "carrier-pigeon"},    # bad transport
            {"id": "dup", "transport": "sse", "url": "u"},
            {"id": "dup", "transport": "sse", "url": "u2"}, # duplicate id
            "not-a-dict",
        ])
        with self._with_json(cfg):
            out = _load_server_configs()
        assert [e["id"] for e in out] == ["dup"]  # only the first valid, unique one


# ---------------------------------------------------------------------------
# Client behavior (mocked SDK)
# ---------------------------------------------------------------------------

def _fake_tool(name, desc="", schema=None):
    t = MagicMock()
    t.name = name
    t.description = desc
    t.inputSchema = schema or {}
    return t


def _fake_session(tools):
    session = MagicMock()
    session.initialize = AsyncMock()
    lt = MagicMock()
    lt.tools = tools
    session.list_tools = AsyncMock(return_value=lt)
    return session


class TestClientConnectAndFilter:
    def test_finalize_filters_dangerous_tools(self):
        client = MCPClient()
        cfg = {"id": "sec", "name": "SEC", "transport": "sse"}
        session = _fake_session([
            _fake_tool("search"), _fake_tool("get_filing"),
            _fake_tool("place_order"),  # must be filtered out
        ])
        stack = MagicMock()
        ok = asyncio.run(client._finalize_session("sec", cfg, session, stack))
        assert ok is True
        names = {t["name"] for t in client._tools["sec"]}
        assert names == {"search", "get_filing"}
        assert client._status["sec"]["tool_count"] == 2
        assert client._status["sec"]["blocked_count"] == 1

    def test_list_tools_qualified_names(self):
        client = MCPClient()
        cfg = {"id": "sec", "name": "SEC", "transport": "sse"}
        session = _fake_session([_fake_tool("search", "find filings")])
        asyncio.run(client._finalize_session("sec", cfg, session, MagicMock()))
        tools = client.list_tools()
        assert tools[0]["qualified_name"] == "mcp__sec__search"
        assert tools[0]["server_name"] == "SEC"


class TestCallTool:
    def _connected_client(self, tool_names, call_result=None):
        client = MCPClient()
        cfg = {"id": "sec", "name": "SEC", "transport": "sse", "allowed_tools": []}
        session = _fake_session([_fake_tool(n) for n in tool_names])
        session.call_tool = AsyncMock(return_value=call_result)
        asyncio.run(client._finalize_session("sec", cfg, session, MagicMock()))
        return client, session

    def test_unknown_tool(self):
        client = MCPClient()
        res = asyncio.run(client.call_tool("mcp__nope__x"))
        assert res["ok"] is False and "unknown" in res["error"]

    def test_successful_call_normalizes_output(self):
        content = MagicMock()
        content.text = "10-K filing text"
        result = MagicMock()
        result.content = [content]
        result.isError = False
        client, _ = self._connected_client(["search"], call_result=result)
        res = asyncio.run(client.call_tool("mcp__sec__search", {"q": "AAPL"}))
        assert res["ok"] is True
        assert "10-K filing text" in res["output"]

    def test_call_time_policy_reblocks_dangerous(self):
        # Even if a dangerous tool somehow sat in the registry, the call-time
        # gate must refuse it. Inject one directly to simulate the worst case.
        client, session = self._connected_client(["search"])
        client._tools["sec"].append({"name": "place_order", "description": "", "input_schema": {}})
        res = asyncio.run(client.call_tool("mcp__sec__place_order", {"qty": 100}))
        assert res["ok"] is False
        assert "blocked by policy" in res["error"]
        session.call_tool.assert_not_called()  # never reached the server

    def test_error_result_surfaced(self):
        content = MagicMock()
        content.text = "boom"
        result = MagicMock()
        result.content = [content]
        result.isError = True
        client, _ = self._connected_client(["search"], call_result=result)
        res = asyncio.run(client.call_tool("mcp__sec__search"))
        assert res["ok"] is False
        assert "boom" in res["error"]


class TestDormantByDefault:
    def test_connect_all_noop_when_disabled(self):
        client = MCPClient()
        with patch.object(mc.settings, "mcp_enabled", False, create=True):
            assert asyncio.run(client.connect_all()) == 0

    def test_enabled_requires_flag_and_sdk(self):
        client = MCPClient()
        with patch.object(mc.settings, "mcp_enabled", True, create=True), \
             patch.object(mc, "_HAS_MCP", True):
            assert client.enabled() is True
        with patch.object(mc.settings, "mcp_enabled", True, create=True), \
             patch.object(mc, "_HAS_MCP", False):
            assert client.enabled() is False
