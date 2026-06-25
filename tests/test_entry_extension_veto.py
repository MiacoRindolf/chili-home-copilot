"""Entry-EXTENSION (chase) veto: defer the BUY this tick when the entry sits too far
ABOVE the breakout level — i.e. the move already ran and we'd be buying near a local
top (06-24: RUN @15.51 vs break 12.94 = +19.9%; PLSM @10.21 vs break 7.63 = +33.8%).
The allowed extension is ADAPTIVE to volatility: cap = max(floor, K * atr_pct). These
tests pin the pure decision helper `_entry_extension_veto`:

  (a) FIRES on the live RUN case (entry=15.51, breakout=12.94, atr_pct=0.015 -> True).
  (b) FIRES on the live PLSM case (entry=10.21, breakout=7.63, atr_pct=0.015 -> True).
  (c) NO-OP when the entry is within the cap (entry just above breakout, +3% -> False).
  (d) NO-OP when the kill-switch flag is OFF (byte-identical / parity).
  (e) NO-OP when breakout_level OR atr_pct is None (absent level / vol -> never veto).

Pure / no DB: mirrors tests/test_entry_flow_veto.py (imports settings + helper).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import _entry_extension_veto


# The full realistic CLEAN regime_atr_pct range the call site now feeds (regime_atr_pct
# clamps to [0.004, 0.12]; we add 0.15 as a paranoid upper bound). 06-24 recalibration:
# the veto is fed regime_atr_pct, NOT the structural-divided _eff_atr_pct, and K/floor were
# re-tuned (8.0/0.08 -> 1.0/0.10) so the cap stays BELOW the RUN/PLSM chase distance across
# this WHOLE range — the prior cherry-picked atr_pct~0.015 check let high-vol chases through.
# (Floor bumped 0.05 -> 0.10 on 06-24 to ALLOW a calm sub-10% break-and-go; RUN/PLSM still veto.)
_REGIME_ATR_RANGE = [0.004, 0.01, 0.015, 0.025, 0.04, 0.06, 0.10, 0.12, 0.15]


# ── (a) veto FIRES on the live RUN case (+19.9% extended) ─────────────────────

def test_veto_fires_on_run_case():
    # RUN: entry=15.51 vs breakout=12.94 (+19.9%), atr_pct=0.015.
    # cap = max(0.10, 1.0*0.015) = 0.10 -> threshold = 12.94*1.10; 15.51 >= -> veto.
    assert _entry_extension_veto(15.51, 12.94, 0.015, settings) is True


# ── (b) veto FIRES on the live PLSM case (+33.8% extended) ────────────────────

def test_veto_fires_on_plsm_case():
    # PLSM: entry=10.21 vs breakout=7.63 (+33.8%), atr_pct=0.015 -> veto.
    assert _entry_extension_veto(10.21, 7.63, 0.015, settings) is True


# ── (a'/b') veto FIRES on RUN AND PLSM across the FULL realistic regime-ATR range ──
# This is the core of the 06-24 recalibration: with the CLEAN regime ATR + K=1.0/floor=0.10
# the chase veto must hold even at the TOP of the vol range (where the old K=8 cap ballooned
# to 0.96 and let both names slip through). The structural path used to INFLATE atr_pct on a
# deeper chase; feeding the clean regime ATR removes that loosening, so a high-vol reading no
# longer unblocks the chase.

@pytest.mark.parametrize("atr_pct", _REGIME_ATR_RANGE)
def test_veto_fires_on_run_across_full_vol_range(atr_pct):
    # RUN +19.9% must be vetoed at EVERY realistic regime ATR (not just the calm cherry-pick).
    assert _entry_extension_veto(15.51, 12.94, atr_pct, settings) is True


@pytest.mark.parametrize("atr_pct", _REGIME_ATR_RANGE)
def test_veto_fires_on_plsm_across_full_vol_range(atr_pct):
    # PLSM +33.8% must be vetoed at EVERY realistic regime ATR.
    assert _entry_extension_veto(10.21, 7.63, atr_pct, settings) is True


@pytest.mark.parametrize("atr_pct", [0.0, 0.004, 0.01, 0.015, 0.025, 0.04, 0.06, 0.10])
def test_veto_fires_on_generic_12pct_chase_in_calm_to_high_vol(atr_pct):
    # A generic +12% chase (entry=11.20 vs break=10.00) is vetoed at calm-through-high vol
    # (cap = max(0.10, atr_pct) < 0.12 while atr_pct < 0.12). Only in a genuinely EXPLOSIVE
    # regime (atr_pct >= 0.12) does the proportional cap reach 12% and allow it — by design.
    assert _entry_extension_veto(11.20, 10.00, atr_pct, settings) is True


def test_veto_allows_12pct_only_in_explosive_regime():
    # cap = max(0.05, 1.0*0.12) = 0.12 -> a +12% entry is exactly AT the proportional room.
    assert _entry_extension_veto(11.20, 10.00, 0.12, settings) is False


# ── (c) NO-OP when the entry is within the adaptive cap (+3%) ─────────────────

def test_veto_noop_within_cap():
    # entry only +3% above the break (10.30 vs 10.00); cap >= floor 10% -> allow the entry.
    assert _entry_extension_veto(10.30, 10.00, 0.015, settings) is False


def test_veto_allows_10pct_followthrough_in_explosive_regime():
    # A +10% break-and-go is allowed once the regime ATR justifies it: at atr_pct=0.12 the cap
    # = max(0.10, 1.0*0.12) = 0.12 -> a +10% entry (11.00 vs 10.00) is WITHIN the room -> no veto.
    assert _entry_extension_veto(11.00, 10.00, 0.12, settings) is False


# ── (c2) the 06-24 FLOOR bump (0.05 -> 0.10): a calm +10%-class break-and-go is now ALLOWED ──
# The floor was raised 0.05 -> 0.10 so a calm break-and-go gets at least 10% of room: at calm
# ATR the cap = max(0.10, 1.0*atr) = 0.10. A +9.9% entry (clearly inside the 10% floor) that
# the OLD 0.05 floor would have VETOED is now allowed across the calm/low ATR band — while the
# RUN/PLSM chases (+19.9% / +33.8%) still veto everywhere (covered above).

@pytest.mark.parametrize("atr_pct", [0.0, 0.004, 0.01, 0.015, 0.025])
def test_veto_allows_calm_under_10pct_break_and_go(atr_pct):
    # +9.9% (10.99 vs 10.00) is WITHIN the new 0.10 floor at calm ATR -> NO veto.
    # (Under the old 0.05 floor cap=0.05 this would have vetoed -> the floor bump is the change.)
    assert _entry_extension_veto(10.99, 10.00, atr_pct, settings) is False


def test_veto_boundary_exactly_10pct_at_calm_atr():
    # Boundary doc: the veto is >= so an entry EXACTLY at the floor (+10.0%, 11.00 vs 10.00) at
    # calm ATR sits AT the cap (max(0.10, ~0) = 0.10) and is still deferred — anything strictly
    # BELOW the 10% floor (the test above) is allowed. The floor bump moved the allow-band from
    # <5% up to <10%.
    assert _entry_extension_veto(11.00, 10.00, 0.0, settings) is True


@pytest.mark.parametrize("atr_pct", _REGIME_ATR_RANGE)
def test_veto_still_fires_run_plsm_after_floor_bump(atr_pct):
    # The floor bump must NOT loosen the RUN/PLSM chase veto: both stay vetoed across the FULL
    # regime-ATR range (the high-ATR binding constraint is K*0.12=0.12, governed by K not the
    # floor, so RUN +19.9% / PLSM +33.8% are above the cap everywhere).
    assert _entry_extension_veto(15.51, 12.94, atr_pct, settings) is True   # RUN +19.9%
    assert _entry_extension_veto(10.21, 7.63, atr_pct, settings) is True    # PLSM +33.8%


def test_veto_noop_at_break_regardless_of_vol():
    # Entering essentially AT the break (+1%) is never a chase, at any vol.
    for atr_pct in _REGIME_ATR_RANGE:
        assert _entry_extension_veto(10.10, 10.00, atr_pct, settings) is False


# ── (d) NO-OP when the kill-switch flag is OFF (parity) ───────────────────────

def test_veto_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_entry_extension_veto_enabled", False)
    # even a wildly extended entry must NOT veto when disabled (byte-identical path).
    assert _entry_extension_veto(15.51, 12.94, 0.015, settings) is False


# ── (e) NO-OP / parity when breakout_level or atr_pct is absent (None) ────────

def test_veto_noop_when_breakout_level_none():
    assert _entry_extension_veto(15.51, None, 0.015, settings) is False


def test_veto_noop_when_atr_pct_none():
    assert _entry_extension_veto(15.51, 12.94, None, settings) is False


def test_veto_noop_when_entry_price_none():
    assert _entry_extension_veto(None, 12.94, 0.015, settings) is False


# ─────────────────────────────────────────────────────────────────────────────
# Daily-loss-cap basis stabilizer (_stabilize_account_equity) — the LOW/failed-read
# guard added 06-24 to stop a flaky/tiny RH agentic equity read from collapsing the
# 5%-of-equity daily-loss cap to ~$1 (the "$1 cap" spurious-HALT bug). The helper is
# PURE (module-level dict + monotonic clock; no DB/network) so we pin its contract
# here. Each test clears the module cache first for isolation.
# ─────────────────────────────────────────────────────────────────────────────

import app.services.trading.momentum_neural.risk_policy as _rp
from app.services.trading.momentum_neural.risk_policy import _stabilize_account_equity


@pytest.fixture(autouse=False)
def _clear_equity_cache():
    _rp._ACCOUNT_EQUITY_LAST_GOOD.clear()
    yield
    _rp._ACCOUNT_EQUITY_LAST_GOOD.clear()


def test_stabilizer_passes_through_good_read_and_primes_cache(_clear_equity_cache):
    # A plausible positive read is returned as-is AND written to the last-good slot.
    assert _stabilize_account_equity("robinhood_agentic_mcp", 13558.66) == 13558.66
    slot = _rp._ACCOUNT_EQUITY_LAST_GOOD["robinhood_agentic_mcp"]
    assert slot["value"] == 13558.66


def test_stabilizer_reuses_last_good_on_none_read(_clear_equity_cache):
    # Prime with a real read, then a failed (None) read reuses the last-good value
    # within the grace window — NOT a collapse to None/$1.
    _stabilize_account_equity("robinhood_agentic_mcp", 13558.66)
    assert _stabilize_account_equity("robinhood_agentic_mcp", None) == 13558.66


def test_stabilizer_reuses_last_good_on_zero_read(_clear_equity_cache):
    _stabilize_account_equity("robinhood_agentic_mcp", 13558.66)
    assert _stabilize_account_equity("robinhood_agentic_mcp", 0.0) == 13558.66


def test_stabilizer_rejects_tiny_flake_read(_clear_equity_cache):
    # The $19.46 legacy-account bleed-through: a fresh read < 10% of a still-fresh
    # last-good is treated as a flake and the last-good is reused, not $19.46.
    _stabilize_account_equity("robinhood_agentic_mcp", 13558.66)
    assert _stabilize_account_equity("robinhood_agentic_mcp", 19.46) == 13558.66


def test_stabilizer_does_not_mask_a_real_drawdown(_clear_equity_cache):
    # A genuine decline (>= 10% of last-good, so not a flake) is a GOOD read: it is
    # returned as-is AND overwrites the cache — the guard never masks a real drop.
    _stabilize_account_equity("robinhood_agentic_mcp", 13558.66)
    assert _stabilize_account_equity("robinhood_agentic_mcp", 4000.0) == 4000.0
    assert _rp._ACCOUNT_EQUITY_LAST_GOOD["robinhood_agentic_mcp"]["value"] == 4000.0


def test_stabilizer_returns_none_when_no_cache(_clear_equity_cache):
    # No prior good read + a failed read -> None (caller falls back to the fixed cap),
    # never an invented floor.
    assert _stabilize_account_equity("robinhood_agentic_mcp", None) is None


def test_stabilizer_returns_none_past_grace_window(_clear_equity_cache, monkeypatch):
    # Past the TTL the last-good is discarded — a persistent outage is NOT hidden
    # indefinitely. Force the cached slot's age beyond the TTL.
    _stabilize_account_equity("robinhood_agentic_mcp", 13558.66)
    slot = _rp._ACCOUNT_EQUITY_LAST_GOOD["robinhood_agentic_mcp"]
    slot["ts"] = slot["ts"] - (_rp._ACCOUNT_EQUITY_LAST_GOOD_TTL_SEC + 5.0)
    assert _stabilize_account_equity("robinhood_agentic_mcp", None) is None


def test_stabilizer_is_per_family_isolated(_clear_equity_cache):
    # Each execution family keeps its own last-good slot; a coinbase failure does not
    # borrow the agentic equity.
    _stabilize_account_equity("robinhood_agentic_mcp", 13558.66)
    assert _stabilize_account_equity("coinbase_spot", None) is None
    assert _stabilize_account_equity("robinhood_agentic_mcp", None) == 13558.66
