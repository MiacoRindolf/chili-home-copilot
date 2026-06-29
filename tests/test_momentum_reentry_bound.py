"""TASK#8 — multi-scalp / re-entry: adaptive after-exit cooldown + bounded
re-entry-after-stop-out. PURE (no DB). Runnable now: pytest tests/test_momentum_reentry_bound.py -v.
"""

from app.services.trading.momentum_neural.risk_policy import (
    adaptive_reentry_cooldown_seconds,
    reentry_after_stop_allowed,
)
from app.services.trading.momentum_neural.metrics_surface import multi_scalp_summary


# ── adaptive_reentry_cooldown_seconds ────────────────────────────────────────
_BASE = 300
_REF = 0.03  # vol_ref_atr_pct default


def test_profit_exit_shortens():
    # WIN (return_bps=+50) at ref ATR => ~base*0.25; < base and > 0.
    secs, dbg = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE,
        last_exit_reason="trail",
        last_exit_return_bps=50.0,
        entry_stop_atr_pct=_REF,
    )
    assert 0 < secs < _BASE
    assert dbg["is_profit"] is True
    assert secs == round(_BASE * 0.25 * 1.0)


def test_stopout_full_base():
    # loss (return_bps=-30) at ref ATR => == base (reason_mult 1.0, vol_mult 1.0).
    secs, dbg = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE,
        last_exit_reason="stop_loss",
        last_exit_return_bps=-30.0,
        entry_stop_atr_pct=_REF,
    )
    assert secs == _BASE
    assert dbg["is_profit"] is False


def test_loss_never_shorter_than_base_via_reason():
    # at ref vol, a loss == base while a WIN at the same ATR is strictly less.
    loss, _ = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE, last_exit_reason="stop_loss",
        last_exit_return_bps=-10.0, entry_stop_atr_pct=_REF,
    )
    win, _ = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE, last_exit_reason="target",
        last_exit_return_bps=10.0, entry_stop_atr_pct=_REF,
    )
    assert loss == _BASE
    assert win < loss


def test_vol_scaling_clamped():
    # ATR=0.30 (10x ref) => vol_mult clamped to span (1.5), not 10x.
    hi, dbg_hi = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE, last_exit_reason="stop_loss",
        last_exit_return_bps=-5.0, entry_stop_atr_pct=0.30, vol_span=1.5,
    )
    assert dbg_hi["vol_mult"] == 1.5
    assert hi == round(_BASE * 1.0 * 1.5)
    # ATR=0.001 => clamped to 1/span. (dbg vol_mult is rounded to 4dp in the source.)
    lo, dbg_lo = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE, last_exit_reason="stop_loss",
        last_exit_return_bps=-5.0, entry_stop_atr_pct=0.001, vol_span=1.5,
    )
    assert dbg_lo["vol_mult"] == round(1.0 / 1.5, 4)
    assert lo == round(_BASE * 1.0 * (1.0 / 1.5))


def test_reason_set_match():
    # reason='first_target' with return_bps=None still treated as profit.
    secs, dbg = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE, last_exit_reason="first_target",
        last_exit_return_bps=None, entry_stop_atr_pct=_REF,
    )
    assert dbg["is_profit"] is True
    assert secs < _BASE


def test_fail_neutral():
    # None atr / None return_bps / garbage reason => base unchanged, never raises.
    secs, _ = adaptive_reentry_cooldown_seconds(
        base_seconds=_BASE, last_exit_reason="???garbage???",
        last_exit_return_bps=None, entry_stop_atr_pct=None,
    )
    assert secs == _BASE


# ── reentry_after_stop_allowed ───────────────────────────────────────────────
def test_under_cap_allowed():
    ok, reason = reentry_after_stop_allowed(
        enabled=True, stopout_cycles=1, max_stopout_reentries=3)
    assert ok is True
    assert reason == "allowed"


def test_at_cap_blocked():
    ok, reason = reentry_after_stop_allowed(
        enabled=True, stopout_cycles=3, max_stopout_reentries=3)
    assert ok is False
    assert reason == "max_stopout_reentries_reached"


def test_flag_off_unlimited():
    ok, reason = reentry_after_stop_allowed(
        enabled=False, stopout_cycles=99, max_stopout_reentries=3)
    assert ok is True
    assert reason == "flag_off"


def test_zero_cap_uncapped():
    ok, reason = reentry_after_stop_allowed(
        enabled=True, stopout_cycles=99, max_stopout_reentries=0)
    assert ok is True
    assert reason == "uncapped"


# ── metrics_surface.multi_scalp_summary ──────────────────────────────────────
def test_summary_reads_counters():
    le = {
        "pyramid_add_count": 2,
        "micropullback_reentry_count": 1,
        "stopout_cycles": 3,
        "trade_cycles": 7,
    }
    assert multi_scalp_summary(le) == {
        "pyramid_adds": 2,
        "micropullback_reloads": 1,
        "stopout_reentries": 3,
        "trade_cycles": 7,
    }
    assert multi_scalp_summary({}) == {
        "pyramid_adds": 0,
        "micropullback_reloads": 0,
        "stopout_reentries": 0,
        "trade_cycles": 0,
    }
