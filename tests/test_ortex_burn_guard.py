"""Ortex CREDIT-BURN GUARD (2026-07-26): process-wide daily HTTP budget + 12h negative
cache. The Trader plan meters credits; a pathological loop / no-data storm must never
drain the account. Budget exhausted => fetches fail-open to None (the chain's existing
neutral no-data semantics) with zero HTTP; UTC-day keyed reset; 0 = unlimited.
"""
from __future__ import annotations

from unittest.mock import patch

import app.services.trading.momentum_neural.short_mechanics as sm


def _reset_budget():
    sm._budget_day = ""
    sm._budget_used = 0


def test_budget_decrements_and_allows_within_budget():
    _reset_budget()
    with patch.object(sm.settings, "chili_ortex_daily_fetch_budget", 3):
        assert sm._daily_budget_remaining() is True
        assert sm._daily_budget_remaining() is True
        assert sm._daily_budget_remaining() is True
        assert sm._budget_used == 3


def test_budget_exhaustion_blocks_without_http():
    _reset_budget()
    with patch.object(sm.settings, "chili_ortex_daily_fetch_budget", 1):
        assert sm._daily_budget_remaining() is True
        assert sm._daily_budget_remaining() is False  # exhausted
        assert sm._daily_budget_remaining() is False  # stays blocked
    # at the fetch layer: no urlopen when exhausted
    _reset_budget()
    with patch.object(sm.settings, "chili_ortex_daily_fetch_budget", 0):
        pass  # (0 = unlimited; covered below)
    _reset_budget()
    with patch.object(sm.settings, "chili_ortex_daily_fetch_budget", 1), \
            patch.object(sm.urllib.request, "urlopen", side_effect=AssertionError("HTTP must not fire")) as mock_open:
        sm._budget_used = 2  # already past the budget crossing
        sm._budget_day = sm.time.strftime("%Y-%m-%d", sm.time.gmtime())
        out = sm._rate_limited_get_json("/stock/nasdaq/TEST/ctb/all", "k")
        assert out is None
        mock_open.assert_not_called()


def test_zero_budget_means_unlimited():
    _reset_budget()
    with patch.object(sm.settings, "chili_ortex_daily_fetch_budget", 0):
        for _ in range(50):
            assert sm._daily_budget_remaining() is True
        assert sm._budget_used == 0  # counter untouched when guard is off


def test_day_rollover_resets_counter():
    _reset_budget()
    with patch.object(sm.settings, "chili_ortex_daily_fetch_budget", 2):
        assert sm._daily_budget_remaining() is True
        assert sm._daily_budget_remaining() is True
        assert sm._daily_budget_remaining() is False
        # bagong UTC day -> reset
        sm._budget_day = "1999-01-01"
        assert sm._daily_budget_remaining() is True
        assert sm._budget_used == 1


def test_neg_cache_ttl_is_daily_class():
    # daily rows: a no-data symbol today stays no-data today (>= 6h floor is the intent)
    assert sm._NEG_CACHE_TTL_SECONDS >= 6 * 3600.0
