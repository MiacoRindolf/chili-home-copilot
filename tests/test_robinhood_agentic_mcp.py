"""Robinhood Agentic Trading MCP rail — client transport, adapter, and routing.

All deterministic: the MCP client's HTTP transport is injected as a fake, so no
network and no DB. Validates the sanctioned-rail foundation (design P0) ahead of
the operator's live OAuth + introspection step.
"""

from __future__ import annotations

import json
import time

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

from app.services.trading import execution_family_registry as efr
from app.services.trading.venue.rh_mcp_client import RhMcpClient, RhMcpError
from app.services.trading.venue.robinhood_mcp import (
    RobinhoodAgenticMcpAdapter,
    _reset_tool_catalog_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _clear_tool_catalog_cache():
    _reset_tool_catalog_cache_for_tests()
    efr._RH_AGENTIC_MCP_ADAPTER_CACHE = None
    yield
    _reset_tool_catalog_cache_for_tests()
    efr._RH_AGENTIC_MCP_ADAPTER_CACHE = None


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


class FailingTransport:
    def __call__(self, url, headers, body, timeout):
        raise RequestsConnectionError("agent.robinhood.com refused connection")


class CountingFailingTransport:
    def __init__(self):
        self.calls = 0

    def __call__(self, url, headers, body, timeout):
        self.calls += 1
        raise RequestsConnectionError("agent.robinhood.com refused connection")


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


def test_adapter_enabled_auth_probe_is_shared_across_instances():
    t = FakeTransport()
    assert _adapter(transport=t).is_enabled() is True
    assert _adapter(transport=t).is_enabled() is True

    auth_calls = [
        r for r in t.requests
        if r["method"] == "tools/call" and r["params"]["name"] == "list_orders"
    ]
    assert len(auth_calls) == 1


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
    a._account_number = "acct-test"
    a._account_verified = True
    out = a.place_market_order(product_id="AAPL", side="buy", base_size="3", client_order_id="cid-1")
    assert out["ok"] is True
    assert out["order_id"] == "ord-77"
    assert out["client_order_id"] == "cid-1"
    call = [r for r in t.requests if r["method"] == "tools/call"][-1]
    args = call["params"]["arguments"]
    assert args == {
        "account_number": "acct-test",
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "3",
        "type": "market",
        "market_hours": "regular_hours",
        "time_in_force": "gfd",
        "ref_id": "cid-1",
    }


def test_place_market_order_unresolved_tool_fails_loud():
    a = _adapter(transport=FakeTransport(tools=[{"name": "unrelated_tool"}]))
    a._account_number = "acct-test"
    a._account_verified = True
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


def test_agentic_read_paths_fail_open_on_raw_transport_connection_error():
    a = _adapter(transport=FailingTransport())
    a._account_number = "acct-test"

    orders, _fresh = a.list_open_orders(product_id="LGPS")
    one_order, _fresh = a.get_order("ord-1")
    fills, _fresh = a.get_fills(product_id="LGPS")

    assert orders == []
    assert one_order is None
    assert fills == []
    assert a.get_account_snapshot()["ok"] is False
    assert a.get_buying_power_usd() is None
    assert a.get_account_equity_usd() is None
    assert a.get_agentic_open_positions() == []
    assert a.get_position_quantity("LGPS") is None
    assert a.get_agentic_open_orders(symbol="LGPS") == []


def test_agentic_positions_failure_backoff_suppresses_retry():
    transport = CountingFailingTransport()
    a = _adapter(transport=transport)
    a._account_number = "acct-test"
    a._resolved["positions"] = "get_equity_positions"

    assert a.get_agentic_open_positions() == []
    assert a.get_agentic_open_positions() == []
    assert transport.calls == 1


def test_agentic_open_orders_failure_backoff_suppresses_retry():
    transport = CountingFailingTransport()
    a = _adapter(transport=transport)
    a._account_number = "acct-test"
    a._resolved["list_orders"] = "get_equity_orders"

    assert a.get_agentic_open_orders(symbol="LGPS") == []
    assert a.get_agentic_open_orders(symbol="LGPS") == []
    assert transport.calls == 1


def test_agentic_positions_backoff_cache_is_not_flat_truth():
    a = _adapter()
    a._account_number = "acct-test"
    now = time.monotonic()
    with a._agentic_positions_cache_lock:
        a._agentic_positions_cache = []
        a._agentic_positions_cache_at = now - 10.0
        a._agentic_positions_next_probe_at = now + 10.0

    assert a.get_position_quantity("LGPS") is None


def test_agentic_order_paths_fail_cleanly_on_raw_transport_connection_error():
    a = _adapter(transport=FailingTransport())
    a._account_number = "acct-test"

    place = a.place_limit_order_gtc(
        product_id="LGPS",
        side="sell",
        base_size="1",
        limit_price="1.00",
        client_order_id="cid-exit",
    )
    cancel = a.cancel_order("ord-exit")

    assert place["ok"] is False
    assert place["client_order_id"] == "cid-exit"
    assert cancel["ok"] is False
    assert cancel["order_id"] == "ord-exit"


def test_agentic_transport_outage_suppresses_subsequent_order_calls():
    transport = CountingFailingTransport()
    a = _adapter(transport=transport)
    a._account_number = "acct-test"
    a._account_verified = True
    a._resolved["place_order"] = "place_equity_order"
    a._resolved["cancel_order"] = "cancel_equity_order"

    first = a.place_limit_order_gtc(
        product_id="LGPS",
        side="sell",
        base_size="1",
        limit_price="1.00",
        client_order_id="cid-exit-1",
    )
    second = a.place_limit_order_gtc(
        product_id="LGPS",
        side="sell",
        base_size="1",
        limit_price="1.00",
        client_order_id="cid-exit-2",
    )
    cancel = a.cancel_order("ord-exit")

    assert first["ok"] is False
    assert second["ok"] is False
    assert second["code"] == "rail_transport_unavailable"
    assert second["retry_after_seconds"] > 0
    assert cancel["ok"] is False
    assert cancel["code"] == "rail_transport_unavailable"
    assert transport.calls == 1


def test_agentic_transient_auth_probe_is_suppressed_until_timeout_window():
    transport = CountingFailingTransport()
    client = RhMcpClient(
        endpoint="https://example.test/mcp",
        token="tok",
        timeout=7.0,
        http_post=transport,
    )
    a = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=_MDStub())

    assert a.is_enabled() is False
    assert a.is_enabled() is False
    assert a.is_enabled() is False
    assert transport.calls == 1
    assert a._execution_auth_transient_unavailable is True


def test_agentic_position_quantity_reuses_position_book_within_tick_window():
    t = FakeTransport(
        tools=[{"name": "get_equity_positions"}],
        call_result={
            "content": [{"type": "text", "text": json.dumps({"positions": [
                {"symbol": "AAPL", "quantity": "2"},
                {"symbol": "META", "quantity": "1"},
            ]})}],
            "isError": False,
        },
    )
    a = _adapter(transport=t)
    a._account_number = "acct-test"

    assert a.get_position_quantity("AAPL") == 2.0
    assert a.get_position_quantity("META") == 1.0
    position_calls = [
        r for r in t.requests
        if r["method"] == "tools/call" and r.get("params", {}).get("name") == "get_equity_positions"
    ]
    assert len(position_calls) == 1


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


def test_factory_caches_mcp_adapter_by_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_cache_rh_agentic_adapter", True, raising=False)
    factory = efr.resolve_live_spot_adapter_factory(efr.EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP)

    assert factory() is factory()


def test_factory_cache_can_be_disabled(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_cache_rh_agentic_adapter", False, raising=False)
    factory = efr.resolve_live_spot_adapter_factory(efr.EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP)

    assert factory() is not factory()


def test_venue_for_mcp_family_is_robinhood():
    assert efr.venue_for_execution_family(efr.EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP) == "robinhood"
