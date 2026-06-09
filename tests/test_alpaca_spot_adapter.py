"""Alpaca equities VenueAdapter (docs/DESIGN/ALPACA_LANE.md) — pure normalization + the
execution-family wiring. The live paper validation (P1) happens once API keys are set;
these tests need neither alpaca-py installed nor keys (lazy SDK imports)."""

from __future__ import annotations

from app.config import settings
from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SPOT,
    DOCUMENTED_EXECUTION_FAMILIES,
    IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES,
    momentum_runner_supports_execution_family,
    normalize_execution_family,
    resolve_live_spot_adapter_factory,
    venue_for_execution_family,
)
from app.services.trading.venue.alpaca_spot import (
    AlpacaSpotAdapter,
    _f,
    _norm_status,
    _to_symbol,
)


# ── pure: status normalization (the fiddly bit — must align with _order_done_for_entry /
#    _order_open from #550/#551) ────────────────────────────────────────────────────────
def test_norm_status_terminal_and_working():
    # terminal -> canonical terminal words
    assert _norm_status("filled") == "filled"
    assert _norm_status("canceled") == "canceled"
    assert _norm_status("cancelled") == "canceled"
    assert _norm_status("expired") == "expired"
    assert _norm_status("rejected") == "rejected"
    assert _norm_status("done_for_day") == "expired"
    assert _norm_status("replaced") == "canceled"
    # working states -> "open" so the fill poll keeps running (NOT mistaken for terminal)
    assert _norm_status("new") == "open"
    assert _norm_status("accepted") == "open"
    assert _norm_status("pending_new") == "open"
    assert _norm_status("partially_filled") == "open"
    assert _norm_status("held") == "open"
    assert _norm_status("pending_cancel") == "pending"


def test_norm_status_handles_enum_like_and_unknown():
    class _E:
        value = "FILLED"
    assert _norm_status(_E()) == "filled"
    assert _norm_status(None) == "unknown"
    assert _norm_status("some_new_alpaca_state") == "some_new_alpaca_state"


def test_to_symbol_and_float_coercion():
    assert _to_symbol("  aapl ") == "AAPL"
    assert _to_symbol("CLSK") == "CLSK"
    assert _f("2.21") == 2.21
    assert _f(None) is None
    assert _f("not-a-number") is None
    assert _f(float("nan")) is None


def test_is_enabled_false_without_config(monkeypatch):
    # default: chili_alpaca_enabled is False -> disabled regardless of keys.
    monkeypatch.setattr(settings, "chili_alpaca_enabled", False, raising=False)
    assert AlpacaSpotAdapter().is_enabled() is False
    # enabled but no keys -> still disabled (keys are a real activation dependency).
    monkeypatch.setattr(settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_api_key", "", raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_api_secret", "", raising=False)
    assert AlpacaSpotAdapter().is_enabled() is False


# ── execution-family wiring ──────────────────────────────────────────────────
def test_alpaca_family_registered_and_implemented():
    assert EXECUTION_FAMILY_ALPACA_SPOT == "alpaca_spot"
    assert EXECUTION_FAMILY_ALPACA_SPOT in DOCUMENTED_EXECUTION_FAMILIES
    assert EXECUTION_FAMILY_ALPACA_SPOT in IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES
    assert momentum_runner_supports_execution_family("alpaca_spot") is True
    assert normalize_execution_family("ALPACA_SPOT") == "alpaca_spot"


def test_resolve_factory_returns_alpaca_adapter():
    factory = resolve_live_spot_adapter_factory("alpaca_spot")
    assert factory is AlpacaSpotAdapter
    # the factory produces an adapter exposing the Protocol surface the runner uses
    ad = factory()
    for m in ("get_best_bid_ask", "place_market_order", "place_limit_order_gtc",
              "get_order", "cancel_order", "get_account_snapshot", "is_enabled"):
        assert hasattr(ad, m)


def test_venue_for_alpaca_family():
    assert venue_for_execution_family("alpaca_spot") == "alpaca"
