"""f-coinbase-autotrader-enablement-phase-3-broker-selector (2026-05-09).

Pin every branch of `select_venue` + the LIVE-flag gate semantics
documented in the brief. Helper-level (no DB; the fast-path branch
uses the test-injection seam).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.trading import broker_selector as bs
from app.services.trading.broker_selector import (
    REASON_COINBASE_WHITELIST,
    REASON_COINBASE_RH_CRYPTO_DEGRADED,
    REASON_FAST_PATH_ACTIVE,
    REASON_KILL_SWITCH_GLOBAL,
    REASON_KILL_SWITCH_GOVERNANCE,
    REASON_NO_VENUE,
    REASON_RH_WHITELIST,
    VenueDecision,
    resolve_coinbase_whitelist,
    resolve_rh_whitelist,
    select_venue,
)


def _settings_stub(*, kill_switch: bool = False, coinbase_live: bool = False):
    return SimpleNamespace(
        chili_autotrader_kill_switch=kill_switch,
        chili_coinbase_autotrader_live=coinbase_live,
        chili_broker_selector_rh_crypto_degraded_fallback_enabled=False,
    )


# ── Branch 1 — kill switch ───────────────────────────────────────────


def test_branch1_env_kill_switch_skips_all():
    s = _settings_stub(kill_switch=True)
    res = select_venue(ticker="AAPL", settings_=s, fast_path_active=False)
    assert res == VenueDecision(
        venue="skip", reason=REASON_KILL_SWITCH_GLOBAL,
    )


def test_branch1_env_kill_switch_short_circuits_before_fast_path():
    """Kill switch precedes fast-path — even if fast-path holds the
    ticker, kill-switch wins."""
    s = _settings_stub(kill_switch=True)
    res = select_venue(ticker="BTC-USD", settings_=s, fast_path_active=True)
    assert res.reason == REASON_KILL_SWITCH_GLOBAL


def test_branch1_governance_kill_switch_skips(monkeypatch):
    """In-process governance.is_kill_switch_active() also trips."""
    s = _settings_stub(kill_switch=False)
    monkeypatch.setattr(
        "app.services.trading.governance.is_kill_switch_active",
        lambda: True,
    )
    res = select_venue(ticker="AAPL", settings_=s, fast_path_active=False)
    assert res == VenueDecision(
        venue="skip", reason=REASON_KILL_SWITCH_GOVERNANCE,
    )


# ── Branch 2 — fast-path overlap ────────────────────────────────────


def test_branch2_fast_path_active_skips_coinbase_routing():
    """When fast-path holds the ticker, the autotrader skips it
    entirely (no Coinbase duplicate)."""
    s = _settings_stub()
    res = select_venue(
        ticker="BTC-USD", settings_=s, fast_path_active=True,
    )
    assert res.venue == "skip"
    assert res.reason == REASON_FAST_PATH_ACTIVE


def test_branch2_fast_path_inactive_for_equity_proceeds():
    """Equities don't have a fast-path universe; the resolver should
    NOT block them."""
    s = _settings_stub()
    res = select_venue(
        ticker="AAPL", settings_=s, fast_path_active=False,
    )
    assert res.venue == "rh"
    assert res.reason == REASON_RH_WHITELIST


# ── Branch 3 — RH whitelist (cost-preferred) ────────────────────────


def test_branch3_rh_equity_routes_rh():
    s = _settings_stub()
    for ticker in ("AAPL", "MSFT", "TSLA", "NVDA", "GME"):
        res = select_venue(
            ticker=ticker, settings_=s, fast_path_active=False,
        )
        assert res.venue == "rh"
        assert res.reason == REASON_RH_WHITELIST


def test_branch3_rh_whitelisted_crypto_routes_rh():
    """RH-listed crypto bases (BTC, ETH, ADA, etc.) prefer RH on cost."""
    s = _settings_stub()
    for ticker in ("BTC-USD", "ETH-USD", "ADA-USD", "DOGE-USD"):
        res = select_venue(
            ticker=ticker, settings_=s, fast_path_active=False,
        )
        assert res.venue == "rh", (
            f"{ticker} should route to RH (whitelisted), got {res}"
        )
        assert res.reason == REASON_RH_WHITELIST


# ── Branch 4 — Coinbase long-tail ───────────────────────────────────


def test_branch3_rh_whitelisted_crypto_falls_back_when_rh_degraded(monkeypatch):
    s = _settings_stub()
    s.chili_broker_selector_rh_crypto_degraded_fallback_enabled = True
    min_failures = 2
    lookback_minutes = 60

    def _degraded_state(_ticker, *, db=None, settings_=None):
        assert settings_ is s
        return bs.RhCryptoDegradationState(
            degraded=True,
            failures=min_failures,
            min_failures=min_failures,
            lookback_minutes=lookback_minutes,
            reason=bs.RH_CRYPTO_DEGRADED_REASON_FAILURE_THRESHOLD,
        )

    monkeypatch.setattr(bs, "rh_crypto_degradation_state", _degraded_state)

    res = select_venue(
        ticker="BTC-USD",
        settings_=s,
        db=object(),
        fast_path_active=False,
    )

    assert res.venue == "coinbase"
    assert res.reason == REASON_COINBASE_RH_CRYPTO_DEGRADED
    assert res.extra == {
        "rh_failures": min_failures,
        "rh_min_failures": min_failures,
        "rh_lookback_minutes": lookback_minutes,
    }


def test_branch4_coinbase_only_crypto_routes_coinbase():
    """Crypto bases NOT in RH whitelist route to Coinbase."""
    s = _settings_stub()
    for ticker in ("AKT-USD", "1INCH-USD", "RENDER-USD", "ARB-USD"):
        res = select_venue(
            ticker=ticker, settings_=s, fast_path_active=False,
        )
        assert res.venue == "coinbase", (
            f"{ticker} should route to Coinbase (long-tail), got {res}"
        )
        assert res.reason == REASON_COINBASE_WHITELIST


# ── Branch 5 — no match ─────────────────────────────────────────────


def test_branch5_empty_ticker_skips():
    s = _settings_stub()
    res = select_venue(ticker="", settings_=s, fast_path_active=False)
    assert res.venue == "skip"


def test_branch5_no_venue_supports():
    """Hypothetical: an equity-suffixed ticker that's neither RH
    nor crypto. The resolver currently treats every non-`-USD` as
    RH-eligible (RH supports any equity at the broker layer; finer
    eligibility lives downstream). So this branch only fires for
    edge cases like bad input.

    The selector reaches branch 5 only if RH whitelist returns False
    AND Coinbase whitelist returns False. For a non-`-USD` ticker
    RH always returns True, so no equity hits this path. For a
    `-USD` ticker Coinbase always returns True, so no whitelisted
    crypto hits this path either.

    Pin: explicit empty / non-`-USD` non-equity triggers skip.
    """
    s = _settings_stub()
    # Whitespace-only ticker.
    res = select_venue(ticker="   ", settings_=s, fast_path_active=False)
    assert res.venue == "skip"


# ── Whitelist resolvers ─────────────────────────────────────────────


def test_resolve_rh_whitelist_equity_always_true():
    assert resolve_rh_whitelist("AAPL") is True
    assert resolve_rh_whitelist("MSFT") is True


def test_resolve_rh_whitelist_crypto_in_whitelist():
    assert resolve_rh_whitelist("BTC-USD") is True
    assert resolve_rh_whitelist("ADA-USD") is True
    assert resolve_rh_whitelist("DOGE-USD") is True


def test_resolve_rh_whitelist_crypto_off_whitelist():
    assert resolve_rh_whitelist("AKT-USD") is False
    assert resolve_rh_whitelist("RENDER-USD") is False


def test_resolve_rh_whitelist_empty():
    assert resolve_rh_whitelist("") is False
    assert resolve_rh_whitelist(None) is False  # type: ignore[arg-type]


def test_resolve_coinbase_whitelist_equity_always_false():
    assert resolve_coinbase_whitelist("AAPL") is False


def test_resolve_coinbase_whitelist_crypto_always_true():
    """Per the brief, the selector hands routing to the autotrader
    which queries the live Coinbase product list at placement.
    Selector-level pre-filtering is Phase 5+."""
    assert resolve_coinbase_whitelist("BTC-USD") is True
    assert resolve_coinbase_whitelist("AKT-USD") is True
    assert resolve_coinbase_whitelist("RENDER-USD") is True


# ── Decision-tree precedence ────────────────────────────────────────


def test_kill_switch_overrides_rh_match():
    s = _settings_stub(kill_switch=True)
    res = select_venue(ticker="AAPL", settings_=s, fast_path_active=False)
    assert res.venue == "skip"
    assert res.reason == REASON_KILL_SWITCH_GLOBAL


def test_fast_path_overrides_rh_match():
    """Fast-path BTC-USD: even though RH would normally win on cost,
    fast-path holds it -> skip routing."""
    s = _settings_stub()
    res = select_venue(ticker="BTC-USD", settings_=s, fast_path_active=True)
    assert res.venue == "skip"
    assert res.reason == REASON_FAST_PATH_ACTIVE


def test_rh_preferred_over_coinbase_when_both_match():
    """The brief locks RH-first preference for cost reasons."""
    s = _settings_stub()
    # ETH-USD is in BOTH whitelists (RH lists it; Coinbase trades it).
    # RH wins on cost.
    res = select_venue(ticker="ETH-USD", settings_=s, fast_path_active=False)
    assert res.venue == "rh"


# ── Constants pinned ────────────────────────────────────────────────


def test_reason_constants_have_expected_values():
    """A typo in any of these breaks downstream audit grep / tests."""
    assert REASON_KILL_SWITCH_GLOBAL == "kill_switch_global"
    assert REASON_KILL_SWITCH_GOVERNANCE == "kill_switch_governance"
    assert REASON_FAST_PATH_ACTIVE == "fast_path_active"
    assert REASON_RH_WHITELIST == "rh_whitelist_match"
    assert REASON_COINBASE_WHITELIST == "coinbase_whitelist_match"
    assert REASON_COINBASE_RH_CRYPTO_DEGRADED == "coinbase_rh_crypto_degraded"
    assert REASON_NO_VENUE == "no_venue_supports"
