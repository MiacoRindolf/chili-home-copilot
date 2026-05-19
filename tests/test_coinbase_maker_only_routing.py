"""f-coinbase-maker-only-routing (2026-05-19) — pin the new maker-only
entry path in the autotrader, the post_only support in the adapter,
and the settings flag default.

The 2026-05-18 TCA finding: avg +102 bps entry slippage on crypto
trades, eating ~60% of pattern 585's 168 bps gross edge. The
``chili_coinbase_maker_only_enabled`` flag routes Coinbase BUY entries
through a post_only limit order at best-bid instead of a crossing
market order. Default OFF.

Tests pinned here:

1. Settings flag exists and defaults to False (no operational change
   on deploy until operator opts in).
2. ``place_limit_order_gtc`` accepts a ``post_only`` parameter.
3. The autotrader code contains the maker-only branch (static-grep).
4. The autotrader code falls back to market when bid is unavailable
   (static-grep for the fallback log line).

Code-shape tests rather than full-broker integration because the
surrounding context (live adapters, broker mocks, settings injection)
is too heavy. Phase 2/3/4 + Coinbase/bracket-stop tests already pin
the helper semantics.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.config import Settings


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


# ── #1 — settings flag default ────────────────────────────────────────


def test_coinbase_maker_only_flag_defaults_to_false():
    """The maker-only flag must default to False so deploys don't change
    behavior without explicit operator opt-in."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.chili_coinbase_maker_only_enabled is False


# ── #2 — adapter post_only support ────────────────────────────────────


def test_adapter_place_limit_order_gtc_accepts_post_only():
    """``CoinbaseSpotAdapter.place_limit_order_gtc`` must accept a
    ``post_only`` kwarg (default False). The autotrader's maker-only
    branch sets it True."""
    from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter

    sig = inspect.signature(CoinbaseSpotAdapter.place_limit_order_gtc)
    assert "post_only" in sig.parameters, (
        "CoinbaseSpotAdapter.place_limit_order_gtc must accept "
        "post_only kwarg for the maker-only routing path."
    )
    # Default must be False so existing callers stay unaffected.
    assert sig.parameters["post_only"].default is False, (
        "post_only kwarg must default to False so non-maker-only callers "
        "(existing fast-path executor, bracket writer, etc.) keep their "
        "current behavior."
    )


def test_adapter_post_only_uses_sdk_variant_or_kwarg():
    """The adapter must try the SDK's *_post_only variant first (so the
    venue rejects orders that would cross) and fall back to the kwarg
    form. Mirror of coinbase_service.place_buy_order's dispatch."""
    text = _read("app/services/trading/venue/coinbase_spot.py")
    assert "limit_order_gtc_buy_post_only" in text, (
        "coinbase_spot.py must reference limit_order_gtc_buy_post_only "
        "(the SDK's preferred maker-only buy method)."
    )
    assert "limit_order_gtc_sell_post_only" in text, (
        "coinbase_spot.py must reference limit_order_gtc_sell_post_only "
        "(symmetric for sell side)."
    )
    assert "post_only=True" in text, (
        "coinbase_spot.py must include the post_only=True fallback "
        "kwarg form for older SDKs."
    )


# ── #3 — autotrader maker-only branch ─────────────────────────────────


def test_autotrader_has_maker_only_branch():
    """``auto_trader.py`` must contain the maker-only branch that
    consults ``chili_coinbase_maker_only_enabled`` and routes through
    ``place_limit_order_gtc(post_only=True)`` when on."""
    text = _read("app/services/trading/auto_trader.py")

    assert "chili_coinbase_maker_only_enabled" in text, (
        "auto_trader.py is missing the maker-only flag check."
    )
    assert "place_limit_order_gtc" in text, (
        "auto_trader.py is missing the place_limit_order_gtc call "
        "(the maker-only entry path)."
    )
    assert "post_only=True" in text, (
        "auto_trader.py is missing post_only=True. The maker-only "
        "branch must request post_only or the broker may cross the "
        "order as taker."
    )
    assert "get_best_bid_ask" in text, (
        "auto_trader.py is missing get_best_bid_ask. The maker-only "
        "branch must fetch the current best-bid to use as limit price."
    )


def test_autotrader_maker_only_falls_back_to_market_on_missing_bid():
    """If best-bid is None/<=0 OR the maker call raises, the autotrader
    must fall back to the existing market-order path. This preserves
    today's behavior when flag is misconfigured or upstream venue
    quote-feed is degraded."""
    text = _read("app/services/trading/auto_trader.py")

    # Two fallback paths exist: explicit "no best_bid" log + the
    # try/except wrapper. Both should log "falling back to market".
    assert "falling back to market" in text, (
        "auto_trader.py is missing the maker-only fallback log line. "
        "Both the no-bid case and the exception case must log "
        "'falling back to market order' to keep ops grep-able."
    )

    # Verify the market path is the explicit fallback (cb_res is None
    # check before place_market_order).
    assert re.search(
        r"cb_res\s*=\s*None.*if\s+cb_res\s+is\s+None.*place_market_order",
        text, re.DOTALL,
    ), (
        "auto_trader.py must initialize cb_res=None before the maker-"
        "only attempt, then guard the place_market_order call with "
        "`if cb_res is None`. This preserves the original market-order "
        "path as the fallback when maker-only is off or fails."
    )


# ── #4 — flag-on observability tag ────────────────────────────────────


def test_autotrader_tags_maker_routed_orders():
    """Maker-routed orders must be tagged with `_chili_maker_only=True`
    in the response dict so downstream audit/observability can grep
    for them. Symmetric with the existing `_chili_broker_source=coinbase`
    tag pattern."""
    text = _read("app/services/trading/auto_trader.py")
    assert '_chili_maker_only' in text, (
        "auto_trader.py is missing the _chili_maker_only response tag. "
        "Required so downstream audit + TCA queries can distinguish "
        "maker-routed orders from market-routed ones."
    )
