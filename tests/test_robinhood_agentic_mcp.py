"""Robinhood Agentic Trading MCP rail — client transport, adapter, and routing.

All deterministic: the MCP client's HTTP transport is injected as a fake, so no
network and no DB. Validates the sanctioned-rail foundation (design P0) ahead of
the operator's live OAuth + introspection step.
"""

from __future__ import annotations

import json

import pytest

from app.services.trading import execution_family_registry as efr
from app.services.trading.venue.rh_mcp_client import RhMcpClient, RhMcpError
from app.services.trading.venue.robinhood_mcp import RobinhoodAgenticMcpAdapter


# ── Fake MCP Streamable-HTTP transport ──────────────────────────────────────


class FakeTransport:
    """Records JSON-RPC requests; returns canned per-method responses.

    Signature matches RhMcpClient's http_post: (url, headers, body, timeout)
    -> (status_code, lower_cased_headers, text).
    """

    def __init__(
        self,
        *,
        tools=None,
        tool_pages=None,
        call_result=None,
        sse=False,
        status_overrides=None,
        session_id="sess-abc",
    ):
        self.tools = tools if tools is not None else [
            {"name": "place_equity_order", "description": "Place an equity order"},
            {"name": "list_orders", "description": "List your orders"},
            {"name": "get_account", "description": "Account balances + buying power"},
        ]
        self.tool_pages = tool_pages
        self._page_idx = 0
        self.call_result = call_result if call_result is not None else {
            "content": [{"type": "text", "text": json.dumps({"id": "ord-1", "status": "queued"})}],
            "isError": False,
        }
        self.sse = sse
        self.status_overrides = status_overrides or {}
        self.session_id = session_id
        self.requests: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        payload = json.loads(body)
        method = payload.get("method")
        rid = payload.get("id")
        self.requests.append({"method": method, "headers": dict(headers), "params": payload.get("params")})

        if method in self.status_overrides:
            return self.status_overrides[method], {}, "error-body"

        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "robinhood", "version": "1.0"},
                "capabilities": {"tools": {}},
            }
            return 200, {"mcp-session-id": self.session_id, "content-type": "application/json"}, self._json(rid, result)

        if method == "notifications/initialized":
            return 202, {}, ""

        if method == "tools/list":
            if self.tool_pages is not None:
                idx = self._page_idx
                page = self.tool_pages[idx]
                self._page_idx += 1
                result = {"tools": page}
                if idx < len(self.tool_pages) - 1:
                    result["nextCursor"] = f"cursor-{idx + 1}"
                return 200, {"content-type": "application/json"}, self._json(rid, result)
            return 200, {"content-type": "application/json"}, self._json(rid, {"tools": self.tools})

        if method == "tools/call":
            if self.sse:
                return 200, {"content-type": "text/event-stream"}, self._sse(rid, self.call_result)
            return 200, {"content-type": "application/json"}, self._json(rid, self.call_result)

        return 200, {"content-type": "application/json"}, self._json(rid, {})

    @staticmethod
    def _json(rid, result):
        return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})

    @staticmethod
    def _sse(rid, result):
        msg = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})
        return f"event: message\ndata: {msg}\n\n"


def _client(transport, token="tok-123"):
    return RhMcpClient(endpoint="https://example.test/mcp", token=token, http_post=transport)


# ── MCP client transport ────────────────────────────────────────────────────


def test_initialize_captures_session_and_sends_initialized():
    t = FakeTransport()
    c = _client(t)
    c.connect()
    methods = [r["method"] for r in t.requests]
    assert methods[:2] == ["initialize", "notifications/initialized"]
    # session id handed back at initialize is reused on the follow-up notification
    assert t.requests[1]["headers"].get("Mcp-Session-Id") == "sess-abc"
    assert c.server_info.get("name") == "robinhood"


def test_tools_list_follows_pagination():
    t = FakeTransport(tool_pages=[[{"name": "a"}], [{"name": "b"}, {"name": "c"}]])
    tools = _client(t).list_tools()
    assert [x["name"] for x in tools] == ["a", "b", "c"]


def test_call_tool_parses_structured_text():
    t = FakeTransport(call_result={
        "content": [{"type": "text", "text": json.dumps({"id": "ord-9", "status": "queued"})}],
        "isError": False,
    })
    res = _client(t).call_tool("place_equity_order", {"symbol": "AAPL"})
    assert res.is_error is False
    assert res.data()["id"] == "ord-9"


def test_call_tool_over_sse_transport():
    t = FakeTransport(sse=True, call_result={"content": [{"type": "text", "text": json.dumps({"ok": True})}]})
    res = _client(t).call_tool("x", {})
    assert res.data()["ok"] is True


def test_unauthorized_raises_typed_error():
    t = FakeTransport(status_overrides={"initialize": 401})
    with pytest.raises(RhMcpError) as ei:
        _client(t).connect()
    assert ei.value.code == "unauthorized"


def test_missing_token_raises_no_token():
    c = _client(FakeTransport())
    c.token = None  # force the no-token path deterministically
    with pytest.raises(RhMcpError) as ei:
        c.list_tools()
    assert ei.value.code == "no_token"


# ── Adapter ─────────────────────────────────────────────────────────────────


class _MDStub:
    """Minimal market-data adapter the MCP adapter should delegate quotes to."""

    def get_quote_price(self, product_id):
        return 12.34

    def get_best_bid_ask(self, product_id):
        return ("TICKER", "FRESH")

    def get_product(self, product_id):
        return (None, "FRESH")

    def get_products(self):
        return ([], "FRESH")

    def get_ticker(self, product_id):
        return ("TICKER", "FRESH")

    def get_recent_trades(self, product_id, *, limit=50):
        return ([], "FRESH")


def _adapter(transport=None, token="tok"):
    client = RhMcpClient(endpoint="https://example.test/mcp", token=token, http_post=transport or FakeTransport())
    return RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=_MDStub())


def test_adapter_enabled_with_token():
    assert _adapter().is_enabled() is True


def test_adapter_disabled_without_token(monkeypatch):
    monkeypatch.delenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN", raising=False)
    client = RhMcpClient(endpoint="https://example.test/mcp", token=None, http_post=FakeTransport())
    client.token = None
    a = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=_MDStub())
    assert a.is_enabled() is False


def test_adapter_delegates_market_data():
    assert _adapter().get_quote_price("AAPL") == 12.34


def test_adapter_capability_keyword_match():
    a = _adapter(transport=FakeTransport())
    assert a._resolve_tool("place_order") == "place_equity_order"
    assert a._resolve_tool("list_orders") == "list_orders"
    assert a._resolve_tool("account") == "get_account"


def test_adapter_tool_override_wins(monkeypatch):
    monkeypatch.setenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP", json.dumps({"place_order": "custom_submit"}))
    a = _adapter(transport=FakeTransport(tools=[{"name": "unrelated"}]))
    assert a._resolve_tool("place_order") == "custom_submit"


def test_place_market_order_builds_args_and_normalizes():
    t = FakeTransport(
        tools=[{"name": "place_equity_order"}],
        call_result={
            "content": [{"type": "text", "text": json.dumps({"id": "ord-77", "status": "queued", "symbol": "AAPL"})}],
            "isError": False,
        },
    )
    a = _adapter(transport=t)
    out = a.place_market_order(product_id="AAPL", side="buy", base_size="3", client_order_id="cid-1")
    assert out["ok"] is True
    assert out["order_id"] == "ord-77"
    assert out["client_order_id"] == "cid-1"
    call = [r for r in t.requests if r["method"] == "tools/call"][-1]
    args = call["params"]["arguments"]
    assert args == {"symbol": "AAPL", "side": "buy", "quantity": "3", "type": "market", "client_order_id": "cid-1"}


def test_place_market_order_unresolved_tool_fails_loud():
    a = _adapter(transport=FakeTransport(tools=[{"name": "unrelated_tool"}]))
    out = a.place_market_order(product_id="AAPL", side="buy", base_size="1")
    assert out["ok"] is False
    assert "no Robinhood Agentic MCP tool" in out["error"]


def test_list_open_orders_normalizes_and_filters():
    t = FakeTransport(
        tools=[{"name": "list_orders"}],
        call_result={
            "content": [{"type": "text", "text": json.dumps({"orders": [
                {"id": "o1", "symbol": "AAPL", "side": "buy", "status": "open",
                 "cumulative_quantity": 2, "average_price": 10.0},
                {"id": "o2", "symbol": "TSLA", "side": "sell", "status": "open"},
            ]})}],
            "isError": False,
        },
    )
    orders, _fresh = _adapter(transport=t).list_open_orders(product_id="AAPL")
    assert len(orders) == 1
    assert orders[0].order_id == "o1"
    assert orders[0].product_id == "AAPL"
    assert orders[0].filled_size == 2.0


# ── Registry routing ────────────────────────────────────────────────────────


def test_routing_default_equity_stays_robinhood_spot(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_equity_execution_rail", "robinhood_spot", raising=False)
    assert efr.resolve_execution_family_for_symbol("AAPL") == efr.EXECUTION_FAMILY_ROBINHOOD_SPOT


def test_routing_equity_uses_mcp_when_selected_and_token(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_equity_execution_rail", "robinhood_agentic_mcp", raising=False)
    monkeypatch.setenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN", "tok-xyz")
    assert efr.resolve_execution_family_for_symbol("AAPL") == efr.EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    # crypto is unaffected — still Coinbase
    assert efr.resolve_execution_family_for_symbol("BTC-USD") == efr.EXECUTION_FAMILY_COINBASE_SPOT


def test_routing_mcp_ignored_when_selected_but_no_token(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_equity_execution_rail", "robinhood_agentic_mcp", raising=False)
    monkeypatch.delenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN", raising=False)
    monkeypatch.setattr(settings, "chili_robinhood_agentic_mcp_token_file", "", raising=False)
    assert efr.resolve_execution_family_for_symbol("AAPL") == efr.EXECUTION_FAMILY_ROBINHOOD_SPOT


def test_factory_resolves_mcp_adapter():
    factory = efr.resolve_live_spot_adapter_factory(efr.EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP)
    assert factory().__class__.__name__ == "RobinhoodAgenticMcpAdapter"


def test_venue_for_mcp_family_is_robinhood():
    assert efr.venue_for_execution_family(efr.EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP) == "robinhood"
