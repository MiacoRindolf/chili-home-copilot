"""Ross-parity L1 (2026-07-25): tape confirm for the NAKED tick-break paths (ORB/ABCD).

The audit found ``orb_break_tick_ok`` / ``abcd_break_tick_ok`` fire on price-thrust alone
while bull_flag / inverse-H&S require ``tape_confirms_hold`` on their tick fires. The
``_tick_break_tape_ok`` wrapper brings ORB/ABCD to the same standard with independent
rollback domains: lever flag OFF -> exact legacy naked behavior; the 12-trigger
``pattern_tape_gate`` rollback -> fail-OPEN here (never newly darkens ORB/ABCD); genuine
dead/thin tape -> fail-CLOSED on the tick path but the detector FALLS THROUGH to its
completed-bar + volume path (degrades to bar entries instead of going dark).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural.captured_paper_selection import (
    captured_paper_candidate_generation_sha256,
)
from app.services.trading.momentum_neural.entry_gates import (
    _tick_break_tape_ok,
    opening_range_breakout_confirmation,
    ross_abcd_confirmation,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
_PROFILE = "app.services.trading.momentum_neural.market_profile"


def _fixed_candidate_generation(
    *,
    debug: dict,
    reason: str,
    setup_family: str,
) -> str:
    return captured_paper_candidate_generation_sha256(
        session_id=1,
        symbol="TEST",
        execution_family="alpaca_spot",
        entry_place_count=1,
        client_order_id="cid-test",
        setup_family=setup_family,
        structural_stop_price=float(debug["pullback_low"]),
        trigger_reason=reason,
        trigger_debug=debug,
        confirmed_arm_marker={"marker": "fixed"},
        viability_updated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        viability_score="0.5",
        viability_payload_sha256="0" * 64,
        execution_readiness_sha256="1" * 64,
    )


# ── helper matrix ────────────────────────────────────────────────────────────

def _enabled_settings() -> SimpleNamespace:
    return SimpleNamespace(chili_momentum_tick_break_tape_confirm_enabled=True)


def test_helper_flag_off_is_open():
    with patch(f"{_GATES}.settings") as ms:
        ms.chili_momentum_tick_break_tape_confirm_enabled = False
        ok, dbg = _tick_break_tape_ok("TEST", db=MagicMock(), settings=ms)
    assert ok is True
    assert dbg["reason"] == "confirm_disabled"


def test_helper_missing_flag_uses_default_on_tape_confirm():
    tape = MagicMock(
        return_value=(False, {"reason": "tape_hold_no_data"})
    )
    with patch(f"{_GATES}.tape_confirms_hold", tape):
        ok, dbg = _tick_break_tape_ok(
            "TEST", db=MagicMock(), settings=SimpleNamespace()
        )
    assert ok is False
    assert dbg["reason"] == "tape_hold_no_data"
    tape.assert_called_once()


def test_helper_tape_confirmed_is_open():
    with patch(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_confirmed"})):
        ok, dbg = _tick_break_tape_ok(
            "TEST", db=MagicMock(), settings=_enabled_settings()
        )
    assert ok is True
    assert dbg["reason"] == "tape_hold_confirmed"


def test_helper_tape_gate_rollback_is_open():
    # pattern_tape_gate rolled back -> its documented dark state governs the 12 legacy
    # triggers ONLY; ORB/ABCD must not newly darken -> fail-OPEN with the explicit reason
    with patch(f"{_GATES}.tape_confirms_hold", return_value=(False, {"reason": "tape_hold_disabled"})):
        ok, dbg = _tick_break_tape_ok(
            "TEST", db=MagicMock(), settings=_enabled_settings()
        )
    assert ok is True
    assert dbg["reason"] == "tape_gate_rolled_back_fail_open"


@pytest.mark.parametrize("reason", ["tape_hold_no_data", "tape_hold_not_confirmed", "tape_hold_error"])
def test_helper_genuine_tape_fail_is_closed(reason):
    with patch(f"{_GATES}.tape_confirms_hold", return_value=(False, {"reason": reason})):
        ok, dbg = _tick_break_tape_ok(
            "TEST", db=MagicMock(), settings=_enabled_settings()
        )
    assert ok is False
    assert dbg["reason"] == reason


def test_helper_exception_is_closed():
    with patch(f"{_GATES}.tape_confirms_hold", side_effect=RuntimeError("boom")):
        ok, dbg = _tick_break_tape_ok(
            "TEST", db=MagicMock(), settings=_enabled_settings()
        )
    assert ok is False
    assert dbg["reason"] == "tick_break_tape_error"


# ── ORB detector integration ─────────────────────────────────────────────────

_ORB_HIGH = 10.02


def _orb_df() -> pd.DataFrame:
    bars = [
        (9.90, 10.02, 9.85),  # 0  OR bar (sets OR-high 10.02 / OR-low 9.85... see below)
        (9.95, 10.00, 9.90),  # 1
        (9.94, 9.98, 9.90),   # 2
        (9.96, 10.00, 9.92),  # 3
        (9.95, 9.99, 9.91),   # 4
        (9.96, 9.98, 9.93),   # 5
        (9.95, 9.99, 9.92),   # 6
        (9.96, 10.00, 9.94),  # 7
        (9.97, 9.99, 9.93),   # 8
        (9.96, 9.98, 9.94),   # 9
        (9.97, 9.99, 9.95),   # 10
        (9.98, 9.99, 9.95),   # 11
        (9.98, 9.99, 9.96),   # 12 cur = NOT broken on the bar (tick path only)
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": (h + l) / 2.0, "Volume": 1_000_000}
            for o, h, l in bars]
    return pd.DataFrame(rows)


def _arrays(n):
    return {
        "volume_ratio": [1.0] * (n - 1) + [3.0],
        "atr": [0.10] * n,
    }


def _orb_settings(ms) -> None:
    ms.chili_momentum_orb_entry_enabled = True
    ms.chili_momentum_orb_minutes = 5
    ms.chili_momentum_orb_window_minutes = 60.0
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5
    ms.chili_momentum_tick_break_tape_confirm_enabled = True


class _OrbPassGuards:
    def __init__(self, arrays=None, mins_since_open=15.0):
        self._arrays = arrays if arrays is not None else _arrays(13)
        self._mins = mins_since_open
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_PROFILE}.minutes_since_regular_open", return_value=self._mins)
        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True)
        _p(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _orb_level(df) -> float:
    # the OR-high the detector derives from the first orb_minutes bars
    return float(df["High"].iloc[0])


class TestOrbTickBreakTape:
    def test_tick_fires_with_confirmed_tape(self):
        df = _orb_df()
        lvl = _orb_level(df)
        with patch(f"{_GATES}.settings") as ms, _OrbPassGuards(), \
                patch(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_confirmed"})):
            _orb_settings(ms)
            ok, reason, dbg = opening_range_breakout_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=lvl + 0.02,
            )
        assert ok is True, f"tick break w/ confirmed tape must fire, got {reason} dbg={dbg}"
        assert reason == "orb_break_tick_ok"
        assert dbg["tape_reason"] == "tape_hold_confirmed"

    def test_dead_tape_blocks_tick_but_bar_path_survives(self):
        # live tick above the level BUT dead tape -> no tick fire; the CURRENT bar has not
        # broken -> waiting_for_break (armed), NOT dark. tape_reason recorded for diagnosis.
        df = _orb_df()
        lvl = _orb_level(df)
        with patch(f"{_GATES}.settings") as ms, _OrbPassGuards(), \
                patch(f"{_GATES}.tape_confirms_hold", return_value=(False, {"reason": "tape_hold_no_data"})):
            _orb_settings(ms)
            ok, reason, dbg = opening_range_breakout_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=lvl + 0.02,
            )
        assert ok is False
        assert reason == "waiting_for_break"
        assert dbg["tape_reason"] == "tape_hold_no_data"

    def test_flag_off_preserves_naked_tick_fire(self):
        df = _orb_df()
        lvl = _orb_level(df)
        with patch(f"{_GATES}.settings") as ms, _OrbPassGuards(), \
                patch(f"{_GATES}.tape_confirms_hold", return_value=(False, {"reason": "tape_hold_no_data"})) as tape:
            _orb_settings(ms)
            ms.chili_momentum_tick_break_tape_confirm_enabled = False
            ok, reason, dbg = opening_range_breakout_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=lvl + 0.02,
            )
        tape.assert_not_called()
        assert ok is True, f"flag OFF must preserve legacy naked tick fire, got {reason}"
        assert reason == "orb_break_tick_ok"
        assert "tape_reason" not in dbg
        assert _fixed_candidate_generation(
            debug=dbg,
            reason=reason,
            setup_family="opening_range_breakout",
        ) == "e5d32cdd6a0473641fb7850e9836a8cbd83c4cee1685ded25c9f543e6871c39b"

    def test_missing_flag_uses_default_on_tape_confirm(self):
        df = _orb_df()
        lvl = _orb_level(df)
        ms = SimpleNamespace()
        _orb_settings(ms)
        delattr(ms, "chili_momentum_tick_break_tape_confirm_enabled")
        with patch(f"{_GATES}.settings", ms), _OrbPassGuards(), \
                patch(f"{_GATES}.tape_confirms_hold", return_value=(False, {"reason": "tape_hold_no_data"})) as tape:
            ok, reason, dbg = opening_range_breakout_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=lvl + 0.02,
            )
        tape.assert_called_once()
        assert ok is False
        assert reason == "waiting_for_break"
        assert dbg["tape_reason"] == "tape_hold_no_data"


# ── ABCD detector integration ────────────────────────────────────────────────
# Fixtures mirror tests/test_momentum_mock_fire_pullback.py exactly (the pivot scanner is
# mocked at the boundary), except the CURRENT bar stays UNDER the 10.10 break level so the
# TICK path is the only fire route.

def _abcd_df() -> pd.DataFrame:
    bars = [
        (9.00, 9.20, 8.95, 9.15),
        (9.15, 9.60, 9.10, 9.55),
        (9.55, 10.00, 9.50, 9.95),   # A region high
        (9.95, 9.98, 9.55, 9.60),    # B low region
        (9.60, 10.05, 9.58, 10.00),  # BC high region (10.10 via pivot mock)
        (10.00, 10.02, 9.70, 9.75),  # C low region
        (9.75, 9.95, 9.72, 9.90),
        (9.90, 10.05, 9.88, 10.00),  # cur: coiled UNDER the 10.10 level (tick path only)
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    return pd.DataFrame(rows)


def _abcd_pivots():
    """A=high 10.00, B=low 9.55, BC=high 10.10, C=low 9.70 (higher low; holds above B)."""
    return [
        {"idx": 1, "price": 9.30, "kind": "L"},
        {"idx": 2, "price": 10.00, "kind": "H"},   # A
        {"idx": 3, "price": 9.55, "kind": "L"},    # B
        {"idx": 4, "price": 10.10, "kind": "H"},   # BC swing high (break level)
        {"idx": 5, "price": 9.70, "kind": "L"},    # C (higher low than B)
    ]


def _abcd_ctx(ms):
    ms.chili_momentum_abcd_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5
    ms.chili_momentum_tick_break_tape_confirm_enabled = True


class TestAbcdTickBreakTape:
    def _run(self, ms, live_price, tape_ret, thrust_ok=True, confirm_on=True):
        _abcd_ctx(ms)
        if confirm_on is None:
            delattr(ms, "chili_momentum_tick_break_tape_confirm_enabled")
        else:
            ms.chili_momentum_tick_break_tape_confirm_enabled = confirm_on
        with patch(f"{_GATES}.settings", ms), \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}._swing_pivots", return_value=_abcd_pivots()), \
                patch(f"{_GATES}._collapse_cap", return_value=0.90), \
                patch(f"{_GATES}.compute_all_from_df", return_value={"volume_ratio": [1.0] * 8}), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}._premarket_tickbreak_confirmed", return_value=thrust_ok), \
                patch(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=thrust_ok), \
                patch(f"{_GATES}.tape_confirms_hold", return_value=tape_ret):
            return ross_abcd_confirmation(
                _abcd_df(), entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=live_price,
            )

    def test_tick_fires_with_confirmed_tape(self):
        with patch(f"{_GATES}.settings") as ms:
            ok, reason, dbg = self._run(ms, live_price=10.15,
                                        tape_ret=(True, {"reason": "tape_hold_confirmed"}))
        assert ok is True, f"ABCD tick w/ confirmed tape must fire, got {reason} dbg={dbg}"
        assert reason == "abcd_break_tick_ok"
        assert dbg["tape_reason"] == "tape_hold_confirmed"

    def test_dead_tape_blocks_tick_falls_to_waiting(self):
        with patch(f"{_GATES}.settings") as ms:
            ok, reason, dbg = self._run(ms, live_price=10.15,
                                        tape_ret=(False, {"reason": "tape_hold_no_data"}))
        assert ok is False
        assert reason == "waiting_for_break"
        assert dbg["tape_reason"] == "tape_hold_no_data"

    def test_thrust_buffer_now_required(self):
        # PR-2 also adds the tick-break family's thrust buffers to ABCD: thrust-fail ->
        # no tick fire even with a hot tape (falls through to the bar path)
        with patch(f"{_GATES}.settings") as ms:
            ok, reason, dbg = self._run(ms, live_price=10.15,
                                        tape_ret=(True, {"reason": "tape_hold_confirmed"}),
                                        thrust_ok=False)
        assert ok is False
        assert reason == "waiting_for_break"

    def test_flag_off_preserves_naked_tick_fire(self):
        with patch(f"{_GATES}.settings") as ms:
            ok, reason, dbg = self._run(ms, live_price=10.15,
                                        tape_ret=(False, {"reason": "tape_hold_no_data"}),
                                        confirm_on=False)
        assert ok is True, f"flag OFF must preserve legacy tick fire, got {reason}"
        assert reason == "abcd_break_tick_ok"
        assert "tape_reason" not in dbg
        assert _fixed_candidate_generation(
            debug=dbg,
            reason=reason,
            setup_family="ross_abcd",
        ) == "876bb2540ca05779eb8c2a3af3d02dfc583f083c8b065944d5a700806f53491f"

    def test_missing_flag_uses_default_on_tick_guards(self):
        ms = SimpleNamespace()
        ok, reason, dbg = self._run(
            ms,
            live_price=10.15,
            tape_ret=(False, {"reason": "tape_hold_no_data"}),
            thrust_ok=False,
            confirm_on=None,
        )
        assert ok is False
        assert reason == "waiting_for_break"
