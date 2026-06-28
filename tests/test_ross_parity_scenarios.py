"""Ross-parity SCENARIOS: encode Ross Cameron's DOCUMENTED small-account-challenge trades
as synthetic frames and assert CHILI's ACTUAL decision functions take the SAME decision.

These are DECISION-PARITY + price-RATIO tests, NEVER dollar/P&L replication. Ross's dollar
figures are single-source marketing — NO Ross dollar amount is asserted anywhere here. We
assert STRUCTURE: which gate fires, the stop = the pullback low (Ross's exact stop rule),
the entry level, the selection rank, and that the anti-pattern (chase / average-down /
revenge) is structurally blocked.

ARCHETYPES (documented Ross trades, used as named geometry — not as $ claims):

  KAVL  — the CLEAN WINNER. A low-float runner whose first 1m candle makes a NEW HIGH after
          a SHALLOW (<50% of the up-leg) pullback on green tape. CHILI's pullback gate must
          FIRE, the stop = the pullback LOW, and a continuation leg pyramids UP into strength.
          Selection ("explosive before pattern"): a low-float / high-RVOL / up>=10% mock must
          out-rank a mega-cap in ``score_universe``.

  DCFC  — the CAUTIONARY trade. RIGHT part: sell INTO the halt-spike strength (a scale fires
          at the target near the high). WRONG part Ross did (FOMO-chased the add too high and
          AVERAGED DOWN) must be BLOCKED: the gate rejects an EXTENDED entry, and the pyramid
          never adds below the entry/avg.

  LGVN  — the DISCIPLINED SMALL LOSS. A choppy tape (no clean trend) => the entry does NOT
          fire; and after a loss there is NO revenge re-entry (the post-loss / reap cooldown
          symbol-guard prevents an immediate re-arm of the same name).

Mirrors the proven scaffolds: the firing-mock pattern from test_momentum_mock_fire_*.py
(settings + indicator layer + chase-guards patched so a clean baseline reaches the fire
path), score_universe pure tests from test_ross_momentum.py, the shared exit helpers from
test_momentum_asymmetric_exit.py, and the seam-patch auto_arm fixture from
test_momentum_auto_arm.py.

TESTS-ONLY — never edits source. Operator runs each file one-at-a-time vs chili_test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import app.services.trading.momentum_neural.auto_arm as aa
from app.services import coinbase_service
from app.services.trading import governance, portfolio_risk
from app.services.trading.momentum_neural import automation_query, operator_actions
from app.services.trading.momentum_neural import paper_execution as pe
from app.services.trading.momentum_neural.entry_gates import (
    first_pullback_break,
    pullback_break_confirmation,
)
from app.services.trading.momentum_neural.paper_execution import pyramid_add_decision
from app.services.trading.momentum_neural.ross_momentum import score_universe

_GATES = "app.services.trading.momentum_neural.entry_gates"
_ROSS = "app.services.trading.momentum_neural.ross_momentum"


# ── shared synthetic helpers (mirrors test_momentum_mock_fire_*.py) ───────────

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
    """Clean front-side arrays: 9>20 EMA, bullish MACD, low VWAP, break-bar volume surge."""
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.30] * n,
        "volume_ratio": [1.0] * (n - 1) + [3.0],
        "atr": [0.20] * n,
    }


def _sig(vol_ratio=None, daily=None, gap=None, float_shares=None, market_cap=None):
    s = {}
    if vol_ratio is not None:
        s["vol_ratio"] = vol_ratio
    if daily is not None:
        s["daily_change_pct"] = daily
    if gap is not None:
        s["gap_pct"] = gap
    if float_shares is not None:
        s["float_shares"] = float_shares
    if market_cap is not None:
        s["market_cap"] = market_cap
    return s


# ══════════════════════════════════════════════════════════════════════════════
#  KAVL ARCHETYPE — the clean winner
# ══════════════════════════════════════════════════════════════════════════════

def _kavl_first_pullback_df() -> pd.DataFrame:
    """KAVL: an up-impulse to a peak, a SHALLOW 2-3 bar pullback holding the 9-EMA, then the
    current 1m bar makes a NEW HIGH above the pullback's prior swing high (Ross's first-
    pullback entry). The pullback LOW is the structural stop."""
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.30, 9.00, 9.25),
        (9.25, 9.55, 9.20, 9.50),
        (9.50, 9.80, 9.45, 9.75),
        (9.75, 10.05, 9.70, 10.00),
        (10.00, 10.20, 9.95, 10.15),   # impulse peak (win_high ~10.20)
        (10.15, 10.18, 10.00, 10.05),
        (10.05, 10.10, 9.95, 10.00),
        (10.00, 10.08, 9.92, 9.98),
        (9.98, 10.05, 9.90, 9.96),     # shallow pullback bar
        (9.96, 10.02, 9.88, 9.95),     # pullback LOW ~9.88 (the stop)
        (9.95, 10.10, 9.93, 10.05),    # last pullback bar -> pb_high ~10.10
        (10.05, 10.40, 10.02, 10.35),  # cur = BREAK new high above pb_high
    ]
    return _ohlcv(bars)


def _fp_settings(ms) -> None:
    ms.chili_momentum_entry_sustained_rvol_floor = 0.0
    ms.chili_momentum_entry_sustain_lookback_bars = 5
    ms.chili_momentum_dipbuy_impulse_accum_min_slope = -1.0
    ms.chili_momentum_dipbuy_distribution_vol_mult = 0.0


class TestKavlCleanWinner:
    def test_selection_explosive_low_float_outranks_megacap(self):
        """'Select explosive before pattern' (Ross's #1 rule). A low-float, high-RVOL,
        up>=10% KAVL-type mock must out-rank a dull mega-cap in ``score_universe`` — CHILI
        ranks the explosive name #1, exactly Ross's leading-gainer focus."""
        res = score_universe({
            "KAVL": _sig(vol_ratio=12.0, daily=45.0, float_shares=3_000_000),
            "MEGA": _sig(vol_ratio=1.1, daily=0.3, market_cap=2_000_000_000_000),
        })
        assert res["KAVL"].rank == 1
        assert res["MEGA"].rank == 2
        assert res["KAVL"].score > res["MEGA"].score

    def test_first_pullback_fires_with_pullback_low_as_stop(self):
        """KAVL's clean first-pullback-into-new-high FIRES. The entry level is the pullback
        HIGH and the structural stop is the pullback LOW (Ross's exact stop rule:
        ``stop == low of the pullback``). debug carries both, stop < entry."""
        df = _kavl_first_pullback_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.95, 0.02, 0.10)), \
                patch(f"{_GATES}._collapse_cap", return_value=0.90), \
                patch(f"{_GATES}._is_first_pullback", return_value=True), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="KAVL", db=MagicMock())
        assert verdict == "FIRE", f"KAVL clean first-pullback must FIRE, got {verdict} dbg={dbg}"
        assert level is not None and stop is not None
        assert dbg["pullback_high"] == pytest.approx(level, abs=1e-6)   # entry = pullback high
        assert dbg["pullback_low"] == pytest.approx(stop, abs=1e-6)     # stop  = pullback low
        assert dbg["pullback_low"] < dbg["pullback_high"]               # Ross's stop rule
        assert dbg.get("pattern") == "first_pullback"

    def test_continuation_pyramids_up_into_strength(self):
        """After the entry, KAVL's continuation leg pyramids INTO strength: with a banked
        cushion (price above the starter avg), a fresh new HOD, and OFI thrust, the pyramid
        ADD fires UP — Ross adds into a winner, never into weakness."""
        out = pyramid_add_decision(
            enabled=True, is_equity=True, add_count=0, max_adds=2, in_flight=False,
            a0=10.05, q0=100.0, d0=0.17,        # starter avg ~ the entry level
            bid=10.60,                          # continuation leg -> above the avg
            stop_px=10.10,                      # stop ratcheted past breakeven
            entry_stop_ref=10.05, high_water_mark=10.60,  # at the new HOD
            ofi=1.0, ofi_threshold=0.0, min_cushion_r=0.5, midday_lull=False,
        )
        assert out["fire"] is True, f"KAVL continuation add SHOULD fire UP: {out}"
        assert out["cushion_usd"] > 0   # the add is into a banked-green runner


# ══════════════════════════════════════════════════════════════════════════════
#  DCFC ARCHETYPE — the cautionary trade
# ══════════════════════════════════════════════════════════════════════════════

class TestDcfcCautionary:
    def test_right_part_scale_into_halt_spike_strength(self):
        """The RIGHT part Ross did on DCFC: sell INTO the halt-resume spike strength. The
        shared scale-out helper (the parity contract both runners call) splits the position
        at the spike target — a scale fires into strength near the high, locking gains. We
        assert the STRUCTURE (a valid split is produced), not any dollar amount."""
        sell_qty, runner, ok = pe.scale_out_quantity(
            current_qty=1.0, original_qty=1.0, fraction=pe.scale_out_fraction(),
        )
        assert ok is True, "a scale into the spike must produce a valid split"
        assert 0.0 < sell_qty < 1.0      # sell PART into strength
        assert runner > 0.0              # hold a runner (Ross keeps a piece)
        # After the partial, the balance stop ratchets to BREAKEVEN (never loosens) — the
        # de-risk that lets the runner ride the post-halt continuation.
        assert pe.breakeven_stop_after_partial(entry_price=10.0, current_stop=9.0) == 10.0

    def test_wrong_part_extended_chase_add_is_rejected(self):
        """The WRONG part Ross did: FOMO-chased the add far ABOVE the anchor after the spike.
        CHILI rejects an EXTENDED entry — a too-deep / stretched pullback returns
        ``pullback_too_deep`` (no fire). The chase is structurally refused."""
        # Impulse, then a DEEP collapse (>50% of the up-leg), then a chase to a new high.
        bars = [
            (9.00, 9.10, 8.95, 9.05),
            (9.05, 9.30, 9.00, 9.25),
            (9.25, 9.55, 9.20, 9.50),
            (9.50, 9.80, 9.45, 9.75),
            (9.75, 10.05, 9.70, 10.00),
            (10.00, 10.20, 9.95, 10.15),   # peak
            (10.15, 10.18, 9.30, 9.40),    # DEEP flush
            (9.40, 9.50, 9.10, 9.20),      # deeper -> pb_low ~9.10
            (9.20, 9.35, 9.05, 9.30),
            (9.30, 9.45, 9.20, 9.40),
            (9.40, 9.60, 9.35, 9.55),
            (9.55, 9.80, 9.50, 9.75),
            (9.75, 11.50, 9.72, 11.45),    # cur = FOMO chase to a huge new high
        ]
        df = _ohlcv(bars)
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.30, 0.02, 0.10)), \
                patch(f"{_GATES}.pullback_ordinal_recent", return_value=1), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")):
            ms.chili_momentum_entry_first_pullback_enabled = False
            ms.chili_momentum_backside_veto_enabled = False
            ms.chili_momentum_candle_quality_multitf_veto_enabled = False
            ms.chili_momentum_red_vol_exhaustion_veto_enabled = False
            ms.chili_momentum_explosive_floor_enabled = False
            ms.chili_momentum_entry_verticality_atr_mult = 0.0
            ms.chili_momentum_entry_macd_open_strict = False
            ok, reason, dbg = pullback_break_confirmation(
                df, entry_interval="1m", symbol="DCFC", db=MagicMock(),
                require_retest=False, require_sustained_volume=False,
                require_break_candle=False, require_vwap_hold=False,
                require_macd_bullish=False, volume_spike_multiple=1.5,
            )
        assert ok is False, f"the DCFC extended chase must NOT fire, got {reason} dbg={dbg}"
        assert reason == "pullback_too_deep"

    def test_wrong_part_never_averages_down(self):
        """The OTHER half of Ross's DCFC mistake: he AVERAGED DOWN as it fell. CHILI's
        pyramid only adds INTO strength — an add BELOW the entry/avg can never fire (the
        banked-cushion guard requires a positive ``(bid - a0)`` cushion). reason=
        ``cushion_not_banked``; the would-be add is underwater."""
        out = pyramid_add_decision(
            enabled=True, is_equity=True, add_count=0, max_adds=2, in_flight=False,
            a0=10.00, q0=100.0, d0=0.20,
            bid=9.40,                  # BELOW the avg -> averaging down
            stop_px=9.30, entry_stop_ref=9.30, high_water_mark=10.50,
            ofi=1.0, ofi_threshold=0.0, min_cushion_r=0.5, midday_lull=False,
        )
        assert out["fire"] is False, f"averaging down must NOT fire: {out}"
        assert out["reason"] == "cushion_not_banked"
        assert out["cushion_usd"] < 0


# ══════════════════════════════════════════════════════════════════════════════
#  LGVN ARCHETYPE — the disciplined small loss (no revenge re-entry)
# ══════════════════════════════════════════════════════════════════════════════

def _lgvn_choppy_df() -> pd.DataFrame:
    """LGVN: a choppy, directionless tape — no clean impulse, the 'pullback' is just noise
    that never resolves into a shallow flag + new-high break. The current bar does NOT make
    a clean new high over a real pullback structure, so the entry must NOT fire."""
    bars = [
        (9.00, 9.20, 8.90, 9.05),
        (9.05, 9.18, 8.92, 8.98),
        (8.98, 9.22, 8.88, 9.12),
        (9.12, 9.16, 8.95, 9.00),
        (9.00, 9.25, 8.90, 9.10),
        (9.10, 9.20, 8.93, 8.97),
        (8.97, 9.24, 8.91, 9.08),
        (9.08, 9.19, 8.96, 9.01),
        (9.01, 9.23, 8.89, 9.06),
        (9.06, 9.17, 8.94, 9.00),
        (9.00, 9.21, 8.92, 9.04),
        (9.04, 9.10, 8.95, 9.02),     # cur: no break of a real pullback high (chop)
    ]
    return _ohlcv(bars)


class TestLgvnDisciplinedLoss:
    def test_choppy_tape_does_not_fire(self):
        """A choppy LGVN tape has no shallow-flag-into-new-high structure, so the entry does
        NOT fire (a wait / decline reason, never a buy). CHILI declines the bad setup — the
        small disciplined loss is AVOIDED by not entering the chop in the first place."""
        df = _lgvn_choppy_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(12)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.50, 0.02, 0.10)), \
                patch(f"{_GATES}.pullback_ordinal_recent", return_value=1), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")):
            ms.chili_momentum_entry_first_pullback_enabled = False
            ms.chili_momentum_backside_veto_enabled = False
            ms.chili_momentum_candle_quality_multitf_veto_enabled = False
            ms.chili_momentum_red_vol_exhaustion_veto_enabled = False
            ms.chili_momentum_explosive_floor_enabled = False
            ms.chili_momentum_entry_verticality_atr_mult = 0.0
            ms.chili_momentum_entry_macd_open_strict = False
            ok, reason, dbg = pullback_break_confirmation(
                df, entry_interval="1m", symbol="LGVN", db=MagicMock(),
                require_retest=False, require_sustained_volume=False,
                require_break_candle=False, require_vwap_hold=False,
                require_macd_bullish=False, volume_spike_multiple=1.5,
            )
        assert ok is False, f"a choppy LGVN tape must NOT fire, got reason={reason} dbg={dbg}"

    def test_no_revenge_reentry_after_loss(self, monkeypatch):
        """After a loss on LGVN, the auto-arm pass must NOT immediately re-arm the SAME name
        — the post-loss / reap symbol-guard (``_symbol_loss_guards`` -> ``loss_blocked``;
        comment in source: 'walk away like Ross does') drops the candidate. No revenge trade.

        Mirrors the seam-patch fixture from test_momentum_auto_arm.py: every seam is pinned
        to the happy path, then the loss-guard is set to block LGVN."""
        # Happy-path seams (the proven auto_arm fixture shape).
        monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
        monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_scheduler_enabled", True, raising=False)
        monkeypatch.setattr(aa.settings, "chili_momentum_live_runner_enabled", True, raising=False)
        monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", 1, raising=False)
        monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
        monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 0)
        monkeypatch.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (False, None))
        monkeypatch.setattr(automation_query, "expire_stale_live_arm_sessions", lambda db, *, user_id: 0)
        monkeypatch.setattr(aa, "_fresh_live_eligible_candidates",
                            lambda db, *, limit: [SimpleNamespace(symbol="LGVN-USD", variant_id=8, viability_score=0.70)])
        monkeypatch.setattr(aa, "_symbol_free", lambda db, sym, uid: True)
        monkeypatch.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
        monkeypatch.setattr(aa, "_candidate_freshness", lambda sym: None)
        monkeypatch.setattr(coinbase_service, "connect", lambda: {"ok": True})
        monkeypatch.setattr(operator_actions, "begin_live_arm",
                            lambda db, **k: {"ok": True, "arm_token": "tok", "session_id": 99})
        monkeypatch.setattr(operator_actions, "confirm_live_arm",
                            lambda db, **k: {"ok": True, "state": "queued_live"})
        # THE REVENGE GUARD: LGVN just took a loss -> it is in the post-loss block set, so the
        # candidate loop must skip it (loss_guard_skipped) and arm NOTHING this pass.
        monkeypatch.setattr(aa, "_symbol_loss_guards", lambda db: ({"LGVN-USD"}, {}))

        out = aa.run_auto_arm_pass(_FakeDB())
        assert out.get("armed", 0) == 0, f"must NOT revenge-re-arm a just-lost name: {out}"
        assert out.get("loss_guard_skipped", 0) >= 1

    def test_reap_cooldown_blocks_immediate_rearm(self):
        """The same name, freshly reaped/churned without firing, is held out by the reap
        cooldown (``_reap_cooldown_active`` after ``_write_reap_cooldown``) — a second guard
        against looping back into a name that just didn't work. Pure in-process check, no DB."""
        sym = "LGVN-USD"
        now = datetime.now(timezone.utc)
        # Not in cooldown before any reap.
        assert aa._reap_cooldown_active(sym, now) is False
        aa._write_reap_cooldown(sym, now)
        # Immediately after a reap -> cooldown ACTIVE -> the pass would skip an immediate re-arm.
        assert aa._reap_cooldown_active(sym, now) is True


class _FakeDB:
    """Minimal DB stand-in for run_auto_arm_pass (every real DB seam is patched out)."""

    def add(self, *_a, **_k) -> None:
        pass

    def commit(self) -> None:
        pass

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return []
