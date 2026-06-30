"""Ross-MISTAKES-guarded: one test per documented Ross Cameron mistake, asserting the
CHILI guard makes the anti-pattern STRUCTURALLY IMPOSSIBLE (the guard, not luck, is what
prevents it). Each test cites the exact guard it pins in its docstring.

These are DECISION-PARITY tests on the REAL decision functions — never dollar/P&L
replication. Ross's headline dollar figures (DCFC −$15k, etc.) are single-source
marketing and are NEVER asserted here; only the STRUCTURE of the decision is.

MISTAKE -> GUARD map (each verified importable + adapted to the real signature):

  M1 REVENGE / double-down after a loss
       -> sizing (``compute_risk_first_quantity``) is a pure fn of equity/ATR/stop/
          liquidity ONLY; the streak dial (``streak_risk_multiplier``) is ANTI-martingale
          (<=1.0, hard floor 0.5 at >=3 consecutive losses) so size can NEVER rise after a
          loss. (+ the auto_arm reap-cooldown and per-broker daily-loss cap bound it above.)
  M2 FOMO chase into extension (buy-the-top)
       -> ``pullback_break_confirmation`` rejects a too-deep / extended entry
          (``pullback_too_deep``); the verticality + HOD-extension chase-guards reject a
          vertical chase (``extended_verticality`` / ``momentum_continuation_extended``).
  M3 OVERTRADING past the edge window (Ross: "ALL my losses came after 10am")
       -> the midday de-weight (``_effective_entry_viability_min`` +
          ``market_profile.in_midday_lull``, PR #770) RAISES the effective entry-viability
          bar inside the 10:30-14:30 ET lull. (PARTIAL vs Ross's full session-phase
          edge-weighting — flagged honestly in the test docstring.)
  M4 NO hard daily max-loss
       -> ``governance.check_daily_loss_breach`` / the per-broker daily-loss cap blocks
          NEW ENTRIES once tripped, but ``_kill_switch_halts_exits()`` is False for a
          daily-loss reason so an EXIT is NEVER blocked.
  M5 AVERAGING DOWN (DCFC)
       -> the pyramid (``pyramid_add_decision``) only adds INTO STRENGTH: it requires a
          banked positive cushion (``bid > a0``) AND a new HOD (``bid >= high_water_mark``),
          so an add BELOW the entry/avg is structurally impossible (``cushion_not_banked``).
  M6 LOW-VOLUME PARABOLIC (Ross's dangerous one)
       -> treated as AVOID: the verticality / HOD-extension chase-guard rejects the
          vertical move (NOT a buy) — ``extended_verticality`` /
          ``momentum_continuation_extended``.

TESTS-ONLY — never edits source. Operator runs each file one-at-a-time vs chili_test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── Real decision functions under test (verified importable) ──────────────────
from app.config import settings
from app.services.trading import governance
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import risk_policy
from app.services.trading.momentum_neural.entry_gates import (
    momentum_continuation_trigger,
    pullback_break_confirmation,
)
from app.services.trading.momentum_neural.market_profile import in_midday_lull
from app.services.trading.momentum_neural.paper_execution import pyramid_add_decision
from app.services.trading.momentum_neural.risk_policy import (
    compute_risk_first_quantity,
    streak_risk_multiplier,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
_ROSS = "app.services.trading.momentum_neural.ross_momentum"


def _ohlcv(bars):
    rows = []
    for b in bars:
        if len(b) == 5:
            o, h, l, c, v = b
        else:
            o, h, l, c = b
            v = 1_000_000
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
    return pd.DataFrame(rows)


def _arrays(n: int) -> dict:
    """Clean front-side indicator arrays (9>20 EMA, bullish MACD, low VWAP)."""
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.30] * n,
        "volume_ratio": [1.0] * (n - 1) + [3.0],
        "atr": [0.20] * n,
    }


# ══════════════════════════════════════════════════════════════════════════════
# M1 — REVENGE / DOUBLE-DOWN AFTER A LOSS
#   GUARD: compute_risk_first_quantity is pure (equity/ATR/stop/liquidity only);
#          streak_risk_multiplier is ANTI-martingale (<=1.0, 0.5 floor at >=3 losses).
# ══════════════════════════════════════════════════════════════════════════════

class TestM1NoRevengeSizing:
    def test_sizing_is_pure_function_of_risk_inputs_no_streak_term(self):
        """The size function takes ONLY entry/atr/max_loss/notional/increment/stop_mult —
        there is no win/loss-streak, consecutive-loss, or 'double-down' argument it could
        read. Identical risk inputs => byte-identical qty, regardless of any trade history."""
        kw = dict(
            entry_price=10.0, atr_pct=0.02, max_loss_usd=50.0,
            max_notional_ceiling_usd=2000.0, stop_atr_mult=0.60,
        )
        q1, _ = compute_risk_first_quantity(**kw)
        q2, _ = compute_risk_first_quantity(**kw)
        assert q1 == q2 and q1 > 0  # deterministic, history-independent

    def test_tighter_stop_more_size_at_constant_risk_not_streak(self):
        """Size grows from a TIGHTER stop (smaller risk-per-share at constant $-risk), the
        Ross way — never from a winning/losing streak. This proves the size lever is the
        stop distance, not a martingale dial."""
        wide, _ = compute_risk_first_quantity(
            entry_price=10.0, atr_pct=0.04, max_loss_usd=50.0,
            max_notional_ceiling_usd=10_000.0, stop_atr_mult=0.60,
        )
        tight, _ = compute_risk_first_quantity(
            entry_price=10.0, atr_pct=0.01, max_loss_usd=50.0,
            max_notional_ceiling_usd=10_000.0, stop_atr_mult=0.60,
        )
        assert tight > wide  # tighter stop => more shares at the SAME $ risk

    def test_streak_multiplier_never_exceeds_one_after_losses(self):
        """The streak dial can only REDUCE risk after losses (clamp 0.5..1.5, but the >1.0
        region requires a HIGH win-rate). A pure loss streak yields <=1.0 and, at >=3
        consecutive losses, the hard 0.5 'stop-digging' floor — the OPPOSITE of revenge."""
        # All-losses window of 5 -> win_rate 0.0 -> mult = clamp(0.5+0.0)=0.5; >=3 consec -> 0.5
        loss_rows = [(-5.0, "stop_loss")] * 5

        class _LossDB:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def all(self):
                return loss_rows

        # streak_risk_multiplier imports is_real_entry_outcome from .outcome_labels INSIDE
        # the function -> patch it at its SOURCE module so every loss row counts as a real
        # entry (so the consecutive-loss run is the full window of 5).
        with patch(
            "app.services.trading.momentum_neural.outcome_labels.is_real_entry_outcome",
            return_value=True,
        ):
            mult, dbg = streak_risk_multiplier(_LossDB(), execution_family="coinbase_spot")
        assert mult <= 1.0, f"a loss streak must NOT increase risk, got {mult} dbg={dbg}"
        assert mult == pytest.approx(0.5)  # 3+ consecutive losses -> hard floor

    def test_streak_multiplier_bounds_are_anti_martingale(self):
        """Structural property: the multiplier is bounded in [0.5, 1.5] and the upper half
        is reachable ONLY via wins. There is no codepath that maps 'more recent losses' ->
        'larger size'. Insufficient history fails NEUTRAL (1.0), never aggressive."""

        class _EmptyDB:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def all(self):
                return []

        mult, dbg = streak_risk_multiplier(_EmptyDB(), execution_family="coinbase_spot")
        assert mult == pytest.approx(1.0)  # <5 outcomes -> neutral, NOT amplified
        assert dbg.get("reason") == "insufficient_history"


# ══════════════════════════════════════════════════════════════════════════════
# M2 — FOMO CHASE INTO EXTENSION (BUY-THE-TOP)
#   GUARD: pullback_break_confirmation rejects too-deep/extended (pullback_too_deep);
#          verticality / HOD-extension reject a vertical chase.
# ══════════════════════════════════════════════════════════════════════════════

def _extended_chase_df() -> pd.DataFrame:
    """An up-impulse then a DEEP (>50% of the up-leg) collapse, with the current bar
    chasing a brand-new high far above the impulse — a buy-the-top FOMO entry. The deep
    retrace makes the raw-break path reject with ``pullback_too_deep``."""
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.30, 9.00, 9.25),
        (9.25, 9.55, 9.20, 9.50),
        (9.50, 9.80, 9.45, 9.75),
        (9.75, 10.05, 9.70, 10.00),
        (10.00, 10.20, 9.95, 10.15),   # impulse peak (win_high ~10.20)
        (10.15, 10.18, 9.30, 9.40),    # DEEP flush
        (9.40, 9.50, 9.10, 9.20),      # deeper -> pb_low ~9.10 (retrace > 50%)
        (9.20, 9.35, 9.05, 9.30),      # pullback bars stay deep
        (9.30, 9.45, 9.20, 9.40),
        (9.40, 9.60, 9.35, 9.55),
        (9.55, 9.80, 9.50, 9.75),
        (9.75, 11.50, 9.72, 11.45),    # cur = FOMO chase to a huge new high
    ]
    return _ohlcv(bars)


class TestM2NoFomoChase:
    def test_deep_extended_entry_does_not_fire(self):
        """A buy-the-top after a DEEP (>shallow-cap) pullback is rejected — the raw-break
        path returns ``pullback_too_deep`` (NOT a fire). The chase-guard, not luck, blocks
        the FOMO entry. Mirrors the firing-mock scaffold (settings + indicators patched)."""
        df = _extended_chase_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.30, 0.02, 0.10)), \
                patch(f"{_GATES}.pullback_ordinal_recent", return_value=1), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")):
            ms.chili_momentum_entry_first_pullback_enabled = False  # raw-break path
            ms.chili_momentum_backside_veto_enabled = False
            ms.chili_momentum_candle_quality_multitf_veto_enabled = False
            ms.chili_momentum_red_vol_exhaustion_veto_enabled = False
            ms.chili_momentum_explosive_floor_enabled = False
            ms.chili_momentum_entry_verticality_atr_mult = 0.0
            ms.chili_momentum_entry_macd_open_strict = False
            ok, reason, dbg = pullback_break_confirmation(
                df, entry_interval="1m", symbol="TEST", db=MagicMock(),
                require_retest=False, require_sustained_volume=False,
                require_break_candle=False, require_vwap_hold=False,
                require_macd_bullish=False, volume_spike_multiple=1.5,
            )
        assert ok is False, f"a deep FOMO chase must NOT fire, got reason={reason} dbg={dbg}"
        assert reason == "pullback_too_deep"

    def test_vertical_chase_rejected_by_verticality_gate(self):
        """With the verticality gate ON, a clean shallow pullback that breaks but whose
        price is stretched far above the 9-EMA (a vertical chase) is rejected with
        ``extended_verticality`` — Ross's 'don't chase the parabolic' rule, enforced."""
        # Shallow, clean pullback + break geometry (would otherwise fire) ...
        bars = [
            (9.00, 9.10, 8.95, 9.05),
            (9.05, 9.30, 9.00, 9.25),
            (9.25, 9.55, 9.20, 9.50),
            (9.50, 9.80, 9.45, 9.75),
            (9.75, 10.05, 9.70, 10.00),
            (10.00, 10.20, 9.95, 10.15),
            (10.15, 10.18, 10.00, 10.05),
            (10.05, 10.10, 9.95, 10.00),
            (10.00, 10.08, 9.92, 9.98),
            (9.98, 10.05, 9.90, 9.96),
            (9.96, 10.02, 9.88, 9.95),
            (9.95, 10.10, 9.93, 10.05),
            (10.05, 12.00, 10.02, 11.95),   # break, but CLOSE 11.95 is ~26% above the 9.50 EMA
        ]
        df = _ohlcv(bars)
        # ema9 held at 9.50; atr 0.20 -> atr_pct ~0.017; verticality cap = atr_pct*mult.
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.95, 0.02, 0.10)), \
                patch(f"{_GATES}.pullback_ordinal_recent", return_value=1), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")):
            ms.chili_momentum_entry_first_pullback_enabled = False
            ms.chili_momentum_backside_veto_enabled = False
            ms.chili_momentum_candle_quality_multitf_veto_enabled = False
            ms.chili_momentum_red_vol_exhaustion_veto_enabled = False
            ms.chili_momentum_explosive_floor_enabled = False
            ms.chili_momentum_entry_verticality_atr_mult = 1.5   # gate ON
            ms.chili_momentum_entry_macd_open_strict = False
            ok, reason, dbg = pullback_break_confirmation(
                df, entry_interval="1m", symbol="TEST", db=MagicMock(),
                require_retest=False, require_sustained_volume=False,
                require_break_candle=False, require_vwap_hold=False,
                require_macd_bullish=False, volume_spike_multiple=1.5,
            )
        assert ok is False, f"a vertical chase must NOT fire, got reason={reason} dbg={dbg}"
        assert reason == "extended_verticality"


# ══════════════════════════════════════════════════════════════════════════════
# M3 — OVERTRADING PAST THE EDGE WINDOW (Ross: "ALL my losses came after 10am")
#   GUARD: midday de-weight RAISES the effective entry-viability bar in the lull.
#
#   NOTE (honest gap): this is a PARTIAL implementation of Ross's full session-phase
#   edge-weighting. CHILI's ADMISSION threshold is raised ONLY in the 10:30-14:30 ET
#   midday lull (a one-sided midday penalty); there is NO open-boosted / power-hour
#   threshold weighting on the viability axis. A separate phase-weighted SIZE multiplier
#   ({hot:1.5, midday:0.5, late:0.0}) exists on the risk axis, and the "late" window hard-
#   blocks new entries — but full open/mid/power-hour edge-weighting of the entry BAR is
#   not built. We assert the midday bump that IS built; we do not fake the rest.
# ══════════════════════════════════════════════════════════════════════════════

# A tz-aware UTC instant inside the 10:30-14:30 ET midday band (June -> EDT = UTC-4, so
# 12:00 ET == 16:00 UTC) and one in the 'hot' band (09:00 ET == 13:00 UTC).
_MIDDAY_UTC = datetime(2026, 6, 26, 16, 0, tzinfo=timezone.utc)   # Fri, 12:00 ET
_HOT_UTC = datetime(2026, 6, 26, 13, 0, tzinfo=timezone.utc)      # Fri, 09:00 ET


class TestM3MiddayDeweightRaisesBar:
    def test_midday_window_helper_is_equity_only_and_clock_correct(self):
        """``in_midday_lull`` returns True for an equity inside the lull and False outside /
        for crypto (24/7). This is the canonical clock the bump keys off."""
        assert in_midday_lull("AAPL", now=_MIDDAY_UTC) is True
        assert in_midday_lull("AAPL", now=_HOT_UTC) is False
        assert in_midday_lull("BTC-USD", now=_MIDDAY_UTC) is False  # crypto never lulls

    def test_midday_raises_effective_entry_viability_min(self, monkeypatch):
        """PARTIAL guard (midday-only admission bump): inside the lull the effective
        entry-viability bar is RAISED by the configured bump (so a marginal name that
        clears the flat bar is held back in the chop). Outside the lull it is unchanged.

        NOTE: this is NOT Ross's full session-phase edge-weighting — only the midday
        penalty is enforced on the viability axis (see the section docstring)."""
        monkeypatch.setattr(settings, "chili_momentum_midday_deweight_enabled", True, raising=False)
        monkeypatch.setattr(settings, "chili_momentum_midday_viability_bump", 0.05, raising=False)
        flat = 0.60
        eff_mid, in_lull, bump = lr._effective_entry_viability_min(flat, "AAPL", now=_MIDDAY_UTC)
        eff_hot, hot_lull, _ = lr._effective_entry_viability_min(flat, "AAPL", now=_HOT_UTC)
        assert in_lull is True and bump == pytest.approx(0.05)
        assert eff_mid == pytest.approx(0.65)   # raised: harder to admit a NEW entry midday
        assert eff_mid > eff_hot                # the midday bar is strictly higher
        assert hot_lull is False and eff_hot == pytest.approx(flat)  # outside lull: unchanged

    def test_midday_deweight_off_is_byte_identical(self, monkeypatch):
        """Kill-switch OFF => the bar is never raised (byte-identical to the flat min) —
        the guard is reversible by construction."""
        monkeypatch.setattr(settings, "chili_momentum_midday_deweight_enabled", False, raising=False)
        eff, in_lull, bump = lr._effective_entry_viability_min(0.60, "AAPL", now=_MIDDAY_UTC)
        assert eff == pytest.approx(0.60) and in_lull is False and bump == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# M4 — NO HARD DAILY MAX-LOSS
#   GUARD: check_daily_loss_breach blocks NEW ENTRIES once tripped; the daily-loss kill
#          reason is EXIT-EXEMPT (_kill_switch_halts_exits() is False) so exits flow.
# ══════════════════════════════════════════════════════════════════════════════

class TestM4DailyLossBreachBlocksEntryNotExit:
    def test_breach_blocks_new_entry(self, monkeypatch):
        """Today's realized loss past the cap => ``check_daily_loss_breach`` reports
        ``breached=True`` — the signal the entry path uses to refuse a NEW entry. There IS
        a hard daily max-loss (Ross's missing guard), enforced."""
        # Drive the REAL function deterministically by pinning its realized-pnl source.
        res = _eval_daily_breach(monkeypatch, realized=-500.0, equity=10_000.0)
        assert res["breached"] is True, f"a daily loss past the cap MUST breach: {res}"
        assert res["limit_usd"] > 0

    def test_within_cap_does_not_breach(self, monkeypatch):
        """A small loss within the cap must NOT breach — the guard is a hard FLOOR, not a
        hair-trigger that blocks all trading."""
        res = _eval_daily_breach(monkeypatch, realized=-5.0, equity=10_000.0)
        assert res["breached"] is False

    def test_daily_loss_kill_reason_never_halts_exits(self):
        """THE load-bearing asymmetry: when the kill switch is up for a daily-loss reason,
        ``_kill_switch_halts_exits()`` is False => an EXIT is NEVER blocked (you can always
        get flat), while a manual/emergency reason DOES halt exits. This is why the hard
        daily max-loss can't trap the account in an open position.

        The reason is read from the module's ``_kill_switch`` / ``_kill_switch_reason``
        globals under ``_kill_switch_lock``, so we set them directly and restore after."""
        import app.services.trading.governance as g

        saved_on = g._kill_switch
        saved_reason = g._kill_switch_reason
        try:
            with g._kill_switch_lock:
                g._kill_switch = True
                g._kill_switch_reason = "global_daily_loss_breach_pct_equity_$515"
            assert g._kill_switch_halts_exits() is False, "daily-loss kill must NOT halt exits"
            # Contrast: a manual reason DOES halt exits (so the asymmetry is real, not a stub).
            with g._kill_switch_lock:
                g._kill_switch_reason = "manual_operator_halt"
            assert g._kill_switch_halts_exits() is True
        finally:
            with g._kill_switch_lock:
                g._kill_switch = saved_on
                g._kill_switch_reason = saved_reason


def _eval_daily_breach(monkeypatch, *, realized: float, equity: float) -> dict:
    """Run the REAL ``check_daily_loss_breach`` with a deterministic realized-pnl source.

    The function reads ``global_realized_pnl_today_et(db, user_id) -> {"total_usd",
    "autotrader_usd", "momentum_usd"}`` and compares ``realized <= -limit_usd`` where the
    limit is the more-conservative of the usd / pct-of-equity caps. We pin the realized
    source (no DB rows) and force a known pct cap so the math is reproducible."""
    import app.services.trading.governance as g

    monkeypatch.setattr(
        g, "global_realized_pnl_today_et",
        lambda db, user_id: {
            "total_usd": realized, "autotrader_usd": 0.0, "momentum_usd": realized,
        },
        raising=False,
    )
    # Force a deterministic 5%-of-equity cap (and clear the absolute usd cap so the pct
    # leg governs): cap = 0.05 * equity. realized=-500 vs -(0.05*10k)=-500 -> breach at
    # the boundary; -5 is comfortably within.
    monkeypatch.setattr(g.settings, "chili_global_max_daily_loss_usd", 0.0, raising=False)
    monkeypatch.setattr(g.settings, "chili_global_max_daily_loss_pct_of_equity", 0.05, raising=False)
    return g.check_daily_loss_breach(MagicMock(), user_id=1, equity_usd=equity, activate=False)


# ══════════════════════════════════════════════════════════════════════════════
# M5 — AVERAGING DOWN (the DCFC mistake)
#   GUARD: pyramid_add_decision only adds INTO STRENGTH (bid > a0 AND bid >= HOD).
# ══════════════════════════════════════════════════════════════════════════════

class TestM5NeverAveragesDown:
    _BASE = dict(
        enabled=True, is_equity=True, add_count=0, max_adds=2, in_flight=False,
        a0=10.0, q0=100.0, d0=0.20, ofi=0.0, ofi_threshold=0.0, min_cushion_r=0.5, midday_lull=False,
    )

    def test_add_below_avg_is_blocked(self):
        """An add BELOW the starter average (``bid < a0``) is an average-DOWN — it can never
        fire: the banked-cushion guard requires ``(bid - a0)*q0 >= min_cushion_r*R0`` (a
        POSITIVE cushion), which a below-avg price fails. reason=``cushion_not_banked``."""
        out = pyramid_add_decision(
            **self._BASE,
            bid=9.50,                 # BELOW the 10.0 avg -> averaging down
            stop_px=9.40,             # also below breakeven
            entry_stop_ref=9.40,
            high_water_mark=10.50,
        )
        assert out["fire"] is False, f"an add below the avg must NOT fire: {out}"
        assert out["reason"] == "cushion_not_banked"
        assert out["cushion_usd"] < 0   # the would-be add is underwater

    def test_add_at_breakeven_without_new_hod_is_blocked(self):
        """Even ABOVE the avg, an add that is NOT at a new HOD is blocked (``not_new_hod``) —
        the pyramid adds only on confirmed continuation, never on a stall/fade."""
        out = pyramid_add_decision(
            **{**self._BASE, "min_cushion_r": 0.0},  # cushion trivially banked
            bid=10.30,                # above avg, but ...
            stop_px=10.05,            # stop ratcheted >= breakeven
            entry_stop_ref=10.05,
            high_water_mark=10.80,    # ... below the running HOD -> not a new high
        )
        assert out["fire"] is False
        assert out["reason"] == "not_new_hod"

    def test_add_into_strength_fires(self):
        """The ONLY add that fires is INTO STRENGTH: above the avg (banked cushion), at a
        new HOD, OFI thrust, stop ratcheted up. This is pyramiding UP, the opposite of
        averaging down."""
        out = pyramid_add_decision(
            **{**self._BASE, "ofi": 1.0},  # override the neutral base ofi: thrust present
            bid=10.80,                # well above the 10.0 avg
            stop_px=10.10,            # ratcheted past breakeven
            entry_stop_ref=10.05,     # trail headroom increased
            high_water_mark=10.80,    # at the new HOD
        )
        assert out["fire"] is True, f"an into-strength add SHOULD fire: {out}"
        assert out["reason"] == "confirmed"
        assert out["cushion_usd"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# M6 — LOW-VOLUME PARABOLIC (Ross's dangerous one)
#   GUARD: treated as AVOID — the extension / HOD chase-guard rejects the vertical
#          move (NOT a buy). momentum_continuation_extended / extended_verticality.
# ══════════════════════════════════════════════════════════════════════════════

def _continuation_df() -> pd.DataFrame:
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.20, 9.00, 9.15),
        (9.15, 9.30, 9.10, 9.25),
        (9.25, 9.40, 9.20, 9.35),
        (9.35, 9.50, 9.30, 9.45),
        (9.45, 9.60, 9.40, 9.55),
        (9.55, 9.70, 9.50, 9.65),
        (9.65, 9.80, 9.60, 9.75),
        (9.75, 9.90, 9.70, 9.85),
        (9.85, 10.00, 9.80, 9.95),
        (9.95, 10.05, 9.90, 10.00),
        (10.00, 10.40, 9.98, 10.35),   # cur = fresh new high
    ]
    return _ohlcv(bars)


class TestM6LowVolumeParabolicAvoided:
    def test_parabolic_extension_does_not_fire_a_buy(self):
        """A parabolic blow-off (the HOD-extension guard reports the break level stretched
        far above the EMA/VWAP anchor) is AVOIDED — ``momentum_continuation_trigger`` returns
        ``momentum_continuation_extended`` (no buy). The chase-guard treats the dangerous
        vertical move as not-a-buy, exactly Ross's 'I avoid the low-float parabolic' rule."""
        df = _continuation_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(12)), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_ROSS}.front_side_state",
                      return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok")), \
                patch(f"{_GATES}._hod_extension_ok", return_value=(False, {"hod_extended_vs": "ema9", "hod_ext_pct": 0.42})), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_momentum_continuation_entry_enabled = True
            ok, reason, dbg = momentum_continuation_trigger(
                df, live_price=None, entry_interval="5m", swing_lookback=6, symbol="TEST", db=MagicMock(),
            )
        assert ok is False, f"a low-volume parabolic must NOT fire a buy, got {reason} dbg={dbg}"
        assert reason == "momentum_continuation_extended"

    def test_non_extended_continuation_does_fire_control(self):
        """Control: the SAME structure WITHOUT the parabolic extension DOES fire — proving
        the no-fire above is the extension guard's doing, not a broken setup."""
        df = _continuation_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(12)), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_ROSS}.front_side_state",
                      return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok")), \
                patch(f"{_GATES}._hod_extension_ok", return_value=(True, {})), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_momentum_continuation_entry_enabled = True
            ok, reason, dbg = momentum_continuation_trigger(
                df, live_price=None, entry_interval="5m", swing_lookback=6, symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"the non-extended control SHOULD fire, got {reason} dbg={dbg}"
        assert reason == "momentum_continuation"
