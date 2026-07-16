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
import threading
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
from .rh_mcp_client import McpToolResult, NeedsReauth, RhMcpClient, RhMcpError, get_default_client

logger = logging.getLogger(__name__)

_VENUE = "robinhood_agentic_mcp"
_TOOL_CATALOG_CACHE_LOCK = threading.Lock()
_TOOL_CATALOG_CACHE: dict[tuple[str, object], tuple[str, ...]] = {}


def _reset_tool_catalog_cache_for_tests() -> None:
    with _TOOL_CATALOG_CACHE_LOCK:
        _TOOL_CATALOG_CACHE.clear()


def _is_rate_limit_exc(exc: Exception) -> bool:
    """True iff an adapter exception represents a 429 / rate-limit push-back from the
    rail. Recognizes the typed RhMcpError code (``http_429``, set at
    ``rh_mcp_client.py`` when the transport returns HTTP 429) and the rate-limit text
    forms (``429`` / ``rate limit`` / ``too many requests``). Used so the POLL path can
    SURFACE a 429 to the rail governor instead of masking it as a benign None."""
    code = str(getattr(exc, "code", "") or "")
    if code in ("http_429",) or code.endswith("_429"):
        return True
    low = str(exc or "").lower()
    return (
        "429" in low
        or "rate limit" in low
        or "rate-limit" in low
        or "ratelimit" in low
        or "too many requests" in low
    )

# ── Tool map (finalized 2026-06-19 against the real RH Agentic schema) ────────────
def _auth_unavailable_reason(exc: Exception) -> str | None:
    if isinstance(exc, NeedsReauth):
        return f"needs_reauth:{getattr(exc, 'reason', '') or 'unknown'}"
    code = str(getattr(exc, "code", "") or "").lower()
    text = str(exc or "").lower()
    if code in ("unauthorized", "no_token"):
        return code
    if (
        "needs_reauth" in text
        or "re-auth" in text
        or "reauth" in text
        or "token missing/expired" in text
        or ("unauthorized" in text and "agentic" in text)
    ):
        return "unauthorized"
    return None


# Capability -> ordered keyword groups; a tool matches a capability if its name (lower)
# contains every keyword in any one group. Override per-capability with an explicit name
# via env CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP. The DEFAULT real schema is:
#   {"place_order":"place_equity_order","preview_order":"review_equity_order",
#    "cancel_order":"cancel_equity_order","list_orders":"get_equity_orders",
#    "get_order":"get_equity_orders","positions":"get_equity_positions",
#    "account":"get_accounts"}
# The keyword hints below resolve to those names off a live tools/list when no override
# is set (e.g. "place_equity_order" matches ["place","order"]).
_TOOL_HINTS: dict[str, list[list[str]]] = {
    "place_order": [["place", "equity", "order"], ["place", "order"], ["submit", "order"]],
    "preview_order": [["review", "equity", "order"], ["review", "order"], ["preview", "order"]],
    "cancel_order": [["cancel", "equity", "order"], ["cancel", "order"]],
    "list_orders": [["get", "equity", "orders"], ["list", "orders"], ["get", "orders"]],
    "get_order": [["get", "equity", "orders"], ["get", "order"], ["order", "status"]],
    "positions": [["get", "equity", "positions"], ["position"], ["holding"]],
    "account": [["get", "accounts"], ["account"], ["balance"]],
    "portfolio": [["get", "portfolio"], ["portfolio"], ["buying", "power"]],
    "quotes": [["get", "equity", "quotes"], ["equity", "quotes"], ["quote"]],
    # 24h-tradeability probe (call BEFORE placing an overnight order to surface
    # per-session eligibility, so ineligible names are skipped — never order-rejected).
    "tradability": [["get", "equity", "tradability"], ["equity", "tradability"], ["tradability"]],
}

# Request-argument keys (the real place_equity_order / review_equity_order schema).
_ARG_KEYS = {
    "account_number": "account_number",
    "symbol": "symbol",
    "side": "side",
    "quantity": "quantity",
    "dollar_amount": "dollar_amount",
    "order_type": "type",
    "limit_price": "limit_price",
    "stop_price": "stop_price",
    "time_in_force": "time_in_force",
    "market_hours": "market_hours",
    "ref_id": "ref_id",
}

# Default order metadata (the real schema's enums).
_DEFAULT_TIF = "gfd"            # day order
_DEFAULT_TIF_RESTING = "gtc"    # resting limit order
_DEFAULT_MARKET_HOURS = "regular_hours"  # regular_hours | extended_hours | all_day_hours

# Response field keys (we try each, in order, then keep raw).
_RESP_KEYS = {
    "order_id": ("id", "order_id", "orderId"),
    "client_order_id": ("ref_id", "client_order_id", "clientOrderId"),
    "symbol": ("symbol", "ticker", "instrument_symbol"),
    "side": ("side", "direction"),
    "status": ("state", "status"),
    "order_type": ("type", "order_type"),
    "filled_size": ("filled_quantity", "cumulative_quantity", "filled_qty"),
    "avg_price": ("average_price", "avg_price", "executed_price", "price"),
    "created_time": ("created_at", "created_time", "createdAt"),
    "fee": ("fees", "fee", "total_fees"),
}

_TERMINAL_ORDER_STATES = frozenset(
    {
        "cancelled",
        "canceled",
        "filled",
        "done",
        "closed",
        "expired",
        "failed",
        "rejected",
    }
)


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


def _is_working_order_dict(od: dict) -> bool:
    status = str(_pick(od, _RESP_KEYS["status"]) or "").strip().lower()
    return not status or status not in _TERMINAL_ORDER_STATES


def _unwrap_payload(data: Any) -> Any:
    """Peel the RH MCP response envelope ``{"data": <payload>, "guide": "..."}``.

    The live agentic tools (get_portfolio/get_accounts/orders/positions) nest the
    real payload under ``data`` alongside a human-readable ``guide`` string; the
    field-extractors below must see the inner payload, not the envelope. Keyed on
    BOTH ``data`` and ``guide`` present so a legitimate payload that merely has a
    ``data`` field is never mis-unwrapped. Idempotent + fail-soft."""
    if isinstance(data, dict) and "guide" in data and "data" in data:
        return data["data"]
    return data


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
        account_number: Optional[str] = None,
    ):
        self._client = client
        self._md = market_data_adapter
        self._tool_names: Optional[list[str]] = None
        self._resolved: dict[str, Optional[str]] = {}
        self._overrides = _load_tool_overrides()
        # The isolated Agentic account every order is PINNED to — frozen at
        # construction from the explicit arg or settings. There is NO code path that
        # takes an account from the caller/brain, so a misrouted order to the main
        # portfolio is structurally impossible.
        cfg_acct = ""
        try:
            from ....config import settings

            cfg_acct = getattr(settings, "chili_robinhood_agentic_mcp_account_number", "") or ""
        except Exception:
            cfg_acct = ""
        if not cfg_acct:
            cfg_acct = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_ACCOUNT_NUMBER", "") or ""
        self._account_number = (account_number or cfg_acct or "").strip()
        # Latched True once the pinned account proves to be non-agentic — the rail
        # then reports DISABLED (never trades a non-isolated account).
        self._pin_invalid = False
        # Cache of the agentic-account verification (avoids a get_accounts per order).
        self._account_verified = False
        self._execution_auth_checked = False
        self._execution_auth_ok = False
        self._execution_auth_error: Optional[str] = None

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
        # Auth-aware, fail-closed: a missing token, an unrecoverable auth state
        # (NeedsReauth), or a pinned account that is NOT agentic-allowed all report
        # DISABLED. A token that can initialize/list tools but cannot call the
        # order tool is also DISABLED: the live runner must not treat an
        # unauthorized execution rail as an empty-but-tradable account.
        try:
            client = self._get_client()
        except Exception:
            return False
        try:
            if not client.has_token():
                return False
            if self._pin_invalid:
                return False
            client.ensure_authable()
            return self._execution_auth_probe()
        except NeedsReauth:
            self._execution_auth_checked = True
            self._execution_auth_ok = False
            self._execution_auth_error = "needs_reauth"
            return False
        except Exception:
            return False

    def _execution_auth_probe(self) -> bool:
        if self._execution_auth_checked:
            return bool(self._execution_auth_ok)
        self._execution_auth_checked = True
        self._execution_auth_ok = False
        try:
            if not self._resolve_tool("list_orders"):
                self._execution_auth_error = "list_orders_tool_unresolved"
                return False
            self._call("list_orders", self._read_args({"placed_agent": "agentic"}))
        except Exception as exc:
            self._execution_auth_error = _auth_unavailable_reason(exc) or str(exc)[:300]
            logger.warning("[rh_mcp_adapter] execution auth probe failed: %s", self._execution_auth_error)
            return False
        self._execution_auth_ok = True
        self._execution_auth_error = None
        return True

    # ── tool resolution (capability matching over a live tools/list) ────

    def _tool_catalog(self) -> list[str]:
        if self._tool_names is None:
            client = self._get_client()
            cache_key = (str(getattr(client, "endpoint", "")), getattr(client, "_http_post", None))
            with _TOOL_CATALOG_CACHE_LOCK:
                cached = _TOOL_CATALOG_CACHE.get(cache_key)
                if cached is None:
                    tools = client.list_tools()
                    names = tuple(str(t.get("name")) for t in tools if t.get("name"))
                    if names:
                        _TOOL_CATALOG_CACHE[cache_key] = names
                else:
                    names = cached
            names = tuple(names)
            self._tool_names = list(names)
            if cached is None:
                logger.info("[rh_mcp_adapter] discovered %d MCP tools: %s", len(names), list(names))
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
        try:
            res = self._get_client().call_tool(name, arguments)
        except (NeedsReauth, RhMcpError) as exc:
            reason = _auth_unavailable_reason(exc)
            if reason:
                self._execution_auth_checked = True
                self._execution_auth_ok = False
                self._execution_auth_error = reason
            raise
        if res.is_error:
            # Surface the BROKER'S actual rejection text in the message — not a bare
            # "returned isError". RH puts the real reason (suitability / collar / an
            # open-position restriction / buying-power / ...) in the content text blocks;
            # it was attached as .raw but str(exc) dropped it, so entry_place_result.error
            # and the log read an opaque "returned isError" and the failure was
            # UNDIAGNOSABLE (RKLZ/CORD 2026-06-22: both whole-share, penny-priced,
            # all_day_tradable, yet isError'd — the reason was invisible).
            _detail = (res.text or "").strip()
            if not _detail:
                try:
                    _detail = str(_unwrap_payload(res.data()) or "")
                except Exception:
                    _detail = ""
            exc = VenueAdapterError(
                f"MCP tool {name!r} returned isError"
                + (f": {_detail[:500]}" if _detail else ""),
                code="tool_error",
                raw=res.data(),
            )
            reason = _auth_unavailable_reason(exc)
            if reason:
                self._execution_auth_checked = True
                self._execution_auth_ok = False
                self._execution_auth_error = reason
            raise exc
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
        data = _unwrap_payload(data)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("orders", "results", "data", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
            return [data]
        return []

    def _read_args(self, extra: Optional[dict] = None) -> dict:
        """Read args scoped to the pinned account (the real reads take account_number)."""
        args: dict[str, Any] = {}
        if self._account_number:
            args[_ARG_KEYS["account_number"]] = self._account_number
        if extra:
            args.update(extra)
        return args

    def list_open_orders(self, *, product_id: Optional[str] = None, limit: int = 50):
        fresh = _now_freshness()
        try:
            res = self._call("list_orders", self._read_args())
            orders = [
                self._normalize_order(o)
                for o in self._as_order_dicts(res.data())
                if _is_working_order_dict(o)
            ]
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
            res = self._call("get_order", self._read_args({"order_id": order_id}))
            dicts = self._as_order_dicts(res.data())
            # get_equity_orders is a LIST endpoint — match the row whose id == order_id.
            # Never trust [0]: a server that ignores the id filter hands back the
            # most-recent order, and the runner would poll the WRONG status/fill.
            match = next(
                (d for d in dicts if str(_pick(d, _RESP_KEYS["order_id"]) or "") == str(order_id)),
                None,
            )
            if match is None and len(dicts) == 1:
                match = dicts[0]  # single-row response ⇒ the server applied the filter
            return (self._normalize_order(match) if match else None), fresh
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] get_order(%s) failed: %s", order_id, exc)
            # RATE-LIMIT OBSERVABILITY (2026-06-27): get_order is a LIST endpoint sharing
            # the per-account rail budget, so a 429 on the POLL path is a primary flooding
            # signal. Swallowing it to (None, fresh) makes the rail governor read the poll
            # as a SUCCESS and WIDEN the rate INTO the rate limit (defeating its purpose).
            # Re-raise ONLY the rate-limit case so the governed wrapper can HALVE the rate;
            # every other transient error still returns (None, fresh) (unchanged behavior).
            if _is_rate_limit_exc(exc):
                raise
            return None, fresh

    def get_fills(self, *, product_id: Optional[str] = None, limit: int = 50):
        fresh = _now_freshness()
        try:
            res = self._call("list_orders", self._read_args())
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

    # ── 24h tradeability probe (TIER-2 overnight, call before ordering) ──

    def get_equity_tradability(self, symbols: list[str]) -> dict[str, dict]:
        """Per-session tradeability for up to 10 equity symbols via the RH MCP
        ``get_equity_tradability`` tool. Returns ``{SYM: {overnight_eligible, extended_eligible,
        fractional}}`` parsed from the per-session eligibility field. Call BEFORE placing an
        overnight order so an ineligible name is SKIPPED at the gate (no untradable-reject spam).

        Batches of <=10 (the tool's cap). Account = the pinned agentic account_number (the tool
        requires it). Fail-open to ``{}`` on any error / unresolved tool, so the caller fails
        CLOSED (no positive eligibility = not 24h-armable)."""
        out: dict[str, dict] = {}
        syms = [str(s or "").strip().upper() for s in (symbols or []) if str(s or "").strip()]
        if not syms:
            return out
        if not self._resolve_tool("tradability"):
            return out
        if not self._account_number:
            return out
        for i in range(0, len(syms), 10):
            batch = syms[i : i + 10]
            try:
                res = self._call(
                    "tradability",
                    self._read_args({"symbols": batch}),
                )
            except (VenueAdapterError, RhMcpError, NeedsReauth) as exc:
                logger.warning("[rh_mcp_adapter] get_equity_tradability(%s) failed: %s", batch, exc)
                continue
            for row in self._as_order_dicts(res.data()):
                sym = str(_pick(row, _RESP_KEYS["symbol"]) or "").strip().upper()
                if not sym:
                    continue
                # Per-session eligibility may arrive flat or nested under a sessions object.
                sessions = row.get("sessions") if isinstance(row.get("sessions"), dict) else row
                _ovn = self._truthy(
                    _pick(
                        sessions,
                        (
                            "twenty_four_seven_tradability",
                            "overnight",
                            "twenty_four_hour",
                            "24_hour",
                        ),
                    )
                    if isinstance(sessions, dict)
                    else None
                )
                _ext = self._truthy(
                    _pick(
                        sessions,
                        (
                            "all_day_tradability",
                            "all_day_hours",
                            "extended_hours",
                            "extended",
                        ),
                    )
                    if isinstance(sessions, dict)
                    else None
                )
                out[sym] = {
                    "overnight_eligible": _ovn,
                    "extended_eligible": _ext,
                    "fractional": self._truthy(
                        _pick(row, ("fractional_tradability", "fractional", "tradable_fractional"))
                    ),
                }
        return out

    @staticmethod
    def _truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            low = v.strip().lower()
            if "not_tradable" in low or "untradable" in low or "non_tradable" in low:
                return False
            if low in ("true", "1", "yes", "eligible", "tradable", "open", "enabled"):
                return True
            return low.endswith("_tradable") or low.endswith(":tradable")
        if isinstance(v, dict):
            # A session object like {"eligible": true} / {"tradable": true}.
            for k in ("eligible", "tradable", "is_eligible", "enabled", "open"):
                if k in v:
                    return RobinhoodAgenticMcpAdapter._truthy(v[k])
        return bool(v) if v is not None else False

    # ── Account pin (the safety latch) ──────────────────────────────────

    @staticmethod
    def _resolve_market_hours(
        market_hours: str,
        market_hours_override: Optional[str],
        extended_hours_override: Optional[bool],
        extended_hours: bool,
        overnight: bool = False,
    ) -> str:
        """Normalize the runner's ext-hours signals into the MCP ``market_hours`` enum.

        The runner uses TWO conventions: the ENTRY passes ``extended_hours`` (bool); the
        EXIT passes the robin_stocks kwarg names ``market_hours_override`` /
        ``extended_hours_override``. Any premarket/extended hint maps to ``extended_hours``
        (pre + regular + post, 04:00-20:00 ET — valid for ALL equities).

        ⚠️ 2026-06-23 LIVE 0-FILL ROOT CAUSE: this previously returned ``all_day_hours``
        (RH's 24-HOUR market), but that designation is only accepted for the small set of
        24h-eligible instruments — for ~every Ross low-float mover (NXTS et al.) RH rejects
        the order with API 400 ``"instrument is untradable for 24 hour trading"``. That single
        wrong enum errored ~every momentum entry at the rail (372 submits -> 4 fills / 44h;
        NXTS submitted 26x, ALL isError). ``extended_hours`` covers the lane's full window and
        is accepted for normal equities, so the order actually rests + fills.

        TIER-2 OVERNIGHT (``overnight=True`` — the runner sets this from
        ``is_overnight_now`` for a name PRE-VERIFIED 24h-eligible): map to ``all_day_hours``
        (RH's 24-hour market). This is the ONLY place all_day_hours is re-introduced, and
        ONLY for names auto_arm already proved 24h-eligible — so the 2026-06-23 regression
        (all_day_hours on NON-eligible names) cannot recur. An explicit market_hours_override
        still wins (exit-path conventions are unchanged)."""
        if market_hours_override:
            return str(market_hours_override)
        if overnight:
            return "all_day_hours"
        if extended_hours_override or extended_hours:
            return "extended_hours"
        return market_hours or _DEFAULT_MARKET_HOURS

    def _build_order_args(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        order_type: str,
        limit_price: Optional[str] = None,
        stop_price: Optional[str] = None,
        market_hours: str = _DEFAULT_MARKET_HOURS,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """Build the order args, UNCONDITIONALLY injecting the pinned account.

        There is no parameter that lets a caller supply an account — the account is
        ALWAYS ``self._account_number``. An empty pin raises ``no_agentic_account``
        so an unconfigured rail can never place an order on the brain's account.
        """
        if not self._account_number:
            raise VenueAdapterError(
                "no Robinhood Agentic account pinned "
                "(set CHILI_ROBINHOOD_AGENTIC_MCP_ACCOUNT_NUMBER)",
                code="no_agentic_account",
            )
        args: dict[str, Any] = {
            _ARG_KEYS["account_number"]: self._account_number,
            _ARG_KEYS["symbol"]: _to_ticker(product_id),
            _ARG_KEYS["side"]: side,
            _ARG_KEYS["quantity"]: base_size,
            _ARG_KEYS["order_type"]: order_type,
            _ARG_KEYS["market_hours"]: market_hours,
        }
        # Honor the caller's TIF (the runner sends "gfd" for DAY entry limits — a
        # resting GTC buy filling hours later is the KMRK -21.9% incident); fall back
        # to the limit/market default only when unspecified.
        _tif = (str(time_in_force).strip().lower() if time_in_force else "") or None
        if limit_price is not None:
            args[_ARG_KEYS["limit_price"]] = limit_price
            args[_ARG_KEYS["time_in_force"]] = _tif or _DEFAULT_TIF_RESTING
        else:
            args[_ARG_KEYS["time_in_force"]] = _tif or _DEFAULT_TIF
        if stop_price is not None:
            args[_ARG_KEYS["stop_price"]] = stop_price
        if client_order_id:
            args[_ARG_KEYS["ref_id"]] = client_order_id
        return args

    def _assert_account_is_agentic(self) -> None:
        """Verify the pinned account is agentic-allowed; latch ``_pin_invalid`` if not.

        Cached after the first success (avoids a get_accounts per order). A pinned
        account whose ``agentic_allowed`` is not True is a hard error — placing on a
        non-isolated account is the exact blast-radius failure the rail must prevent.
        """
        if self._pin_invalid:
            raise VenueAdapterError("pinned account is not agentic-allowed", code="account_not_agentic")
        if self._account_verified:
            return
        if not self._account_number:
            raise VenueAdapterError("no Robinhood Agentic account pinned", code="no_agentic_account")
        try:
            res = self._call("account", {})
        except (VenueAdapterError, RhMcpError):
            # Could not verify this tick — do NOT latch invalid (transient). The order
            # path still injects the pin; re-verification happens next call.
            raise
        accounts = self._as_account_dicts(res.data())
        match = None
        for a in accounts:
            num = str(_pick(a, ("account_number", "number", "id")) or "")
            if num and num == self._account_number:
                match = a
                break
        if match is None:
            # The pinned account was not returned — cannot confirm it is agentic.
            raise VenueAdapterError(
                "pinned agentic account not found in get_accounts", code="account_not_found"
            )
        allowed = match.get("agentic_allowed")
        if allowed is not True:
            self._pin_invalid = True
            logger.error(
                "[rh_mcp_adapter] pinned account is NOT agentic_allowed — rail DISABLED "
                "(account tail=%s)", self._account_number[-4:],
            )
            raise VenueAdapterError("pinned account is not agentic-allowed", code="account_not_agentic")
        self._account_verified = True

    @staticmethod
    def _as_account_dicts(data: Any) -> list[dict]:
        data = _unwrap_payload(data)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("accounts", "results", "data", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
            return [data]
        return []

    def _review_blocks_order(self, args: dict) -> Optional[str]:
        """Run review_equity_order; return a reason string iff a HARD pre-trade alert
        clearly blocks the order. Conservative + fail-OPEN: soft alerts pass, and any
        ambiguity / review failure does NOT block (the in-process gates already vetted
        the trade — review is a belt-and-suspenders pre-trade check)."""
        if not self._resolve_tool("preview_order"):
            return None
        review_args = {k: v for k, v in args.items() if k != _ARG_KEYS["ref_id"]}
        try:
            res = self._call("preview_order", review_args)
        except (VenueAdapterError, RhMcpError):
            return None  # fail-open: review unavailable does not block
        data = _unwrap_payload(res.data())
        od = data if isinstance(data, dict) else {}
        # Only abort on a clearly-blocking marker. RH review surfaces alerts under
        # "alerts"/"warnings"; we treat severity in {error, blocking, reject} OR an
        # explicit can_place == False as HARD. Everything else passes.
        if od.get("can_place") is False or od.get("can_proceed") is False:
            return "review_can_place_false"
        for key in ("alerts", "warnings", "messages"):
            items = od.get(key)
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                sev = str(it.get("severity") or it.get("type") or it.get("level") or "").lower()
                if sev in ("error", "blocking", "block", "reject", "hard"):
                    return f"review_hard_alert:{sev}"
        return None

    def _place(self, capability: str, args: dict, *, is_review: bool = False) -> McpToolResult:
        """THE single order chokepoint every order method routes through.

        Guarantees, in order: (1) the pinned agentic account is injected (done in
        ``_build_order_args``, asserted non-empty); (2) the account is verified
        agentic-allowed; (3) optional review-before-place aborts on a HARD alert;
        (4) the call carries ``ref_id`` (the runner's idempotency token). Raises
        ``VenueAdapterError`` on a blocked order; transport errors propagate.
        """
        # (1) the account must already be pinned in args (defense-in-depth).
        if args.get(_ARG_KEYS["account_number"]) != self._account_number or not self._account_number:
            raise VenueAdapterError("agentic account pin missing on order args", code="no_agentic_account")
        # (2) verify the pinned account is agentic.
        self._assert_account_is_agentic()
        # (3) review-before-place (skipped for the review call itself).
        try:
            from ....config import settings

            review_on = bool(getattr(settings, "chili_robinhood_agentic_mcp_review_before_place", True))
        except Exception:
            review_on = True
        if review_on and not is_review:
            reason = self._review_blocks_order(args)
            if reason:
                raise VenueAdapterError(f"pre-trade review blocked order ({reason})", code="review_blocked")
        # (4) place.
        return self._call(capability, args)

    def _order_result(self, res: McpToolResult, client_order_id: Optional[str]) -> dict:
        data = _unwrap_payload(res.data())
        od = data if isinstance(data, dict) else {}
        order_id = _pick(od, _RESP_KEYS["order_id"])
        # A place response may nest the order under a singular "order"/"result" key, or
        # arrive as a list — descend/peel BEFORE giving up on the id (do this whenever the
        # id is unresolved, not only when od is empty: a non-empty dict with no id is
        # exactly the case that would otherwise orphan a real fill).
        if order_id is None and isinstance(od, dict):
            for _k in ("order", "result"):
                _inner = od.get(_k)
                if isinstance(_inner, dict) and _pick(_inner, _RESP_KEYS["order_id"]) is not None:
                    od = _inner
                    order_id = _pick(od, _RESP_KEYS["order_id"])
                    break
        if order_id is None:
            _dicts = self._as_order_dicts(data)
            if _dicts:
                od = _dicts[0]
                order_id = _pick(od, _RESP_KEYS["order_id"])
        # An ACCEPTED order with no resolvable order_id is unmanageable — the runner could
        # not poll its fill or place a stop, leaving a naked, unmanaged long. Fail the place
        # (ok=False) so the caller takes the place-failed branch and re-watches, rather than
        # advancing to PENDING_ENTRY with a None id. Loud: should never happen on the real
        # RH place schema (docs/DESIGN/ROBINHOOD_AGENTIC_MCP.md §11).
        if order_id is None:
            logger.error(
                "[rh_mcp_adapter] place response had NO resolvable order_id (cid=%s) — "
                "failing the place to avoid a naked unmanaged position; raw=%r",
                client_order_id, res.raw,
            )
            return {
                "ok": False, "venue": _VENUE, "error": "no_order_id_in_place_response",
                "client_order_id": client_order_id, "raw": res.raw,
            }
        return {
            "ok": True,
            "venue": _VENUE,
            "order_id": str(order_id),
            "client_order_id": client_order_id,
            "status": _pick(od, _RESP_KEYS["status"]),
            "raw": res.raw,
        }

    def _failed_order(self, client_order_id: Optional[str], exc: Exception) -> dict:
        return {"ok": False, "venue": _VENUE, "error": str(exc), "client_order_id": client_order_id}

    def place_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        client_order_id: Optional[str] = None,
        market_hours: str = _DEFAULT_MARKET_HOURS,
        market_hours_override: Optional[str] = None,
        extended_hours_override: Optional[bool] = None,
        extended_hours: bool = False,
        overnight: bool = False,
        time_in_force: Optional[str] = None,
    ) -> dict:
        try:
            res = self._place(
                "place_order",
                self._build_order_args(
                    product_id=product_id,
                    side=side,
                    base_size=base_size,
                    order_type="market",
                    market_hours=self._resolve_market_hours(
                        market_hours, market_hours_override, extended_hours_override,
                        extended_hours, overnight=overnight,
                    ),
                    time_in_force=time_in_force,
                    client_order_id=client_order_id,
                ),
            )
            return self._order_result(res, client_order_id)
        except NeedsReauth as e:
            return {"ok": False, "venue": _VENUE, "error": "needs_reauth", "reason": e.reason,
                    "client_order_id": client_order_id}
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] place_market_order(%s) failed: %s", product_id, exc)
            return self._failed_order(client_order_id, exc)

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
        market_hours: str = _DEFAULT_MARKET_HOURS,
        market_hours_override: Optional[str] = None,
        extended_hours_override: Optional[bool] = None,
        extended_hours: bool = False,
        overnight: bool = False,
        time_in_force: Optional[str] = None,
    ) -> dict:
        try:
            res = self._place(
                "place_order",
                self._build_order_args(
                    product_id=product_id,
                    side=side,
                    base_size=base_size,
                    order_type="limit",
                    limit_price=limit_price,
                    market_hours=self._resolve_market_hours(
                        market_hours, market_hours_override, extended_hours_override,
                        extended_hours, overnight=overnight,
                    ),
                    time_in_force=time_in_force,
                    client_order_id=client_order_id,
                ),
            )
            return self._order_result(res, client_order_id)
        except NeedsReauth as e:
            return {"ok": False, "venue": _VENUE, "error": "needs_reauth", "reason": e.reason,
                    "client_order_id": client_order_id}
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] place_limit_order_gtc(%s) failed: %s", product_id, exc)
            return self._failed_order(client_order_id, exc)

    def place_stop_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        stop_price: str,
        client_order_id: Optional[str] = None,
        market_hours: str = _DEFAULT_MARKET_HOURS,
    ) -> dict:
        """Forward-looking broker-side stop (CHILI manages stops in-process today)."""
        try:
            res = self._place(
                "place_order",
                self._build_order_args(
                    product_id=product_id,
                    side=side,
                    base_size=base_size,
                    order_type="stop_market",
                    stop_price=stop_price,
                    market_hours=market_hours,
                    client_order_id=client_order_id,
                ),
            )
            return self._order_result(res, client_order_id)
        except NeedsReauth as e:
            return {"ok": False, "venue": _VENUE, "error": "needs_reauth", "reason": e.reason,
                    "client_order_id": client_order_id}
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] place_stop_market_order(%s) failed: %s", product_id, exc)
            return self._failed_order(client_order_id, exc)

    def place_stop_limit_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        stop_price: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
        market_hours: str = _DEFAULT_MARKET_HOURS,
    ) -> dict:
        """Forward-looking broker-side stop-limit (CHILI manages stops in-process)."""
        try:
            res = self._place(
                "place_order",
                self._build_order_args(
                    product_id=product_id,
                    side=side,
                    base_size=base_size,
                    order_type="stop_limit",
                    limit_price=limit_price,
                    stop_price=stop_price,
                    market_hours=market_hours,
                    client_order_id=client_order_id,
                ),
            )
            return self._order_result(res, client_order_id)
        except NeedsReauth as e:
            return {"ok": False, "venue": _VENUE, "error": "needs_reauth", "reason": e.reason,
                    "client_order_id": client_order_id}
        except (VenueAdapterError, RhMcpError) as exc:
            logger.warning("[rh_mcp_adapter] place_stop_limit_order(%s) failed: %s", product_id, exc)
            return self._failed_order(client_order_id, exc)

    def cancel_order(self, order_id: str) -> dict:
        try:
            if not self._account_number:
                raise VenueAdapterError("no Robinhood Agentic account pinned", code="no_agentic_account")
            self._assert_account_is_agentic()
            res = self._call(
                "cancel_order",
                {_ARG_KEYS["account_number"]: self._account_number, "order_id": order_id},
            )
            return {"ok": True, "venue": _VENUE, "order_id": order_id, "raw": res.raw}
        except NeedsReauth as e:
            return {"ok": False, "venue": _VENUE, "error": "needs_reauth", "reason": e.reason, "order_id": order_id}
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
        if not self._account_number:
            return {"ok": False, "venue": _VENUE, "error": "no_agentic_account"}
        try:
            args = {
                _ARG_KEYS["account_number"]: self._account_number,
                _ARG_KEYS["symbol"]: _to_ticker(product_id),
                _ARG_KEYS["side"]: side,
            }
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

    def get_buying_power_usd(self) -> Optional[float]:
        """Real spendable BUYING POWER of the pinned agentic account, in USD.

        get_accounts -> confirm the pinned agentic account -> get_portfolio(account)
        -> float(buying_power.buying_power). The agentic account is a CASH account
        (buying_power == real spendable, NO margin), so the sizing read uses this
        directly with NO margin multiple. Fail-open: returns None on any error so the
        sizing layer falls back to its documented cap (never sizes against unknown BP).
        """
        if not self._account_number:
            return None
        try:
            self._assert_account_is_agentic()
            res = self._call("portfolio", {_ARG_KEYS["account_number"]: self._account_number})
        except (VenueAdapterError, RhMcpError):
            return None
        data = _unwrap_payload(res.data())
        od = data if isinstance(data, dict) else {}
        if not od:
            dicts = self._as_account_dicts(data)
            od = dicts[0] if dicts else {}
        # The schema nests buying_power under a "buying_power" object: {"buying_power": {...}}.
        bp_obj = od.get("buying_power")
        if isinstance(bp_obj, dict):
            return _sf(_pick(bp_obj, ("buying_power", "amount", "value")))
        # Flat fallback: a scalar buying_power.
        return _sf(bp_obj)

    def get_account_equity_usd(self) -> Optional[float]:
        """Total ACCOUNT VALUE (equity) of the pinned agentic account, in USD — the stable
        cash+positions value (``total_value``), NOT the intraday-fluctuating buying_power. The
        daily-loss RISK cap sizes off THIS (operator 2026-06-22: "equity based naman dapat
        talaga") so the cap is steady (~5% x $13.8k ~= $690), not jittering with settled-cash /
        BP swings. Same portfolio call as get_buying_power_usd. Fail-open: None on any error."""
        if not self._account_number:
            return None
        try:
            self._assert_account_is_agentic()
            res = self._call("portfolio", {_ARG_KEYS["account_number"]: self._account_number})
        except (VenueAdapterError, RhMcpError):
            return None
        data = _unwrap_payload(res.data())
        od = data if isinstance(data, dict) else {}
        if not od:
            dicts = self._as_account_dicts(data)
            od = dicts[0] if dicts else {}
        v = _sf(_pick(od, ("total_value", "equity", "equity_value", "portfolio_value")))
        if v is not None and v > 0:
            return v
        return _sf(od.get("cash"))

    # ── B3: agentic-account orphan sweep (the reconciler is blind to this account) ──

    def get_agentic_open_positions(self) -> list[dict]:
        """Raw open positions for the PINNED agentic account (B3 orphan detection).

        The broker-sync reconciler runs a robin_stocks session on the MAIN account and
        is BLIND to the isolated agentic account, so a filled agentic position has no
        reconciler backstop. This surfaces the agentic book so the runner's restart/adopt
        path can re-adopt an unmanaged position. Fail-open (empty on error)."""
        if not self._account_number:
            return []
        try:
            res = self._call("positions", {_ARG_KEYS["account_number"]: self._account_number})
            return self._as_order_dicts(res.data())
        except (VenueAdapterError, RhMcpError, NeedsReauth) as exc:
            logger.warning("[rh_mcp_adapter] get_agentic_open_positions failed: %s", exc)
            return []

    def get_position_quantity(self, product_id: str) -> Optional[float]:
        """TOTAL shares the AGENTIC account holds for *product_id*, per RH truth.

        This is the agentic twin of ``broker_service.get_open_position_quantity``
        (used by ``robinhood_spot``) and shares its fail-safe contract so the exit
        BROKER-QTY CLAMP in ``live_runner`` fires for the agentic family too:

          * ``None``  = unknown (no account / API error / re-auth) — caller MUST
            fail SAFE and never treat unknown as flat (the clamp leaves the
            requested quantity unchanged).
          * ``0.0``   = a SUCCESSFUL fetch confirming this symbol is absent from the
            agentic book (confirmed flat → the clamp reconciles the session to
            exited via ``broker_zero`` instead of stranding a phantom sell).
          * ``> 0``   = the real held quantity; if it is LESS than the requested
            exit quantity the clamp sells only what the broker holds, killing the
            "Not enough shares to sell" → 8-retry → live_error → naked-long storm
            (e.g. PLSM 2026-06-24 agentic sessions 8613/8616).

        Reuses the proven ``get_agentic_open_positions()`` read + the same position
        dict field keys as ``rh_agentic_orphan_sweep`` (symbol via
        symbol/ticker/instrument_symbol, qty via quantity/shares/position/size).
        One position read per exit attempt — exits are infrequent, so this does not
        hammer the rate-limited agentic MCP.
        """
        want = _to_ticker(product_id)
        if not want:
            return None
        try:
            positions = self.get_agentic_open_positions()
        except Exception as exc:  # noqa: BLE001 — fail SAFE: unknown, not flat
            logger.warning("[rh_mcp_adapter] get_position_quantity(%s) read failed: %s", want, exc)
            return None
        if positions is None:
            return None
        try:
            # UNWRAP the agentic positions payload. get_agentic_open_positions can return
            # the RH-nested shape [{"positions": [{symbol..}, ...]}] (a single wrapper dict
            # whose "positions" key holds the real rows) OR a flat list of position dicts.
            # The wrapper has NO "symbol", so iterating it directly missed EVERY symbol and
            # returned 0.0 for REAL holdings -> the exit clamp read broker_qty=0 -> blocked
            # every sell (FCUV/WEN stuck + un-exitable 2026-06-25). Flatten BOTH shapes; and
            # on an UNRECOGNIZED payload fail SAFE as UNKNOWN (None), never 0.0 — assuming
            # flat from an unparsed payload is the exact failure that blocked exits.
            rows: list = []
            recognized = False
            for item in positions:
                if isinstance(item, dict) and isinstance(item.get("positions"), list):
                    rows.extend(item["positions"])
                    recognized = True
                elif isinstance(item, dict) and _pick(item, ("symbol", "ticker", "instrument_symbol")):
                    rows.append(item)
                    recognized = True
            if not recognized and positions:
                logger.warning(
                    "[rh_mcp_adapter] get_position_quantity(%s): unrecognized positions shape -> unknown (fail-safe)",
                    want,
                )
                return None
            for pos in rows:
                if not isinstance(pos, dict):
                    continue
                sym = str(_pick(pos, ("symbol", "ticker", "instrument_symbol")) or "").strip().upper()
                if sym != want:
                    continue
                qty = _sf(_pick(pos, ("quantity", "shares", "position", "size")))
                return abs(qty) if qty is not None else 0.0
            # Recognized book, symbol genuinely absent -> confirmed flat.
            return 0.0
        except Exception as exc:  # noqa: BLE001 — fail SAFE
            logger.warning("[rh_mcp_adapter] get_position_quantity(%s) parse failed: %s", want, exc)
            return None

    def get_agentic_open_orders(self, *, symbol: Optional[str] = None) -> list[dict]:
        """Raw open orders for the pinned agentic account, filtered to agent-placed.

        Mirrors the robin_stocks orphan-detection read but scoped to the agentic
        account + ``placed_agent='agentic'``. Fail-open (empty on error)."""
        if not self._account_number:
            return []
        args: dict[str, Any] = {
            _ARG_KEYS["account_number"]: self._account_number,
            "placed_agent": "agentic",
        }
        if symbol:
            args[_ARG_KEYS["symbol"]] = _to_ticker(symbol)
        try:
            res = self._call("list_orders", args)
            orders = [o for o in self._as_order_dicts(res.data()) if _is_working_order_dict(o)]
            if symbol:
                want = _to_ticker(symbol)
                orders = [
                    o for o in orders
                    if str(_pick(o, _RESP_KEYS["symbol"]) or "").strip().upper() == want
                ]
            return orders
        except (VenueAdapterError, RhMcpError, NeedsReauth) as exc:
            logger.warning("[rh_mcp_adapter] get_agentic_open_orders failed: %s", exc)
            return []
