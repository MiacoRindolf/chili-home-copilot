"""VenueAdapter for Robinhood equities via the **official Agentic Trading MCP rail**.

This is the sanctioned counterpart to ``robinhood_spot.py`` (which executes via the
*unofficial* ``robin_stocks`` private API). Order flow + isolated-account position/balance
truth go through Robinhood's hosted MCP server (``agent.robinhood.com/mcp/trading``) using
``RhMcpClient`` — deterministic ``tools/call``, **no LLM in the loop**. Orders land in a
dedicated, blast-radius-bounded **Agentic account**.

Design:
- **Market data** (quotes/products) **delegates to ``RobinhoodSpotAdapter``** — quotes are not
  the rail's purpose, and the spot adapter already does fill-venue-accurate equity quotes with the
  Legend/BOATS overnight fallback. Reusing it avoids guessing MCP market-data tools that may not exist.
- **Execution + account** (orders, fills, positions, balance) go through the MCP rail.
- ``is_enabled()`` gates on **token presence** — a real dependency, not a default-OFF dark flag.

The two things the public docs don't yet pin down — the exact RH **tool names** and the
**request/response field names** — are isolated in the ``_TOOL_HINTS`` / ``_ARG_KEYS`` / ``_RESP_KEYS``
constants below. They are resolved at runtime from a live ``tools/list`` (capability matching) and
finalized in one place against ``scripts/rh_agentic_introspect.py`` output (design P1). Until a
capability resolves to a real tool, the execution methods **fail loud** rather than guess.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
    VenueAdapter,
    VenueAdapterError,
)
from .rh_mcp_client import McpToolResult, RhMcpClient, RhMcpError, get_default_client

logger = logging.getLogger(__name__)

_VENUE = "robinhood_agentic_mcp"

# ── Isolated unknowns (finalized in design P1 from the live tools/list) ──────────
# Capability -> ordered keyword groups; a tool matches a capability if its name (lower)
# contains every keyword in any one group. Override per-capability with an explicit name
# via env CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP='{"place_order":"<real_name>", ...}'.
_TOOL_HINTS: dict[str, list[list[str]]] = {
    "place_order": [["place", "order"], ["submit", "order"], ["create", "order"]],
    "cancel_order": [["cancel", "order"]],
    "list_orders": [["list", "orders"], ["get", "orders"], ["open", "orders"]],
    "get_order": [["get", "order"], ["order", "status"], ["order", "detail"]],
    "positions": [["position"], ["holding"]],
    "account": [["account"], ["balance"], ["buying", "power"]],
    "preview_order": [["preview", "order"], ["estimate", "order"], ["quote", "order"]],
}

# Candidate request-argument keys (we send a superset-safe subset; finalized in P1).
_ARG_KEYS = {
    "symbol": "symbol",
    "side": "side",
    "quantity": "quantity",
    "order_type": "type",
    "limit_price": "limit_price",
    "time_in_force": "time_in_force",
    "client_order_id": "client_order_id",
}

# Candidate response field keys (we try each, in order, then keep raw). Finalized in P1.
_RESP_KEYS = {
    "order_id": ("id", "order_id", "orderId"),
    "client_order_id": ("client_order_id", "clientOrderId"),
    "symbol": ("symbol", "ticker", "instrument_symbol"),
    "side": ("side", "direction"),
    "status": ("status", "state"),
    "order_type": ("type", "order_type"),
    "filled_size": ("filled_quantity", "cumulative_quantity", "filled_qty", "quantity"),
    "avg_price": ("average_price", "avg_price", "executed_price", "price"),
    "created_time": ("created_at", "created_time", "createdAt"),
    "fee": ("fees", "fee", "total_fees"),
}


def _now_freshness(max_age: float = 15.0) -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=max_age)


def _to_ticker(product_id: str) -> str:
    s = (product_id or "").strip().upper()
    return s[:-4] if s.endswith("-USD") else s


def _pick(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return None


def _sf(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def _load_tool_overrides() -> dict[str, str]:
    raw = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP") or ""
    if not raw:
        try:
            from ....config import settings

            raw = getattr(settings, "chili_robinhood_agentic_mcp_tool_map", "") or ""
        except Exception:
            raw = ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v}
    except Exception as exc:
        logger.warning("[rh_mcp_adapter] bad CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP json: %s", exc)
    return {}


class RobinhoodAgenticMcpAdapter(VenueAdapter):
    """Robinhood equities via the sanctioned Agentic MCP rail (execution) +
    ``robinhood_spot`` (market data)."""

    def __init__(
        self,
        *,
        client: Optional[RhMcpClient] = None,
        market_data_adapter: Optional[Any] = None,
    ):
        self._client = client
        self._md = market_data_adapter
        self._tool_names: Optional[list[str]] = None
        self._resolved: dict[str, Optional[str]] = {}
        self._overrides = _load_tool_overrides()

    # ── wiring ─────────────────────────────────────────────────────────

    def _get_client(self) -> RhMcpClient:
        if self._client is None:
            self._client = get_default_client()
        return self._client

    def _market_data(self):
        if self._md is None:
            from .robinhood_spot import RobinhoodSpotAdapter

            self._md = RobinhoodSpotAdapter()
        return self._md

    def is_enabled(self) -> bool:
        # Token present == enabled. A missing token is a real dependency, not a dark flag.
        try:
            return self._get_client().has_token()
        except Exception:
            return False

    # ── tool resolution (capability matching over a live tools/list) ────

    def _tool_catalog(self) -> list[str]:
        if self._tool_names is None:
            tools = self._get_client().list_tools()
            self._tool_names = [str(t.get("name")) for t in tools if t.get("name")]
            logger.info("[rh_mcp_adapter] discovered %d MCP tools: %s", len(self._tool_names), self._tool_names)
        return self._tool_names

    def _resolve_tool(self, capability: str) -> Optional[str]:
        if capability in self._resolved:
            return self._resolved[capability]
        # 1) explicit operator override wins (set after introspection)
        override = self._overrides.get(capability)
        if override:
            self._resolved[capability] = override
            return override
        # 2) keyword capability match against the live catalog
        names = self._tool_catalog()
        match: Optional[str] = None
        for groups in _TOOL_HINTS.get(capability, []):
            for name in names:
                low = name.lower()
                if all(kw in low for kw in groups):
                    match = name
                    break
            if match:
                break
        self._resolved[capability] = match
        if match is None:
            logger.warning("[rh_mcp_adapter] no MCP tool resolved for capability=%s", capability)
        return match

    def _require_tool(self, capability: str) -> str:
        name = self._resolve_tool(capability)
        if not name:
            raise VenueAdapterError(
                f"no Robinhood Agentic MCP tool for capability {capability!r} "
                f"(resolve via introspection -> CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP)",
                code="tool_unresolved",
            )
        return name

    def _call(self, capability: str, arguments: dict) -> McpToolResult:
        name = self._require_tool(capability)
        res = self._get_client().call_tool(name, arguments)
        if res.is_error:
            raise VenueAdapterError(
                f"MCP tool {name!r} returned isError", code="tool_error", raw=res.data()
            )
        return res

    # ── Market data — delegate to the proven robin_stocks spot adapter ──

    def get_product(self, product_id: str):
        return self._market_data().get_product(product_id)

    def get_products(self):
        return self._market_data().get_products()

    def get_best_bid_ask(self, product_id: str):
        return self._market_data().get_best_bid_ask(product_id)

    def get_ticker(self, product_id: str):
        return self._market_data().get_ticker(product_id)

    def get_recent_trades(self, product_id: str, *, limit: int = 50):
        return self._market_data().get_recent_trades(product_id, limit=limit)

    def get_quote_price(self, product_id: str) -> Optional[float]:
        md = self._market_data()
        fn = getattr(md, "get_quote_price", None)
        return fn(product_id) if callable(fn) else None

    def get_quote_prices_batch(self, product_ids: list[str]) -> dict[str, float]:
        md = self._market_data()
        fn = getattr(md, "get_quote_prices_batch", None)
        return fn(product_ids) if callable(fn) else {}

    # ── Orders / account — via the sanctioned MCP rail ──────────────────

    def _normalize_order(self, od: dict) -> NormalizedOrder:
        return NormalizedOrder(
            order_id=str(_pick(od, _RESP_KEYS["order_id"]) or ""),
            client_order_id=_pick(od, _RESP_KEYS["client_order_id"]),
            product_id=str(_pick(od, _RESP_KEYS["symbol"]) or ""),
            side=str(_pick(od, _RESP_KEYS["side"]) or "buy"),
            status=str(_pick(od, _RESP_KEYS["status"]) or "unknown"),
            order_type=str(_pick(od, _RESP_KEYS["order_type"]) or "market"),
            filled_size=_sf(_pick(od, _RESP_KEYS["filled_size"])) or 0.0,
            average_filled_price=_sf(_pick(od, _RESP_KEYS["avg_price"])),
            created_time=_pick(od, _RESP_KEYS["created_time"]),
            raw=od,
        )

    @staticmethod
    def _as_order_dicts(data: Any) -> list[dict]:
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("orders", "results", "data", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
            return [data]
        return []

    def list_open_orders(self, *, product_id: Optional[str] = None, limit: int = 50):
        fresh = _now_freshness()
        try:
            res = self._call("list_orders", {})
            orders = [self._normalize_order(o) for o in self._as_order_dicts(res.data())]
            if product_id:
                t = _to_ticker(product_id)
                orders = [o for o in orders if o.product_id.upper() == t]
            return orders[:limit], fresh
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] list_open_orders failed: %s", exc)
            return [], fresh

    def get_order(self, order_id: str):
        fresh = _now_freshness()
        try:
            res = self._call("get_order", {"order_id": order_id, "id": order_id})
            dicts = self._as_order_dicts(res.data())
            return (self._normalize_order(dicts[0]) if dicts else None), fresh
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] get_order(%s) failed: %s", order_id, exc)
            return None, fresh

    def get_fills(self, *, product_id: Optional[str] = None, limit: int = 50):
        fresh = _now_freshness()
        try:
            res = self._call("list_orders", {})
            fills: list[NormalizedFill] = []
            for od in self._as_order_dicts(res.data()):
                status = str(_pick(od, _RESP_KEYS["status"]) or "").lower()
                if status not in ("filled", "complete", "completed"):
                    continue
                fills.append(
                    NormalizedFill(
                        fill_id=str(_pick(od, _RESP_KEYS["order_id"]) or "") or None,
                        order_id=str(_pick(od, _RESP_KEYS["order_id"]) or "") or None,
                        product_id=str(_pick(od, _RESP_KEYS["symbol"]) or ""),
                        side=str(_pick(od, _RESP_KEYS["side"]) or "buy"),
                        size=_sf(_pick(od, _RESP_KEYS["filled_size"])) or 0.0,
                        price=_sf(_pick(od, _RESP_KEYS["avg_price"])) or 0.0,
                        fee=_sf(_pick(od, _RESP_KEYS["fee"])),
                        trade_time=_pick(od, _RESP_KEYS["created_time"]),
                        raw=od,
                    )
                )
            if product_id:
                t = _to_ticker(product_id)
                fills = [f for f in fills if f.product_id.upper() == t]
            return fills[:limit], fresh
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] get_fills failed: %s", exc)
            return [], fresh

    def _build_order_args(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        order_type: str,
        limit_price: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        args = {
            _ARG_KEYS["symbol"]: _to_ticker(product_id),
            _ARG_KEYS["side"]: side,
            _ARG_KEYS["quantity"]: base_size,
            _ARG_KEYS["order_type"]: order_type,
        }
        if limit_price is not None:
            args[_ARG_KEYS["limit_price"]] = limit_price
            args[_ARG_KEYS["time_in_force"]] = "gtc"
        if client_order_id:
            args[_ARG_KEYS["client_order_id"]] = client_order_id
        return args

    def _order_result(self, res: McpToolResult, client_order_id: Optional[str]) -> dict:
        data = res.data()
        od = data if isinstance(data, dict) else {}
        if not od:
            dicts = self._as_order_dicts(data)
            od = dicts[0] if dicts else {}
        order_id = _pick(od, _RESP_KEYS["order_id"])
        return {
            "ok": True,
            "venue": _VENUE,
            "order_id": str(order_id) if order_id is not None else None,
            "client_order_id": client_order_id,
            "status": _pick(od, _RESP_KEYS["status"]),
            "raw": res.raw,
        }

    def place_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        client_order_id: Optional[str] = None,
    ) -> dict:
        try:
            res = self._call(
                "place_order",
                self._build_order_args(
                    product_id=product_id,
                    side=side,
                    base_size=base_size,
                    order_type="market",
                    client_order_id=client_order_id,
                ),
            )
            return self._order_result(res, client_order_id)
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] place_market_order(%s) failed: %s", product_id, exc)
            return {"ok": False, "venue": _VENUE, "error": str(exc), "client_order_id": client_order_id}

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
    ) -> dict:
        try:
            res = self._call(
                "place_order",
                self._build_order_args(
                    product_id=product_id,
                    side=side,
                    base_size=base_size,
                    order_type="limit",
                    limit_price=limit_price,
                    client_order_id=client_order_id,
                ),
            )
            return self._order_result(res, client_order_id)
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] place_limit_order_gtc(%s) failed: %s", product_id, exc)
            return {"ok": False, "venue": _VENUE, "error": str(exc), "client_order_id": client_order_id}

    def cancel_order(self, order_id: str) -> dict:
        try:
            res = self._call("cancel_order", {"order_id": order_id, "id": order_id})
            return {"ok": True, "venue": _VENUE, "order_id": order_id, "raw": res.raw}
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] cancel_order(%s) failed: %s", order_id, exc)
            return {"ok": False, "venue": _VENUE, "error": str(exc), "order_id": order_id}

    def preview_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: Optional[str] = None,
        quote_size: Optional[str] = None,
    ) -> dict:
        # Preview is optional on the rail; degrade gracefully if no such tool exists.
        if not self._resolve_tool("preview_order"):
            return {"ok": False, "venue": _VENUE, "error": "preview not supported on MCP rail"}
        try:
            args = {_ARG_KEYS["symbol"]: _to_ticker(product_id), _ARG_KEYS["side"]: side}
            if base_size is not None:
                args[_ARG_KEYS["quantity"]] = base_size
            res = self._call("preview_order", args)
            return {"ok": True, "venue": _VENUE, "raw": res.raw, "data": res.data()}
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] preview_market_order(%s) failed: %s", product_id, exc)
            return {"ok": False, "venue": _VENUE, "error": str(exc)}

    def get_account_snapshot(self) -> dict:
        try:
            res = self._call("account", {})
            return {"ok": True, "venue": _VENUE, "data": res.data(), "raw": res.raw}
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] get_account_snapshot failed: %s", exc)
            return {"ok": False, "venue": _VENUE, "error": str(exc)}
