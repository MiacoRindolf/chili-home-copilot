"""Phase 8: live automation runner (guarded Coinbase adapter path)."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import inspect
import re
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_BAILOUT,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    assert_transition_live,
    can_transition_live,
)
from app.services.trading.momentum_neural.live_runner import (
    _NON_STRUCTURAL_ENTRY_TRIGGER_REASONS,
    _STRUCTURAL_STOP_TRIGGER_REASONS,
    _adaptive_live_max_spread_bps,
    _boundary_risk_block_payload,
    _c1_iqfeed_phantom_loss,
    _entry_candidate_event_payload,
    _entry_candidate_budget_stop_distance,
    _entry_client_order_id,
    _entry_trace_event_payload,
    _expected_move_bps_from_ohlcv,
    _is_ross_tick_tape_entry,
    _limit_entry_spread_ceiling_bps,
    _notional_guard_multiplier,
    _pre_submit_ross_universe_block,
    _pre_submit_stale_path_block,
    _quote_quality_block,
    _ross_instant_bid_cut_suppressed,
    _ross_live_entry_shape_block,
    _ross_live_pre_candidate_shape_block,
    _ross_tick_profile_signal,
    _ross_transcript_starter_signal,
    _ross_scalp_time_floor_bound_s,
    _reset_rejected_tick_scalp_fire,
    _select_entry_trigger_frame,
    _should_surface_tick_watch_wait,
    list_runnable_live_sessions,
    summarize_live_execution,
    tick_live_session,
)
from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import (
    TAPE_HOLD_VALID_WAIT_REASONS,
    TICK_ARMED_WAIT_REASONS,
    absorption_snap_entry,
    ask_thins_dip_entry,
    blue_sky_break_confirmation,
    bull_flag_confirmation,
    cup_and_handle_confirmation,
    ma_vwap_pullback_confirmation,
    premarket_pivot_macd_entry,
    round_number_entry_context,
    tape_confirmed_hold_trigger,
    wedge_break_entry,
)
from app.services.trading.momentum_neural.daily_levels import DailyContext
from app.services.trading.momentum_neural.paper_runner import list_runnable_paper_sessions
from app.services.trading.momentum_neural.risk_policy import (
    RISK_SNAPSHOT_KEY,
    _A_SETUP_SIZE_FLOOR_TRIGGER_REASONS,
    apply_a_setup_combined_size_floor,
    apply_a_setup_notional_floor_budget,
)
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder, NormalizedProduct, NormalizedTicker

from tests.test_momentum_paper_runner import _seed_live_eligible_row


def test_entry_client_order_id_is_retry_stable_and_cycle_unique() -> None:
    first = _entry_client_order_id(
        session_id=10343,
        correlation_id="tc-live-session",
        trade_cycles=1,
        stopout_cycles=0,
        place_n=1,
    )
    retry = _entry_client_order_id(
        session_id=10343,
        correlation_id="tc-live-session",
        trade_cycles=1,
        stopout_cycles=0,
        place_n=1,
    )
    next_cycle = _entry_client_order_id(
        session_id=10343,
        correlation_id="tc-live-session",
        trade_cycles=2,
        stopout_cycles=0,
        place_n=1,
    )
    stopout_cycle = _entry_client_order_id(
        session_id=10343,
        correlation_id="tc-live-session",
        trade_cycles=2,
        stopout_cycles=1,
        place_n=1,
    )

    assert retry == first
    assert next_cycle != first
    assert stopout_cycle != next_cycle
    assert len(first) <= 120


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"LiveRun_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _variant_id_for_live_test(db: Session, *, symbol: str = "SOL-USD") -> int:
    from app.models.trading import MomentumStrategyVariant

    variant = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").first()
    if variant is not None:
        return int(variant.id)
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    return int(vid)


def _fresh() -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


def test_structural_setup_aliases_have_a_setup_floor_coverage() -> None:
    missing = (
        set(_STRUCTURAL_STOP_TRIGGER_REASONS)
        - set(_A_SETUP_SIZE_FLOOR_TRIGGER_REASONS)
        - set(_NON_STRUCTURAL_ENTRY_TRIGGER_REASONS)
    )
    assert missing == set()


def test_emitted_entry_fire_aliases_have_stop_and_floor_coverage() -> None:
    source = inspect.getsource(entry_gates)
    emitted = {
        reason
        for reason in re.findall(r"return\s+True,\s+[\"']([^\"']+)[\"']", source)
    }
    for yes_reason, no_reason in re.findall(
        r"return\s+True,\s+\([\"']([^\"']+)[\"']\s+if.*?else\s+[\"']([^\"']+)[\"']\)",
        source,
    ):
        emitted.update({yes_reason, no_reason})

    entry_family_tokens = (
        "break",
        "reclaim",
        "pullback",
        "dip",
        "vwap",
        "hod",
        "flat_top",
        "blue_sky",
        "abcd",
        "bottom",
        "shoulders",
        "cup",
        "orb",
        "red_to_green",
        "flag",
        "wedge",
        "absorption",
        "trap",
        "roc",
        "pivot",
        "starter",
        "continuation",
        "wick",
        "micro",
    )
    internal_or_context_true_reasons = {
        # Internal pullback evaluator results are normalized by
        # pullback_break_confirmation before live entry telemetry is emitted.
        "raw_break",
        "break_retest",
        # Tape hold and halt-add helpers emit public live aliases elsewhere.
        "tape_hold_ok",
        "add_into_halt_ok",
    }
    entry_like = {
        reason
        for reason in emitted
        if any(token in reason for token in entry_family_tokens)
        and not reason.startswith(("waiting_", "round_number_", "rth_"))
        and reason not in internal_or_context_true_reasons
    }

    uncovered_stop = (
        entry_like
        - set(_STRUCTURAL_STOP_TRIGGER_REASONS)
        - set(_NON_STRUCTURAL_ENTRY_TRIGGER_REASONS)
    )
    uncovered_floor = (
        entry_like
        - set(_A_SETUP_SIZE_FLOOR_TRIGGER_REASONS)
        - set(_NON_STRUCTURAL_ENTRY_TRIGGER_REASONS)
    )

    assert uncovered_stop == set()
    assert uncovered_floor == set()


def test_live_runner_direct_entry_trigger_aliases_have_stop_and_floor_coverage() -> None:
    """Direct live-runner aliases bypass the entry_gates return-value scan."""

    source = inspect.getsource(tick_live_session)
    tree = ast.parse(source)
    aliases: set[str] = set()

    def subscript_key(node: ast.AST) -> str | None:
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            return str(node.slice.value)
        return None

    def collect_string_literals(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            aliases.add(node.value)
            return
        for child in ast.iter_child_nodes(node):
            collect_string_literals(child)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(subscript_key(target) == "entry_trigger_reason" for target in node.targets):
            continue
        collect_string_literals(node.value)

    aliases = {
        alias
        for alias in aliases
        if alias
        and alias != "entry_trigger_reason"
        and not alias.startswith(("previous_", "source_", "blocked_"))
    }
    assert aliases

    uncovered_stop = (
        aliases
        - set(_STRUCTURAL_STOP_TRIGGER_REASONS)
        - set(_NON_STRUCTURAL_ENTRY_TRIGGER_REASONS)
    )
    uncovered_floor = (
        aliases
        - set(_A_SETUP_SIZE_FLOOR_TRIGGER_REASONS)
        - set(_NON_STRUCTURAL_ENTRY_TRIGGER_REASONS)
    )

    assert uncovered_stop == set()
    assert uncovered_floor == set()


def test_actionable_wait_reasons_have_tick_and_tape_coverage_contract() -> None:
    source = inspect.getsource(entry_gates)
    emitted_waits = {
        reason
        for reason in re.findall(
            r"[\"'](waiting_for_[A-Za-z0-9_]+|ross_breakout_starter_waiting_for_[A-Za-z0-9_]+)[\"']",
            source,
        )
    }
    assert emitted_waits <= set(TICK_ARMED_WAIT_REASONS)
    assert set(TAPE_HOLD_VALID_WAIT_REASONS) <= set(TICK_ARMED_WAIT_REASONS)
    assert not (
        {
            "ross_breakout_starter_waiting_for_level",
            "ross_breakout_starter_waiting_for_push",
        }
        & set(TAPE_HOLD_VALID_WAIT_REASONS)
    )


def _frontside_tape_hold_df():
    import pandas as pd

    rows = [
        ("2026-07-01 11:56:00", 10.00, 10.18, 9.96, 10.12, 100_000),
        ("2026-07-01 11:56:15", 10.12, 10.28, 10.08, 10.24, 112_000),
        ("2026-07-01 11:56:30", 10.24, 10.42, 10.20, 10.38, 128_000),
        ("2026-07-01 11:56:45", 10.38, 10.55, 10.32, 10.48, 135_000),
        ("2026-07-01 11:57:00", 10.48, 10.70, 10.42, 10.63, 150_000),
        ("2026-07-01 11:57:15", 10.63, 10.82, 10.55, 10.76, 170_000),
        ("2026-07-01 11:57:30", 10.76, 10.92, 10.62, 10.68, 145_000),
        ("2026-07-01 11:57:45", 10.68, 10.78, 10.50, 10.58, 132_000),
        ("2026-07-01 11:58:00", 10.58, 10.74, 10.46, 10.66, 138_000),
        ("2026-07-01 11:58:15", 10.66, 10.88, 10.60, 10.82, 158_000),
        ("2026-07-01 11:58:30", 10.82, 10.96, 10.74, 10.90, 176_000),
        ("2026-07-01 11:58:45", 10.90, 11.02, 10.84, 10.98, 190_000),
    ]
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts")


def test_tape_hold_structural_leg_fires_on_frontside_ema_hold() -> None:
    ok, reason, debug = tape_confirmed_hold_trigger(
        _frontside_tape_hold_df(),
        pullback_high=11.02,
        pullback_low=10.46,
        live_price=10.99,
        entry_interval="15s",
    )

    assert ok is True, (reason, debug)
    assert reason == "tape_hold_ok"
    assert debug["reason"] == "tape_hold_ok"
    assert debug["pullback_high"] == 11.02
    assert debug["pullback_low"] == 10.46
    assert debug["above_vwap"] is True


def test_tape_hold_structural_leg_fails_closed_without_pullback_low() -> None:
    ok, reason, debug = tape_confirmed_hold_trigger(
        _frontside_tape_hold_df(),
        pullback_high=11.02,
        pullback_low=None,
        live_price=10.99,
        entry_interval="15s",
    )

    assert ok is False
    assert reason == "tape_hold_struct_wait"
    assert debug["reason"] == "no_structural_low"


def test_tape_hold_structural_leg_fails_closed_on_backside(monkeypatch) -> None:
    from app.services.trading.momentum_neural import ross_momentum

    monkeypatch.setattr(
        ross_momentum,
        "front_side_state",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_backside=True,
            reason="backside_fade",
            above_vwap=False,
        ),
    )

    ok, reason, debug = tape_confirmed_hold_trigger(
        _frontside_tape_hold_df(),
        pullback_high=11.02,
        pullback_low=10.46,
        live_price=10.99,
        entry_interval="15s",
    )

    assert ok is False
    assert reason == "tape_hold_backside"
    assert debug["reason"] == "front_side_backside"


def _ma_vwap_pullback_df(*, collapse: bool = False):
    import pandas as pd

    closes = [
        10.00,
        10.05,
        10.10,
        10.15,
        10.22,
        10.35,
        10.52,
        10.70,
        10.88,
        11.05,
        11.14,
        10.78 if not collapse else 10.22,
        11.02,
    ]
    rows = []
    for i, close in enumerate(closes):
        if i in (8, 9, 10, 12):
            open_px = close - 0.08
        elif i == 11:
            open_px = close + 0.06
        else:
            open_px = close - 0.01
        low = close - (0.16 if i == 11 else 0.08)
        rows.append(
            {
                "Open": open_px,
                "High": close + 0.08,
                "Low": low,
                "Close": close,
                "Volume": 220_000 if i == 12 else 100_000,
            }
        )
    return pd.DataFrame(
        rows,
        index=pd.date_range("2026-07-01 14:00:00", periods=len(rows), freq="15s", tz="UTC"),
    )


def test_ma_vwap_pullback_fires_tick_reclaim_with_structural_stop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_ma_vwap_pullback_enabled", True)

    ok, reason, debug = ma_vwap_pullback_confirmation(
        _ma_vwap_pullback_df(),
        entry_interval="15s",
        live_price=11.03,
        symbol="JEM",
    )

    assert ok is True, (reason, debug)
    assert reason == "ma_vwap_pullback_tick_ok"
    assert debug["support"] == "9ema"
    assert debug["tick_break"] is True
    assert debug["pullback_high"] > debug["pullback_low"] > 0
    assert debug["reclaim_level"] == pytest.approx(debug["pullback_high"])


def test_ma_vwap_pullback_refuses_collapse(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_ma_vwap_pullback_enabled", True)

    ok, reason, debug = ma_vwap_pullback_confirmation(
        _ma_vwap_pullback_df(collapse=True),
        entry_interval="15s",
        live_price=11.03,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "ma_vwap_pullback_collapse"
    assert debug["depth"] > debug["collapse_cap"]


def test_ma_vwap_pullback_refuses_backside_lifecycle(monkeypatch) -> None:
    from app.services.trading.momentum_neural import ross_momentum

    monkeypatch.setattr(settings, "chili_momentum_ma_vwap_pullback_enabled", True)
    monkeypatch.setattr(
        ross_momentum,
        "front_side_state",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_backside=True,
            reason="backside_fade",
            above_vwap=False,
        ),
    )

    ok, reason, debug = ma_vwap_pullback_confirmation(
        _ma_vwap_pullback_df(),
        entry_interval="15s",
        live_price=11.03,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "ma_vwap_pullback_backside_lifecycle"
    assert debug["front_side_state"] == "backside_fade"


def _bull_flag_df(*, shallow: bool = False):
    import pandas as pd

    base = 10.50
    pull_low = 10.96 if shallow else 10.82
    closes = [
        base,
        base + 0.02,
        base + 0.04,
        base + 0.06,
        base + 0.08,
        base + 0.12,
        base + 0.20,
        10.75,
        10.92,
        11.08,
        11.20,
        10.94,
        10.88,
        11.04,
    ]
    rows = []
    for i, close in enumerate(closes):
        if i in (7, 8, 9, 10, 13):
            open_px = close - 0.08
        elif i in (11, 12):
            open_px = close + 0.06
        else:
            open_px = close - 0.01
        volume = (
            500_000
            if i in (7, 8, 9, 10)
            else 50_000
            if i in (11, 12)
            else 600_000
            if i == 13
            else 100_000
        )
        low = close - 0.08
        if i == 12:
            low = pull_low
        rows.append(
            {
                "Open": open_px,
                "High": close + 0.08,
                "Low": low,
                "Close": close,
                "Volume": volume,
            }
        )
    return pd.DataFrame(
        rows,
        index=pd.date_range("2026-07-01 14:00:00", periods=len(rows), freq="15s", tz="UTC"),
    )


def test_bull_flag_fires_tick_break_with_structural_stop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_bull_flag_entry_enabled", True)
    monkeypatch.setattr(
        entry_gates,
        "tape_confirms_hold",
        lambda *_args, **_kwargs: (
            True,
            {
                "reason": "tape_hold_confirmed",
                "signed_tape_accel": 1.0,
                "tick_rate": 2.0,
                "tick_rate_floor": 1.0,
            },
        ),
    )

    ok, reason, debug = bull_flag_confirmation(
        _bull_flag_df(),
        entry_interval="15s",
        live_price=11.40,
        symbol="JEM",
    )

    assert ok is True
    assert reason == "bull_flag_break_tick_ok"
    assert debug["tick_break"] is True
    assert debug["pullback_high"] > debug["pullback_low"] > 0
    assert debug["pullback_dryup_ratio"] < 0.85
    assert debug["tape_reason"] == "tape_hold_confirmed"


def test_bull_flag_refuses_shallow_first_pullback_geometry(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_bull_flag_entry_enabled", True)
    monkeypatch.setattr(entry_gates, "tape_confirms_hold", lambda *_args, **_kwargs: (True, {"reason": "tape_hold_confirmed"}))

    ok, reason, debug = bull_flag_confirmation(
        _bull_flag_df(shallow=True),
        entry_interval="15s",
        live_price=11.40,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "bull_flag_too_shallow_is_first_pullback"
    assert debug["bull_flag_retrace"] <= debug["bull_flag_floor"]


def test_bull_flag_requires_confirming_tape(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_bull_flag_entry_enabled", True)
    monkeypatch.setattr(
        entry_gates,
        "tape_confirms_hold",
        lambda *_args, **_kwargs: (
            False,
            {
                "reason": "tape_hold_not_confirmed",
                "signed_tape_accel": -1.0,
                "tick_rate": 1.0,
                "tick_rate_floor": 1.0,
            },
        ),
    )

    ok, reason, debug = bull_flag_confirmation(
        _bull_flag_df(),
        entry_interval="15s",
        live_price=11.40,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "bull_flag_tape_unconfirmed"
    assert debug["tape_reason"] == "tape_hold_not_confirmed"


def _blue_sky_df(*, wide_base: bool = False):
    import pandas as pd

    closes = [
        10.00,
        10.10,
        10.20,
        10.35,
        10.50,
        10.70,
        10.90,
        11.05,
        11.10,
        11.12,
        11.11,
        11.13,
        11.12,
        11.14,
    ]
    rows = []
    for i, close in enumerate(closes):
        open_px = close - 0.03 if i < 10 or i == 13 else close + 0.01
        high = close + 0.03
        low = close - (0.35 if wide_base and i == 11 else 0.03)
        volume = 250_000 if i == 13 else 120_000 if i >= 10 else 100_000
        rows.append(
            {
                "Open": open_px,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": volume,
            }
        )
    return pd.DataFrame(
        rows,
        index=pd.date_range("2026-07-01 14:00:00", periods=len(rows), freq="15s", tz="UTC"),
    )


def _clear_sky_ctx() -> DailyContext:
    return DailyContext(
        price=11.0,
        atr=1.0,
        sma_200=6.0,
        dist_to_sma_200_atr=5.0,
        dist_to_resistance_atr=None,
        swing_high_nd=None,
        nearest_unfilled_gap_bottom=None,
        rejection_count=0,
        is_blue_sky=True,
    )


def test_blue_sky_break_fires_tick_break_with_structural_stop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_blue_sky_entry_enabled", True)

    ok, reason, debug = blue_sky_break_confirmation(
        _blue_sky_df(),
        entry_interval="15s",
        daily_ctx=_clear_sky_ctx(),
        live_price=11.30,
        symbol="JEM",
    )

    assert ok is True
    assert reason == "blue_sky_break_tick_ok"
    assert debug["tick_break"] is True
    assert debug["is_blue_sky"] is True
    assert debug["pullback_high"] > debug["pullback_low"] > 0


def test_blue_sky_break_refuses_overhead_daily_context(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_blue_sky_entry_enabled", True)
    blocked_ctx = DailyContext(
        price=11.0,
        atr=1.0,
        sma_200=None,
        dist_to_sma_200_atr=None,
        dist_to_resistance_atr=0.25,
        swing_high_nd=11.25,
        nearest_unfilled_gap_bottom=None,
        rejection_count=1,
        is_blue_sky=False,
    )

    ok, reason, debug = blue_sky_break_confirmation(
        _blue_sky_df(),
        entry_interval="15s",
        daily_ctx=blocked_ctx,
        live_price=11.30,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "blue_sky_break_not_clear_sky"
    assert debug["is_blue_sky"] is False


def test_blue_sky_break_refuses_wide_base(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_blue_sky_entry_enabled", True)

    ok, reason, debug = blue_sky_break_confirmation(
        _blue_sky_df(wide_base=True),
        entry_interval="15s",
        daily_ctx=_clear_sky_ctx(),
        live_price=11.30,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "blue_sky_break_base_too_wide"
    assert debug["base_range_pct"] > 0.0


def _wedge_break_df(*, rising: bool = False):
    import pandas as pd

    if rising:
        bars = [
            (10.00, 10.20, 9.70, 10.00, 100_000),
            (10.10, 10.40, 9.85, 10.20, 100_000),
            (10.35, 10.65, 10.05, 10.45, 105_000),
            (10.70, 10.90, 10.45, 10.78, 150_000),
            (10.55, 10.65, 10.10, 10.30, 85_000),
            (10.15, 10.35, 9.85, 10.05, 80_000),
            (10.20, 10.55, 10.05, 10.40, 90_000),
            (10.60, 11.10, 10.45, 10.95, 140_000),
            (10.70, 10.85, 10.30, 10.45, 80_000),
            (10.45, 10.75, 10.15, 10.35, 75_000),
            (10.55, 10.85, 10.35, 10.70, 95_000),
            (10.75, 11.00, 10.55, 10.90, 100_000),
            (10.95, 11.15, 10.75, 11.05, 110_000),
            (11.10, 11.28, 10.95, 11.22, 180_000),
        ]
    else:
        bars = [
            (10.00, 10.20, 9.80, 10.00, 100_000),
            (10.10, 10.40, 9.90, 10.20, 100_000),
            (10.30, 10.70, 10.10, 10.50, 100_000),
            (10.60, 11.00, 10.40, 10.80, 150_000),
            (10.50, 10.70, 10.00, 10.20, 90_000),
            (10.00, 10.30, 9.50, 9.80, 80_000),
            (10.00, 10.50, 9.80, 10.20, 90_000),
            (10.30, 10.80, 10.10, 10.60, 130_000),
            (10.40, 10.60, 10.00, 10.20, 80_000),
            (10.10, 10.40, 9.80, 10.00, 75_000),
            (10.20, 10.50, 10.00, 10.30, 85_000),
            (10.40, 10.65, 10.15, 10.50, 90_000),
            (10.55, 10.70, 10.30, 10.60, 100_000),
            (10.70, 10.95, 10.50, 10.90, 180_000),
        ]

    return pd.DataFrame(
        bars,
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.date_range("2026-07-01 14:00:00", periods=len(bars), freq="15s", tz="UTC"),
    )


def test_wedge_break_fires_tick_break_with_structural_stop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_wedge_break_entry_enabled", True)
    monkeypatch.setattr(
        entry_gates,
        "tape_confirms_hold",
        lambda *_args, **_kwargs: (True, {"reason": "tape_hold_confirmed"}),
    )

    ok, reason, debug = wedge_break_entry(
        _wedge_break_df(),
        entry_interval="15s",
        live_price=10.95,
        symbol="JEM",
    )

    assert ok is True
    assert reason == "wedge_break_tick"
    assert debug["wedge_kind"] == "falling"
    assert debug["tick_break"] is True
    assert debug["pullback_high"] > debug["pullback_low"] > 0
    assert debug["tape_reason"] == "tape_hold_confirmed"


def test_wedge_break_refuses_rising_wedge(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_wedge_break_entry_enabled", True)
    monkeypatch.setattr(entry_gates, "tape_confirms_hold", lambda *_args, **_kwargs: (True, {"reason": "tape_hold_confirmed"}))

    ok, reason, debug = wedge_break_entry(
        _wedge_break_df(rising=True),
        entry_interval="15s",
        live_price=11.25,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "wedge_break_rising_skip"
    assert debug["wedge_kind"] == "rising"


def test_wedge_break_requires_confirming_tape(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_wedge_break_entry_enabled", True)
    monkeypatch.setattr(
        entry_gates,
        "tape_confirms_hold",
        lambda *_args, **_kwargs: (False, {"reason": "tape_hold_not_confirmed"}),
    )

    ok, reason, debug = wedge_break_entry(
        _wedge_break_df(),
        entry_interval="15s",
        live_price=10.95,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "wedge_break_tape_unconfirmed"
    assert debug["tape_reason"] == "tape_hold_not_confirmed"


def _cup_and_handle_df(*, deep_handle: bool = False):
    import pandas as pd

    bars = [
        (9.20, 9.35, 9.10, 9.30, 80_000),
        (9.32, 9.60, 9.28, 9.55, 95_000),
        (9.58, 9.82, 9.45, 9.72, 115_000),
        (9.75, 10.00, 9.65, 9.92, 160_000),  # first rim high
        (9.70, 9.82, 9.45, 9.58, 90_000),
        (9.82, 10.02, 9.72, 9.98, 150_000),  # second rim high
        (9.92, 9.96, 9.86 if not deep_handle else 9.10, 9.90, 70_000),
        (9.94, 9.98, 9.85 if not deep_handle else 9.05, 9.94, 68_000),
        (10.00, 10.18, 9.96, 10.12, 220_000),  # live tick breaks rim
    ]
    return pd.DataFrame(
        bars,
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.date_range("2026-07-01 14:00:00", periods=len(bars), freq="15s", tz="UTC"),
    )


def _patch_cup_and_handle_dependencies(monkeypatch, *, tape_ok: bool = True) -> None:
    from app.services.trading.momentum_neural import ross_momentum

    monkeypatch.setattr(settings, "chili_momentum_cup_and_handle_entry_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_cup_and_handle_lookback_bars", 20)
    monkeypatch.setattr(settings, "chili_momentum_cup_and_handle_max_handle_bars", 3)
    monkeypatch.setattr(entry_gates, "_batch_c_atr_pct", lambda *_args, **_kwargs: (0.02, 0.20))
    monkeypatch.setattr(entry_gates, "_vol_aware_pullback_tolerances", lambda *_args, **_kwargs: (0.08, 0.02, 0.0))
    monkeypatch.setattr(entry_gates, "_collapse_cap", lambda *_args, **_kwargs: 0.12)
    monkeypatch.setattr(
        entry_gates,
        "_swing_pivots",
        lambda *_args, **_kwargs: [
            {"kind": "H", "idx": 3, "price": 10.00},
            {"kind": "H", "idx": 5, "price": 10.02},
        ],
    )
    monkeypatch.setattr(entry_gates, "_detect_back_side", lambda *_args, **_kwargs: (False, "frontside"))
    monkeypatch.setattr(
        ross_momentum,
        "front_side_state",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_backside=False,
            reason="frontside",
            above_vwap=True,
        ),
    )
    monkeypatch.setattr(entry_gates, "_hod_extension_ok", lambda **_kwargs: (True, {"extension_ok": True}))
    monkeypatch.setattr(entry_gates, "_l2_entry_veto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        entry_gates,
        "tape_confirms_hold",
        lambda *_args, **_kwargs: (
            bool(tape_ok),
            {"reason": "tape_hold_confirmed" if tape_ok else "tape_hold_not_confirmed"},
        ),
    )


def test_cup_and_handle_fires_tick_break_with_structural_stop(monkeypatch) -> None:
    _patch_cup_and_handle_dependencies(monkeypatch)

    ok, reason, debug = cup_and_handle_confirmation(
        _cup_and_handle_df(),
        entry_interval="15s",
        live_price=10.08,
        symbol="JEM",
    )

    assert ok is True, (reason, debug)
    assert reason == "cup_and_handle_break_tick_ok"
    assert debug["tick_break"] is True
    assert debug["pullback_high"] == pytest.approx(10.02)
    assert debug["pullback_low"] == pytest.approx(9.85)
    assert debug["pullback_high"] > debug["pullback_low"] > 0
    assert debug["tape_reason"] == "tape_hold_confirmed"


def test_cup_and_handle_refuses_deep_handle(monkeypatch) -> None:
    _patch_cup_and_handle_dependencies(monkeypatch)

    ok, reason, debug = cup_and_handle_confirmation(
        _cup_and_handle_df(deep_handle=True),
        entry_interval="15s",
        live_price=10.08,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "cup_and_handle_handle_too_deep"
    assert debug["handle_depth_pct"] > debug["handle_shallow_cap"] * 100


def test_cup_and_handle_requires_confirming_tape(monkeypatch) -> None:
    _patch_cup_and_handle_dependencies(monkeypatch, tape_ok=False)

    ok, reason, debug = cup_and_handle_confirmation(
        _cup_and_handle_df(),
        entry_interval="15s",
        live_price=10.08,
        symbol="JEM",
    )

    assert ok is False
    assert reason == "cup_and_handle_tape_unconfirmed"
    assert debug["tape_reason"] == "tape_hold_not_confirmed"


def test_round_number_timing_defers_entry_into_overhead(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_round_number_entry_timing_enabled", True)

    ok, reason, debug = round_number_entry_context(
        entry_price=9.98,
        breakout_level=9.99,
        atr_pct=0.005,
    )

    assert ok is False
    assert reason == "round_number_into_overhead"
    assert debug["round_number"] == 10.0
    assert debug["round_number_band"] > 0.0


def test_round_number_timing_permits_break_and_hold_over_level(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_round_number_entry_timing_enabled", True)

    ok, reason, debug = round_number_entry_context(
        entry_price=9.99,
        breakout_level=10.02,
        atr_pct=0.005,
    )

    assert ok is True
    assert reason == "round_number_break_and_hold"
    assert debug["round_number"] == 10.0
    assert debug["round_number_held"] is True


def test_round_number_timing_permits_when_no_nearby_overhead(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_round_number_entry_timing_enabled", True)

    ok, reason, debug = round_number_entry_context(
        entry_price=10.06,
        breakout_level=10.08,
        atr_pct=0.005,
    )

    assert ok is True
    assert reason == "round_number_not_overhead"
    assert debug["round_number"] == 10.5


def test_round_number_timing_disabled_permits_without_dark_block(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_round_number_entry_timing_enabled", False)

    ok, reason, debug = round_number_entry_context(
        entry_price=9.98,
        breakout_level=9.99,
        atr_pct=0.005,
    )

    assert ok is True
    assert reason == "round_number_disabled"
    assert debug == {}


def _absorption_snap_df():
    import pandas as pd

    closes = [
        10.00,
        10.05,
        10.10,
        10.20,
        10.32,
        10.45,
        10.58,
        10.62,
        10.60,
        10.58,
        10.61,
        10.63,
        10.65,
        10.82,
    ]
    rows = []
    for i, close in enumerate(closes):
        open_px = close - 0.03 if i < 8 or i == 13 else close + 0.02
        high = close + 0.05
        low = close - 0.05
        if i in (7, 8, 9, 10, 11, 12):
            high = 10.70
        if i in (8, 9, 10, 11, 12):
            low = 10.48 + 0.01 * (i - 8)
        if i == 13:
            high = 10.90
            low = 10.66
        rows.append(
            {
                "Open": open_px,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": 180_000 if i == 13 else 100_000,
            }
        )
    return pd.DataFrame(
        rows,
        index=pd.date_range("2026-07-01 14:00:00", periods=len(rows), freq="15s", tz="UTC"),
    )


class _FakeMappingResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeL2Db:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return _FakeMappingResult(self.rows)


class _FakeL2AndTradeDb:
    def __init__(self, *, depth_rows: list[dict], trade_rows: list[dict]) -> None:
        self.depth_rows = depth_rows
        self.trade_rows = trade_rows

    def execute(self, statement, *_args, **_kwargs):
        sql = str(statement)
        if "iqfeed_trade_ticks" in sql:
            return _FakeMappingResult(self.trade_rows)
        return _FakeMappingResult(self.depth_rows)


def test_ladder_distribution_reader_requires_fresh_depth(monkeypatch) -> None:
    from datetime import timedelta
    from app.services.trading.momentum_neural.pipeline import read_ladder_distribution

    monkeypatch.setattr(settings, "chili_momentum_l2_snapshot_max_age_seconds", 5.0)
    now = datetime(2026, 7, 1, 14, 0, 5)
    fresh_rows = [
        {
            "observed_at": now - timedelta(seconds=1),
            "bid_top": 10.69,
            "ask_top": 10.70,
            "bid5_size": 120_000,
            "ask5_size": 80_000,
            "imbalance5": 0.20,
        },
        {
            "observed_at": now - timedelta(seconds=2),
            "bid_top": 10.68,
            "ask_top": 10.70,
            "bid5_size": 95_000,
            "ask5_size": 90_000,
            "imbalance5": 0.03,
        },
        {
            "observed_at": now - timedelta(seconds=3),
            "bid_top": 10.67,
            "ask_top": 10.70,
            "bid5_size": 80_000,
            "ask5_size": 120_000,
            "imbalance5": -0.20,
        },
    ]

    ladder = read_ladder_distribution("JEM", db=_FakeL2Db(fresh_rows), as_of=now)
    thin = read_ladder_distribution("JEM", db=_FakeL2Db(fresh_rows[:1]), as_of=now)
    stale = read_ladder_distribution(
        "JEM",
        db=_FakeL2Db([{**fresh_rows[0], "observed_at": now - timedelta(seconds=10)}]),
        as_of=now,
    )

    assert ladder is not None
    assert ladder.n_snaps == 3
    assert ladder.snapshot_age_s == pytest.approx(1.0)
    assert ladder.ofi == pytest.approx(0.40)
    assert ladder.depth_imbal_pctile == pytest.approx(1.0)
    assert thin is None
    assert stale is None


def test_ladder_distribution_reader_detects_target_ask_eaten(monkeypatch) -> None:
    from datetime import timedelta
    from app.services.trading.momentum_neural.pipeline import read_ladder_distribution

    monkeypatch.setattr(settings, "chili_momentum_l2_snapshot_max_age_seconds", 5.0)
    monkeypatch.setattr(settings, "chili_momentum_l2_distribution_min_snaps", 3)
    monkeypatch.setattr(settings, "chili_momentum_l2_ask_eaten_pctile_ceiling", 0.50)
    now = datetime(2026, 7, 1, 14, 0, 5)
    rows = [
        {
            "observed_at": now - timedelta(seconds=1),
            "bids_json": [[10.69, 160_000], [10.68, 40_000]],
            "asks_json": [[10.70, 20_000], [10.71, 20_000]],
        },
        {
            "observed_at": now - timedelta(seconds=2),
            "bids_json": [[10.69, 130_000], [10.68, 40_000]],
            "asks_json": [[10.70, 60_000], [10.71, 20_000]],
        },
        {
            "observed_at": now - timedelta(seconds=3),
            "bids_json": [[10.69, 80_000], [10.68, 30_000]],
            "asks_json": [[10.70, 100_000], [10.71, 20_000]],
        },
    ]

    ladder = read_ladder_distribution("JEM", db=_FakeL2Db(rows), as_of=now, target_level=10.70)

    assert ladder is not None
    assert ladder.ask_build < 0.0
    assert ladder.ofi > 0.0
    assert ladder.ask_eaten_confirmed is True
    assert ladder.ask_eaten_frac == pytest.approx(0.80)
    assert ladder.ask_eaten_pctile == pytest.approx(1 / 3)


def test_ladder_distribution_reader_detects_target_bid_refill(monkeypatch) -> None:
    from datetime import timedelta
    from app.services.trading.momentum_neural.pipeline import read_ladder_distribution

    monkeypatch.setattr(settings, "chili_momentum_l2_snapshot_max_age_seconds", 5.0)
    monkeypatch.setattr(settings, "chili_momentum_l2_distribution_min_snaps", 3)
    monkeypatch.setattr(settings, "chili_momentum_l2_bid_refill_pctile_floor", 0.50)
    now = datetime(2026, 7, 1, 14, 0, 5)
    rows = [
        {
            "observed_at": now - timedelta(seconds=1),
            "bids_json": [[10.50, 120_000], [10.49, 40_000]],
            "asks_json": [[10.70, 40_000], [10.71, 20_000]],
        },
        {
            "observed_at": now - timedelta(seconds=2),
            "bids_json": [[10.50, 80_000], [10.49, 40_000]],
            "asks_json": [[10.70, 50_000], [10.71, 20_000]],
        },
        {
            "observed_at": now - timedelta(seconds=3),
            "bids_json": [[10.50, 30_000], [10.49, 30_000]],
            "asks_json": [[10.70, 60_000], [10.71, 20_000]],
        },
    ]

    ladder = read_ladder_distribution("JEM", db=_FakeL2Db(rows), as_of=now, target_level=10.50)

    assert ladder is not None
    assert ladder.bid_refill > 0.0
    assert ladder.depth_imbal_pctile == pytest.approx(1.0)
    assert ladder.bid_refill_confirmed is True
    assert ladder.bid_refill_frac == pytest.approx(3.0)
    assert ladder.bid_refill_pctile == pytest.approx(1.0)


def test_target_level_trade_prints_attribute_ask_lifts(monkeypatch) -> None:
    from datetime import timedelta
    from app.services.trading.momentum_neural.pipeline import read_target_level_trade_prints

    monkeypatch.setattr(settings, "chili_momentum_l2_target_print_window_seconds", 15.0)
    now = datetime(2026, 7, 1, 14, 0, 5)
    rows = [
        {
            "observed_at": now - timedelta(seconds=3),
            "price": 10.68,
            "size": 1_000,
            "bid": 10.68,
            "ask": 10.70,
        },
        {
            "observed_at": now - timedelta(seconds=2),
            "price": 10.70,
            "size": 4_000,
            "bid": 10.69,
            "ask": 10.70,
        },
        {
            "observed_at": now - timedelta(seconds=1),
            "price": 10.705,
            "size": 6_000,
            "bid": 10.69,
            "ask": 10.70,
        },
    ]

    prints = read_target_level_trade_prints(
        "JEM",
        db=_FakeL2Db(rows),
        target_level=10.70,
        as_of=now,
    )

    assert prints is not None
    assert prints.ask_lift_confirmed is True
    assert prints.n_prints == 3
    assert prints.ask_lift_volume == pytest.approx(10_000)
    assert prints.target_print_volume == pytest.approx(10_000)
    assert prints.ask_lift_ratio == pytest.approx(10_000 / 11_000)
    assert prints.latest_print_age_s == pytest.approx(1.0)


def test_absorption_snap_fires_with_fresh_l2_and_tape(monkeypatch) -> None:
    import app.services.trading.momentum_neural.pipeline as pipeline_mod

    monkeypatch.setattr(settings, "chili_momentum_absorption_snap_entry_enabled", True)
    monkeypatch.setattr(
        pipeline_mod,
        "read_ladder_distribution",
        lambda *_args, **_kwargs: SimpleNamespace(
            n_snaps=5,
            ofi=0.50,
            ask_build=0.20,
            micro_edge=1.0,
            depth_imbal_pctile=0.80,
            snapshot_age_s=0.5,
        ),
    )
    monkeypatch.setattr(entry_gates, "_l2_entry_veto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entry_gates, "tape_confirms_hold", lambda *_args, **_kwargs: (True, {"reason": "tape_hold_confirmed"}))

    ok, reason, debug = absorption_snap_entry(
        _absorption_snap_df(),
        entry_interval="15s",
        live_price=10.86,
        symbol="JEM",
        db=object(),
    )

    assert ok is True
    assert reason == "absorption_snap_tick"
    assert debug["pullback_high"] == pytest.approx(10.70)
    assert debug["pullback_low"] == pytest.approx(10.49)
    assert debug["ofi"] == pytest.approx(0.50)
    assert debug["ask_build"] == pytest.approx(0.20)
    assert debug["tape_reason"] == "tape_hold_confirmed"


def test_absorption_snap_fires_when_target_ask_is_eaten_even_if_aggregate_ask_build_shrinks(monkeypatch) -> None:
    from datetime import timedelta

    monkeypatch.setattr(settings, "chili_momentum_absorption_snap_entry_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_l2_snapshot_max_age_seconds", 5.0)
    monkeypatch.setattr(settings, "chili_momentum_l2_distribution_min_snaps", 3)
    monkeypatch.setattr(entry_gates, "_l2_entry_veto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entry_gates, "tape_confirms_hold", lambda *_args, **_kwargs: (True, {"reason": "tape_hold_confirmed"}))

    now = datetime(2026, 7, 1, 14, 0, 5)
    rows = [
        {
            "observed_at": now - timedelta(seconds=1),
            "bids_json": [[10.69, 160_000], [10.68, 40_000]],
            "asks_json": [[10.70, 20_000], [10.71, 20_000]],
        },
        {
            "observed_at": now - timedelta(seconds=2),
            "bids_json": [[10.69, 130_000], [10.68, 40_000]],
            "asks_json": [[10.70, 60_000], [10.71, 20_000]],
        },
        {
            "observed_at": now - timedelta(seconds=3),
            "bids_json": [[10.69, 80_000], [10.68, 30_000]],
            "asks_json": [[10.70, 100_000], [10.71, 20_000]],
        },
    ]
    trades = [
        {
            "observed_at": now - timedelta(seconds=2),
            "price": 10.70,
            "size": 4_000,
            "bid": 10.69,
            "ask": 10.70,
        },
        {
            "observed_at": now - timedelta(seconds=1),
            "price": 10.705,
            "size": 6_000,
            "bid": 10.69,
            "ask": 10.70,
        },
    ]

    ok, reason, debug = absorption_snap_entry(
        _absorption_snap_df(),
        entry_interval="15s",
        live_price=10.86,
        symbol="JEM",
        db=_FakeL2AndTradeDb(depth_rows=rows, trade_rows=trades),
        l2_as_of=now,
    )

    assert ok is True, (reason, debug)
    assert reason == "absorption_snap_tick"
    assert debug["ask_build"] < 0.0
    assert debug["ask_eaten_confirmed"] is True
    assert debug["ask_eaten_frac"] == pytest.approx(0.80)
    assert debug["ask_lift_print_confirmed"] is True
    assert debug["ask_lift_volume"] == pytest.approx(10_000)
    assert debug["target_print_ratio"] == pytest.approx(1.0)
    assert debug["pullback_high"] == pytest.approx(10.70)


def test_absorption_snap_fails_closed_without_l2(monkeypatch) -> None:
    import app.services.trading.momentum_neural.pipeline as pipeline_mod

    monkeypatch.setattr(settings, "chili_momentum_absorption_snap_entry_enabled", True)
    monkeypatch.setattr(pipeline_mod, "read_ladder_distribution", lambda *_args, **_kwargs: None)

    ok, reason, debug = absorption_snap_entry(
        _absorption_snap_df(),
        entry_interval="15s",
        live_price=10.86,
        symbol="JEM",
        db=object(),
    )

    assert ok is False
    assert reason == "absorption_snap_no_l2"
    assert "pullback_high" not in debug


def test_absorption_snap_requires_confirming_tape(monkeypatch) -> None:
    import app.services.trading.momentum_neural.pipeline as pipeline_mod

    monkeypatch.setattr(settings, "chili_momentum_absorption_snap_entry_enabled", True)
    monkeypatch.setattr(
        pipeline_mod,
        "read_ladder_distribution",
        lambda *_args, **_kwargs: SimpleNamespace(
            n_snaps=5,
            ofi=0.50,
            ask_build=0.20,
            micro_edge=1.0,
            depth_imbal_pctile=0.80,
            snapshot_age_s=0.5,
        ),
    )
    monkeypatch.setattr(entry_gates, "_l2_entry_veto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entry_gates, "tape_confirms_hold", lambda *_args, **_kwargs: (False, {"reason": "tape_hold_not_confirmed"}))

    ok, reason, debug = absorption_snap_entry(
        _absorption_snap_df(),
        entry_interval="15s",
        live_price=10.86,
        symbol="JEM",
        db=object(),
    )

    assert ok is False
    assert reason == "absorption_snap_tape_unconfirmed"
    assert debug["tape_reason"] == "tape_hold_not_confirmed"


def test_ask_thins_dip_fails_closed_without_l2(monkeypatch) -> None:
    import app.services.trading.momentum_neural.pipeline as pipeline_mod

    monkeypatch.setattr(settings, "chili_momentum_ask_thins_dip_entry_enabled", True)
    monkeypatch.setattr(pipeline_mod, "read_ladder_distribution", lambda *_args, **_kwargs: None)

    ok, reason, debug = ask_thins_dip_entry(
        _absorption_snap_df(),
        entry_interval="15s",
        live_price=10.86,
        symbol="JEM",
        db=object(),
    )

    assert ok is False
    assert reason == "ask_thins_no_l2"
    assert "pullback_high" not in debug


def _premarket_pivot_df(*, ts: str = "2026-07-02 13:25:00+00:00"):
    import pandas as pd

    idx = pd.date_range(end=pd.Timestamp(ts), periods=10, freq="1min")
    return pd.DataFrame(
        {
            "Open": [9.80, 9.82, 9.86, 9.88, 9.90, 9.93, 9.95, 9.97, 9.98, 10.01],
            "High": [9.85, 9.90, 10.00, 9.96, 9.93, 9.98, 10.04, 10.02, 10.03, 10.06],
            "Low": [9.70, 9.76, 9.84, 9.78, 9.74, 9.86, 9.90, 9.92, 9.94, 9.98],
            "Close": [9.82, 9.88, 9.95, 9.88, 9.86, 9.94, 10.00, 9.99, 10.01, 10.03],
            "Volume": [20_000, 25_000, 35_000, 18_000, 17_000, 22_000, 30_000, 28_000, 32_000, 45_000],
        },
        index=idx,
    )


def _patch_premarket_pivot_dependencies(monkeypatch, *, rvol: float = 2.5, tape_ok: bool = True) -> None:
    n = len(_premarket_pivot_df())
    monkeypatch.setattr(settings, "chili_momentum_premarket_pivot_macd_entry_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_premarket_pivot_cold_rvol_floor", 1.5)
    monkeypatch.setattr(settings, "chili_momentum_swing_pivot_half_window", 2)
    monkeypatch.setattr(settings, "chili_momentum_swing_pivot_atr_noise_frac", 0.5)
    monkeypatch.setattr(
        entry_gates,
        "compute_all_from_df",
        lambda *_args, **_kwargs: {
            "ema_9": [9.80 + i * 0.02 for i in range(n)],
            "ema_20": [9.70 + i * 0.015 for i in range(n)],
            "vwap": [9.75 + i * 0.02 for i in range(n)],
            "macd": [-0.10, -0.08, -0.06, -0.04, -0.02, -0.01, 0.0, 0.04, 0.08, 0.10],
            "macd_signal": [0.0 for _ in range(n)],
            "atr": [0.20 for _ in range(n)],
            "volume_ratio": [rvol for _ in range(n)],
        },
    )
    monkeypatch.setattr(entry_gates, "_batch_c_atr_pct", lambda *_args, **_kwargs: (0.02, 0.20))
    monkeypatch.setattr(
        entry_gates,
        "_swing_pivots",
        lambda *_args, **_kwargs: [
            {"kind": "L", "idx": 4, "price": 9.74},
            {"kind": "H", "idx": 6, "price": 10.04},
            {"kind": "L", "idx": 7, "price": 9.92},
            {"kind": "H", "idx": 8, "price": 10.03},
        ],
    )
    monkeypatch.setattr(entry_gates, "_detect_back_side", lambda *_args, **_kwargs: (False, "frontside"))
    monkeypatch.setattr(entry_gates, "_hod_extension_ok", lambda **_kwargs: (True, {}))
    monkeypatch.setattr(entry_gates, "_l2_entry_veto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        entry_gates,
        "tape_confirms_hold",
        lambda *_args, **_kwargs: (tape_ok, {"reason": "tape_hold_confirmed" if tape_ok else "tape_hold_not_confirmed"}),
    )
    from app.services.trading.momentum_neural import ross_momentum

    monkeypatch.setattr(
        ross_momentum,
        "front_side_state",
        lambda *_args, **_kwargs: SimpleNamespace(above_vwap=True, is_backside=False, reason="frontside"),
    )


def test_premarket_pivot_macd_fires_tick_break_with_structural_stop(monkeypatch) -> None:
    _patch_premarket_pivot_dependencies(monkeypatch)

    ok, reason, debug = premarket_pivot_macd_entry(
        _premarket_pivot_df(),
        entry_interval="1m",
        live_price=10.08,
        symbol="JEM",
        db=object(),
    )

    assert ok is True, (reason, debug)
    assert reason == "premarket_pivot_macd_tick"
    assert debug["market_session"] == "premarket"
    assert debug["macd_recross"] is True
    assert debug["pullback_high"] == pytest.approx(10.03)
    assert debug["pullback_low"] == pytest.approx(9.92)
    assert debug["tick_break"] is True
    assert debug["tape_reason"] == "tape_hold_confirmed"


def test_premarket_pivot_macd_refuses_cold_premarket(monkeypatch) -> None:
    _patch_premarket_pivot_dependencies(monkeypatch, rvol=1.0)

    ok, reason, debug = premarket_pivot_macd_entry(
        _premarket_pivot_df(),
        entry_interval="1m",
        live_price=10.08,
        symbol="JEM",
        db=object(),
    )

    assert ok is False
    assert reason == "premarket_pivot_cold_market"
    assert debug["rvol"] == pytest.approx(1.0)


def test_premarket_pivot_macd_refuses_outside_open_window(monkeypatch) -> None:
    _patch_premarket_pivot_dependencies(monkeypatch)

    ok, reason, debug = premarket_pivot_macd_entry(
        _premarket_pivot_df(ts="2026-07-02 16:30:00+00:00"),
        entry_interval="1m",
        live_price=10.08,
        symbol="JEM",
        db=object(),
    )

    assert ok is False
    assert reason == "premarket_pivot_outside_open_window"
    assert debug["schedule_window"] == "midday"


def test_premarket_pivot_macd_requires_confirming_tape(monkeypatch) -> None:
    _patch_premarket_pivot_dependencies(monkeypatch, tape_ok=False)

    ok, reason, debug = premarket_pivot_macd_entry(
        _premarket_pivot_df(),
        entry_interval="1m",
        live_price=10.08,
        symbol="JEM",
        db=object(),
    )

    assert ok is False
    assert reason == "premarket_pivot_tape_unconfirmed"
    assert debug["tape_reason"] == "tape_hold_not_confirmed"


def test_big_buyer_bid_starter_annotates_only_fresh_tight_self_relative_bid_stack(monkeypatch) -> None:
    import app.services.trading.momentum_neural.pipeline as pipeline_mod

    monkeypatch.setattr(settings, "chili_momentum_big_buyer_bid_starter_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_big_buyer_bid_max_spread_bps", 80.0)
    monkeypatch.setattr(settings, "chili_momentum_big_buyer_bid_pctile_ceiling", 0.85)
    monkeypatch.setattr(
        pipeline_mod,
        "read_ladder_distribution",
        lambda *_args, **_kwargs: SimpleNamespace(
            n_snaps=5,
            spread_bps=18.5,
            depth_imbal_pctile=0.92,
            bid_refill_confirmed=True,
            bid_refill_frac=1.25,
            bid_refill_pctile=0.95,
        ),
    )

    permit = entry_gates._l2_big_buyer_bid_starter(
        "JEM",
        db=object(),
        price=10.00,
        atr_pct=0.01,
        support_level=9.86,
    )

    assert permit is not None
    reason, patch = permit
    assert reason == "l2_big_buyer_bid"
    assert patch["l2_buyer_pctile"] == pytest.approx(0.92)
    assert patch["l2_buyer_ceiling"] == pytest.approx(0.85)
    assert patch["l2_spread_bps"] == pytest.approx(18.5)
    assert patch["l2_bid_support_level"] == pytest.approx(9.86)
    assert patch["l2_bid_refill_confirmed"] is True
    assert patch["l2_bid_refill_frac"] == pytest.approx(1.25)
    assert patch["l2_bid_refill_pctile"] == pytest.approx(0.95)


@pytest.mark.parametrize(
    ("spread_bps", "pctile"),
    [
        (120.0, 0.95),
        (18.0, 0.70),
    ],
)
def test_big_buyer_bid_starter_fails_closed_without_tight_high_percentile_depth(
    monkeypatch,
    spread_bps: float,
    pctile: float,
) -> None:
    import app.services.trading.momentum_neural.pipeline as pipeline_mod

    monkeypatch.setattr(settings, "chili_momentum_big_buyer_bid_starter_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_big_buyer_bid_max_spread_bps", 80.0)
    monkeypatch.setattr(settings, "chili_momentum_big_buyer_bid_pctile_ceiling", 0.85)
    monkeypatch.setattr(
        pipeline_mod,
        "read_ladder_distribution",
        lambda *_args, **_kwargs: SimpleNamespace(
            n_snaps=5,
            spread_bps=spread_bps,
            depth_imbal_pctile=pctile,
        ),
    )

    assert entry_gates._l2_big_buyer_bid_starter("JEM", db=object(), price=10.00) is None


def test_big_buyer_bid_starter_is_not_a_standalone_or_missing_data_entry(monkeypatch) -> None:
    import app.services.trading.momentum_neural.pipeline as pipeline_mod

    monkeypatch.setattr(settings, "chili_momentum_big_buyer_bid_starter_enabled", True)
    monkeypatch.setattr(pipeline_mod, "read_ladder_distribution", lambda *_args, **_kwargs: None)

    assert entry_gates._l2_big_buyer_bid_starter("JEM", db=object(), price=10.00) is None
    assert entry_gates._l2_big_buyer_bid_starter("JEM", db=None, price=10.00) is None
    assert entry_gates._l2_big_buyer_bid_starter("", db=object(), price=10.00) is None


def test_early_trail_arm_precedes_no_confirmation_bailouts() -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    source = inspect.getsource(live_runner_mod.tick_live_session)
    early_idx = source.index("chili_momentum_early_trail_arm_enabled")
    threshold_idx = source.index("float(bid) >= avg * trail_activate_return", early_idx)
    bail_idx = source.index("bail_on_no_confirmation(", threshold_idx)
    legacy_idx = source.index("if st == STATE_LIVE_ENTERED and bid >= avg * trail_activate_return", bail_idx)

    assert early_idx < bail_idx < legacy_idx
    assert threshold_idx < bail_idx


class _FrameStub:
    empty = False

    def __init__(self, rows: int = 10) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return self._rows


def test_ross_starter_wait_replaces_pullback_wait_without_level() -> None:
    assert _should_surface_tick_watch_wait(
        "waiting_for_vwap_reclaim",
        {},
        "ross_breakout_starter_waiting_for_level",
        {"pullback_high": 9.5, "breakout_level": 9.5},
    )


def test_ross_starter_wait_replaces_reclaim_wait_even_with_level() -> None:
    assert _should_surface_tick_watch_wait(
        "waiting_for_reclaim",
        {"pullback_high": 9.2},
        "ross_breakout_starter_waiting_for_push",
        {"pullback_high": 9.5, "breakout_level": 9.5},
    )


def test_generic_wait_does_not_replace_existing_tick_level() -> None:
    assert not _should_surface_tick_watch_wait(
        "waiting_for_break",
        {"pullback_high": 9.2},
        "waiting_for_vwap_reclaim",
        {"pullback_high": 9.1},
    )


def test_entry_trace_payload_carries_canonical_setup_envelope() -> None:
    payload = _entry_trace_event_payload(
        {
            "entry_trigger_reason": "abcd_break_tick_ok",
            "entry_source_wait_reason": "waiting_for_break",
            "entry_pullback_high": 4.25,
            "entry_pullback_low": 3.92,
            "structural_stop_price": 3.92,
            "breakout_level_price": 4.25,
            "entry_stop_model": "structural_or_vol_floor",
            "entry_stop_atr_pct": 0.031,
            "entry_above_vwap": True,
            "entry_micro_frame": {
                "micro_frame_used": "15s",
                "micro_frame_target": "15s",
                "fallback_reason": None,
                "micro_rows": 84,
                "micro_bars": 12,
                "micro_has_iqfeed": True,
            },
            "a_setup_size_floor_eval": {
                "applied": True,
                "reason": "raised_to_soft_floor",
                "floor_source": "soft_geomean",
                "floor_fraction": 0.61,
                "floor_loss_usd": 74.25,
            },
        }
    )

    assert payload["setup_reason"] == "abcd_break_tick_ok"
    assert payload["pullback_high"] == 4.25
    trace = payload["setup_trace"]
    assert trace["setup_alias"] == "abcd_break_tick_ok"
    assert trace["source_wait_reason"] == "waiting_for_break"
    assert trace["source_wait_tick_armed"] is True
    assert trace["source_wait_tape_hold_eligible"] is True
    assert trace["source_wait_has_pullback_levels"] is True
    assert trace["structural_stop_covered"] is True
    assert trace["a_setup_floor_covered"] is True
    assert trace["micro_frame"]["micro_frame_used"] == "15s"
    assert trace["sizing_floor"]["floor_source"] == "soft_geomean"


def test_entry_trace_payload_covers_ross_breakout_starter_alias() -> None:
    payload = _entry_trace_event_payload(
        {
            "entry_trigger_reason": "ross_breakout_starter_tick",
            "entry_source_wait_reason": "ross_breakout_starter_waiting_for_level",
            "entry_pullback_high": 4.25,
            "entry_pullback_low": 4.08,
            "structural_stop_price": 4.08,
        }
    )

    trace = payload["setup_trace"]
    assert trace["setup_alias"] == "ross_breakout_starter_tick"
    assert trace["setup_coverage"] == "structural_a_setup"
    assert trace["structural_stop_covered"] is True
    assert trace["a_setup_floor_covered"] is True
    assert trace["source_wait_tick_armed"] is True
    assert trace["source_wait_tape_hold_eligible"] is False


def test_entry_trace_payload_covers_tick_first_pullback_wait_alias() -> None:
    payload = _entry_trace_event_payload(
        {
            "entry_trigger_reason": "tick_first_pullback_scalp",
            "entry_source_wait_reason": "waiting_for_first_pullback_break",
            "entry_pullback_high": 3.91,
            "entry_pullback_low": 3.70,
            "entry_micro_frame": {"micro_frame_used": "tick", "fallback_reason": None},
        }
    )

    trace = payload["setup_trace"]
    assert trace["setup_alias"] == "tick_first_pullback_scalp"
    assert trace["setup_coverage"] == "structural_a_setup"
    assert trace["source_wait_reason"] == "waiting_for_first_pullback_break"
    assert trace["source_wait_tick_armed"] is True
    assert trace["source_wait_has_pullback_levels"] is True
    assert trace["pullback_high"] == 3.91
    assert trace["pullback_low"] == 3.70
    assert trace["structural_stop_price"] == 3.70
    assert trace["micro_frame"]["micro_frame_used"] == "tick"


def test_entry_trace_payload_covers_micro_pullback_trigger_wait_alias() -> None:
    payload = _entry_trace_event_payload(
        {
            "entry_trigger_reason": "micro_pullback_trigger_wait",
            "entry_source_wait_reason": "bid_prop_unconfirmed_wait",
            "entry_pullback_high": 19.855,
            "entry_pullback_low": 19.81,
            "structural_stop_price": 19.81,
            "entry_micro_frame": {"micro_frame_used": "15s", "micro_has_iqfeed": True},
        }
    )

    trace = payload["setup_trace"]
    assert trace["setup_alias"] == "micro_pullback_trigger_wait"
    assert trace["setup_coverage"] == "structural_a_setup"
    assert trace["structural_stop_covered"] is True
    assert trace["a_setup_floor_covered"] is True
    assert trace["source_wait_reason"] == "bid_prop_unconfirmed_wait"
    assert trace["source_wait_has_pullback_levels"] is True
    assert trace["structural_stop_price"] == 19.81


def test_entry_trace_payload_marks_non_structural_volume_fallback() -> None:
    payload = _entry_trace_event_payload(
        {
            "entry_trigger_reason": "momentum_ok_rel_vol",
            "entry_micro_frame": {"micro_frame_used": "15s", "micro_frame_target": "15s"},
        }
    )

    trace = payload["setup_trace"]
    assert trace["setup_alias"] == "momentum_ok_rel_vol"
    assert trace["setup_coverage"] == "non_structural_volume_fallback"
    assert trace["structural_stop_covered"] is False
    assert trace["a_setup_floor_covered"] is False


def test_pre_candidate_shape_block_reports_structural_setup_coverage() -> None:
    block = _ross_live_pre_candidate_shape_block(
        {"entry_trigger_reason": "abcd_break_tick_ok"},
        trigger_reason="abcd_break_tick_ok",
        entry_frame_debug={"micro_frame_used": "1m", "micro_frame_target": "15s"},
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="JEM",
    )

    assert block is not None
    assert block["reason"] == "ross_live_requires_tick_tape_revalidation"
    assert block["setup_coverage"] == "structural_a_setup"


def test_entry_candidate_payload_marks_uncovered_alias_in_trace() -> None:
    payload = _entry_candidate_event_payload(
        le={"entry_trigger_reason": "unknown_new_alias"},
        viability_score=0.81,
        trigger_reason="unknown_new_alias",
        entry_interval="15s",
        structural_level_meta={"trigger_reason": "unknown_new_alias"},
        l2=None,
        entry_frame_debug={"micro_frame_used": "15s", "micro_frame_target": "15s"},
    )

    assert payload["setup_coverage"] == "uncovered"
    assert payload["setup_structural_stop_covered"] is False
    assert payload["setup_a_floor_covered"] is False
    assert payload["setup_trace"]["setup_coverage"] == "uncovered"
    assert payload["setup_trace"]["entry_interval"] == "15s"


def test_micro_frame_selector_uses_setup_derived_micro_when_primary_enabled(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(settings, "chili_momentum_micropull_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_micro_pullback_primary_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_micropull_bar_seconds", 15, raising=False)
    micro = _FrameStub(12)
    monkeypatch.setattr(
        live_runner_mod,
        "_build_micro_bar_df_with_meta",
        lambda *a, **k: (
            micro,
            {
                "micro_frame_target": "15s",
                "micro_frame_used": "15s",
                "fallback_reason": None,
                "micro_rows": 80,
                "micro_bars": 12,
                "micro_has_iqfeed": True,
            },
        ),
    )

    df, frame, meta = _select_entry_trigger_frame(None, "JEM", _FrameStub(10), "5m")

    assert df is micro
    assert frame == "15s"
    assert meta["fallback_reason"] is None
    assert meta["micro_frame_enable_source"] == "setup_derived"


def test_micro_frame_selector_falls_back_with_telemetry_when_setup_micro_thin(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(settings, "chili_momentum_micropull_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_micro_pullback_primary_enabled", True, raising=False)
    base = _FrameStub(10)
    monkeypatch.setattr(
        live_runner_mod,
        "_build_micro_bar_df_with_meta",
        lambda *a, **k: (
            None,
            {
                "micro_frame_target": "15s",
                "micro_frame_used": None,
                "fallback_reason": "thin_ticks",
                "micro_rows": 1,
                "micro_bars": 0,
                "micro_has_iqfeed": False,
            },
        ),
    )

    df, frame, meta = _select_entry_trigger_frame(None, "JEM", base, "5m")

    assert df is base
    assert frame == "5m"
    assert meta["fallback_reason"] == "thin_ticks"
    assert meta["micro_frame_enable_source"] == "setup_derived"


def _mk_adapter():
    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="SOL-USD",
            bid=99.95,
            ask=100.05,
            mid=100.0,
            spread_bps=10.0,
            freshness=_fresh(),
        ),
        _fresh(),
    )
    prod = NormalizedProduct(
        product_id="SOL-USD",
        base_currency="SOL",
        quote_currency="USD",
        status="online",
        trading_disabled=False,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        base_increment=0.001,
        base_min_size=0.001,
    )
    ad.get_product.return_value = (prod, _fresh())
    ad.place_market_order.return_value = {"ok": True, "order_id": "ord-entry-1", "client_order_id": "cid-e1"}
    ad.get_order.return_value = (
        NormalizedOrder(
            order_id="ord-entry-1",
            client_order_id="cid-e1",
            product_id="SOL-USD",
            side="buy",
            status="FILLED",
            order_type="market",
            filled_size=0.25,
            average_filled_price=100.5,
        ),
        _fresh(),
    )
    ad.cancel_order.return_value = {"ok": True, "raw": {}}
    return ad


def _exit_order(
    *,
    order_id: str = "ord-exit-1",
    status: str = "OPEN",
    raw_state: str | None = None,
    filled_size: float = 0.0,
    average_filled_price: float | None = None,
) -> NormalizedOrder:
    raw = {"state": raw_state} if raw_state is not None else {}
    return NormalizedOrder(
        order_id=order_id,
        client_order_id="cid-exit",
        product_id="LGPS",
        side="sell",
        status=status,
        order_type="market",
        filled_size=filled_size,
        average_filled_price=average_filled_price,
        raw=raw,
    )


def test_live_fsm_transition_rules() -> None:
    assert can_transition_live("armed_pending_runner", STATE_QUEUED_LIVE)
    assert not can_transition_live(STATE_QUEUED_LIVE, "armed_pending_runner")
    assert can_transition_live(STATE_LIVE_PENDING_ENTRY, STATE_LIVE_ENTERED)
    assert not can_transition_live(STATE_QUEUED_LIVE, STATE_LIVE_ENTERED)
    with pytest.raises(ValueError):
        assert_transition_live(STATE_QUEUED_LIVE, "armed_pending_runner")


def test_summarize_live_execution_helpers() -> None:
    assert summarize_live_execution({}) == {}
    assert summarize_live_execution(None) == {}  # type: ignore[arg-type]
    snap = {
        "momentum_live_execution": {
            "tick_count": 2,
            "entry_order_id": "o1",
            "last_exit_intent": {"reason": "stop"},
            "exit_execution_intents": [{"reason": "stop"}],
            "pending_exit_reason": "stop",
            "pending_exit_quantity": 0.1,
            "pending_exit_queued_at_utc": "2026-07-02T10:00:00+00:00",
            "pending_exit_presubmit_deferred": True,
            "pending_exit_product_id": "LGPS",
            "pending_exit_client_order_id": "cid-deferred",
            "last_exit_pending_confirmation": {"why": "exit_fill_pending"},
            "pending_exit_deferred_until_utc": "2026-07-02T13:30:00+00:00",
            "pending_exit_market_session": "post_market",
            "pending_exit_order_status": "queued",
            "pending_exit_filled_size": 0.0,
            "last_exit_deferred": {"why": "exit_queued_non_tradable"},
            "last_exit_deferred_adopted": {"why": "exit_deferred_order_active"},
            "last_exit_terminal_no_fill": {"why": "terminal_no_fill"},
            "last_partial_exit_reason": "target",
            "last_partial_exit_price": 51.0,
            "last_exit_notional_basis_usd": 5.0,
            "last_exit_return_bps": 200.0,
            "last_partial_exit_notional_basis_usd": 1.0,
            "last_partial_exit_return_bps": 100.0,
            "position": {"quantity": 0.1, "avg_entry_price": 50.0, "notional_usd": 5.0},
        }
    }
    s = summarize_live_execution(snap)
    assert s.get("tick_count") == 2
    assert s.get("in_position") is True
    assert s.get("avg_entry_price") == 50.0
    assert s.get("last_exit_intent") == {"reason": "stop"}
    assert s.get("exit_execution_intent_count") == 1
    assert s.get("pending_exit_reason") == "stop"
    assert s.get("pending_exit_quantity") == 0.1
    assert s.get("pending_exit_queued_at_utc") == "2026-07-02T10:00:00+00:00"
    assert s.get("pending_exit_presubmit_deferred") is True
    assert s.get("pending_exit_product_id") == "LGPS"
    assert s.get("pending_exit_client_order_id") == "cid-deferred"
    assert s.get("last_exit_pending_confirmation") == {"why": "exit_fill_pending"}
    assert s.get("pending_exit_deferred_until_utc") == "2026-07-02T13:30:00+00:00"
    assert s.get("pending_exit_market_session") == "post_market"
    assert s.get("pending_exit_order_status") == "queued"
    assert s.get("pending_exit_filled_size") == 0.0
    assert s.get("last_exit_deferred") == {"why": "exit_queued_non_tradable"}
    assert s.get("last_exit_deferred_adopted") == {"why": "exit_deferred_order_active"}
    assert s.get("last_exit_terminal_no_fill") == {"why": "terminal_no_fill"}
    assert s.get("last_partial_exit_reason") == "target"
    assert s.get("last_partial_exit_price") == 51.0
    assert s.get("last_exit_notional_basis_usd") == 5.0
    assert s.get("last_exit_return_bps") == 200.0
    assert s.get("last_partial_exit_notional_basis_usd") == 1.0
    assert s.get("last_partial_exit_return_bps") == 100.0


def test_quote_quality_block_preserves_zero_live_spread_cap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 0.0)
    fresh = _fresh()

    gate = _quote_quality_block(
        NormalizedTicker(
            product_id="SOL-USD",
            bid=99.995,
            ask=100.005,
            mid=100.0,
            spread_bps=1.0,
            freshness=fresh,
        ),
        fresh,
    )

    assert gate is not None
    assert gate["reason"] == "wide_bbo_spread"
    assert gate["spread_bps"] == 1.0
    assert gate["max_spread_bps"] == 0.0


def test_notional_guard_multiplier_preserves_zero_bps(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_order_notional_guard_bps", 0.0)

    assert _notional_guard_multiplier() == 1.0


def test_pre_submit_stale_path_blocks_internal_latency_without_refreshing_clock() -> None:
    old_ts = "2026-07-01T14:05:01+00:00"
    le = {"entry_pending_place_utc": old_ts}

    meta = _pre_submit_stale_path_block(
        le,
        pending_place_utc=old_ts,
        pending_age_s=7.229,
        max_age_s=6.0,
    )

    assert meta is not None
    assert meta["reason"] == "stale_pre_submit_pending"
    assert meta["pending_place_utc"] == old_ts
    assert le["entry_pending_place_utc"] == old_ts
    assert "entry_pre_submit_stale_blocked_utc" in le
    assert le["entry_pre_submit_internal_latency_s"] == 7.229

    missing = {}
    missing_meta = _pre_submit_stale_path_block(
        missing,
        pending_place_utc=None,
        pending_age_s=None,
        max_age_s=6.0,
    )
    assert missing_meta is not None
    assert missing_meta["reason"] == "missing_pending_timestamp"
    assert "entry_pending_place_utc" not in missing


def test_rejected_tick_scalp_fire_rearms_instead_of_staying_already_fired() -> None:
    le = {
        "entry_trigger_reason": "tick_first_pullback_scalp",
        "entry_source_wait_reason": "already_fired",
        "tick_scalp_state": {
            "symbol": "JEM",
            "phase": "fired",
            "fired": True,
            "high": 3.8107,
            "pullback_low": 3.705,
            "last_price": 3.8107,
        },
    }

    assert _reset_rejected_tick_scalp_fire(le, reason="stale_pre_submit_pending") is True

    state = le["tick_scalp_state"]
    assert state["phase"] == "pullback"
    assert "fired" not in state
    assert state["last_rejected_fire_reason"] == "stale_pre_submit_pending"
    assert le["entry_tick_scalp_fire_rearmed_reason"] == "stale_pre_submit_pending"


def test_stranded_tick_scalp_rearms_even_without_legacy_trigger_reason() -> None:
    le = {
        "entry_source_wait_reason": "already_fired",
        "tick_scalp_state": {
            "symbol": "DSY",
            "phase": "fired",
            "fired": True,
            "high": 5.12,
            "pullback_low": 4.255,
        },
    }

    assert _reset_rejected_tick_scalp_fire(
        le,
        reason="stranded_fired_no_submitted_order",
        require_trigger_reason=False,
    ) is True
    state = le["tick_scalp_state"]
    assert state.get("fired") is None
    assert state["phase"] == "pullback"
    assert le["entry_source_wait_reason"] == "rearmed_waiting_for_tick_reclaim"
    assert le["entry_tick_scalp_fire_rearmed_reason"] == "stranded_fired_no_submitted_order"
    assert le["entry_source_wait_reason"] == "rearmed_waiting_for_tick_reclaim"

    assert _reset_rejected_tick_scalp_fire(
        {"entry_trigger_reason": "momentum_ok_rel_vol", "tick_scalp_state": {"fired": True}},
        reason="ignored",
    ) is False

    assert _reset_rejected_tick_scalp_fire(
        {"tick_scalp_state": {"phase": "fired", "fired": True, "pullback_low": 3.48}},
        reason="stranded_fired_no_submitted_order",
    ) is False

    stranded = {"tick_scalp_state": {"phase": "fired", "fired": True, "pullback_low": 3.48}}
    assert _reset_rejected_tick_scalp_fire(
        stranded,
        reason="stranded_fired_no_submitted_order",
        require_trigger_reason=False,
    ) is True
    assert stranded["tick_scalp_state"]["phase"] == "pullback"
    assert "fired" not in stranded["tick_scalp_state"]


def test_ross_scalp_time_floor_bound_uses_trade_horizon_not_fixed_cap() -> None:
    bound, source = _ross_scalp_time_floor_bound_s(
        {"entry_trigger_reason": "tick_first_pullback_scalp", "tick_scalp_max_hold_seconds": 12.0},
        max_hold_seconds=3600,
        breakout_window_seconds=30,
    )

    assert bound == 12.0
    assert source == "tick_scalp_max_hold_seconds"

    breakout_bound, breakout_source = _ross_scalp_time_floor_bound_s(
        {"entry_trigger_reason": "ross_breakout_starter_tick"},
        max_hold_seconds=3600,
        breakout_window_seconds=30,
    )

    assert breakout_bound == 30.0
    assert breakout_source == "breakout_window_seconds"


def test_pre_submit_ross_universe_block_reuses_final_risk_boundary(monkeypatch) -> None:
    from app.services.trading.momentum_neural import risk_evaluator

    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    monkeypatch.setattr(
        risk_evaluator,
        "_ross_lane_universe_required",
        lambda *, mode, execution_family, symbol: True,
    )
    monkeypatch.setattr(
        risk_evaluator,
        "_ross_lane_universe_check",
        lambda symbol, via: (
            False,
            "ross_universe_price_above_profile",
            {"price": 618.15, "price_max": 20.0},
        ),
    )

    block = _pre_submit_ross_universe_block(
        symbol="META",
        mode="live",
        execution_family="robinhood_agentic_mcp",
        via=SimpleNamespace(),
    )

    assert block is not None
    assert block["reason"] == "ross_universe_price_above_profile"
    assert block["symbol"] == "META"
    assert block["detail"]["price_max"] == 20.0


def test_pre_submit_ross_universe_block_allows_verified_smallcap(monkeypatch) -> None:
    from app.services.trading.momentum_neural import risk_evaluator

    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    monkeypatch.setattr(
        risk_evaluator,
        "_ross_lane_universe_required",
        lambda *, mode, execution_family, symbol: True,
    )
    monkeypatch.setattr(
        risk_evaluator,
        "_ross_lane_universe_check",
        lambda symbol, via: (True, "ross_universe_profile_ok", {"price": 3.86}),
    )

    assert _pre_submit_ross_universe_block(
        symbol="JEM",
        mode="live",
        execution_family="robinhood_agentic_mcp",
        via=SimpleNamespace(),
    ) is None


def test_ross_live_entry_shape_blocks_generic_volume_fallback() -> None:
    block = _ross_live_entry_shape_block(
        {
            "entry_trigger_reason": "momentum_ok_rel_vol",
            "entry_micro_frame": {"micro_frame_used": "15s"},
        },
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="EHGO",
    )

    assert block is not None
    assert block["reason"] == "ross_live_non_structural_volume_fallback"
    assert block["setup_coverage"] == "non_structural_volume_fallback"


def test_ross_live_entry_shape_blocks_5m_abcd_without_tick_tape_revalidation() -> None:
    block = _ross_live_entry_shape_block(
        {
            "entry_trigger_reason": "abcd_break_tick_ok",
            "entry_micro_frame": {"micro_frame_used": "5m"},
        },
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="LHAI",
    )

    assert block is not None
    assert block["reason"] == "ross_live_requires_tick_tape_revalidation"
    assert block["micro_frame_used"] == "5m"


def test_ross_live_pre_candidate_shape_blocks_micro_error_5m_fallback() -> None:
    block = _ross_live_pre_candidate_shape_block(
        {},
        trigger_reason="abcd_break_tick_ok",
        entry_frame_debug={
            "micro_frame_target": "15s",
            "micro_frame_used": "5m",
            "fallback_reason": "micro_error",
            "micro_rows": 0,
            "micro_bars": 0,
            "micro_has_iqfeed": False,
        },
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="LHAI",
    )

    assert block is not None
    assert block["reason"] == "ross_live_requires_tick_tape_revalidation"
    assert block["micro_frame_used"] == "5m"


def test_ross_live_pre_candidate_shape_allows_tick_frame() -> None:
    assert _ross_live_pre_candidate_shape_block(
        {},
        trigger_reason="ross_breakout_starter_tick",
        entry_frame_debug={"micro_frame_used": "tick", "fallback_reason": None},
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="JEM",
    ) is None


def test_ross_live_entry_shape_blocks_1m_breakout_attempt_without_tick_tape_revalidation() -> None:
    block = _ross_live_entry_shape_block(
        {
            "entry_trigger_reason": "ross_breakout_attempt",
            "entry_micro_frame": {"micro_frame_used": "1m"},
        },
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="JEM",
    )

    assert block is not None
    assert block["reason"] == "ross_live_requires_tick_tape_revalidation"
    assert block["trigger_reason"] == "ross_breakout_attempt"
    assert block["micro_frame_used"] == "1m"


def test_ross_live_entry_shape_blocks_tick_label_without_tick_frame_evidence() -> None:
    block = _ross_live_entry_shape_block(
        {
            "entry_trigger_reason": "abcd_break_tick_ok",
        },
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="LHAI",
    )

    assert block is not None
    assert block["reason"] == "ross_live_requires_tick_tape_revalidation"
    assert block["trigger_reason"] == "abcd_break_tick_ok"
    assert block["micro_frame_used"] is None


def test_ross_live_entry_shape_allows_tick_first_pullback() -> None:
    assert _ross_live_entry_shape_block(
        {
            "entry_trigger_reason": "tick_first_pullback_scalp",
            "entry_micro_frame": {"micro_frame_used": "tick"},
        },
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="CANF",
    ) is None


def test_ross_live_entry_shape_allows_tick_breakout_attempt() -> None:
    assert _ross_live_entry_shape_block(
        {
            "entry_trigger_reason": "ross_breakout_starter_tick",
            "entry_micro_frame": {"micro_frame_used": "tick"},
        },
        mode="live",
        execution_family="robinhood_agentic_mcp",
        symbol="JEM",
    ) is None


def test_ross_tick_tape_entry_is_adaptive_hold_family() -> None:
    assert _is_ross_tick_tape_entry({"entry_trigger_reason": "ross_breakout_starter_tick"}) is True
    assert _is_ross_tick_tape_entry({"entry_trigger_reason": "tick_first_pullback_scalp"}) is True
    assert _is_ross_tick_tape_entry({"entry_trigger_reason": "tape_confirmed_hold"}) is True
    assert _is_ross_tick_tape_entry({"entry_trigger_reason": "momentum_ok_rel_vol"}) is False


def test_ross_transcript_starter_signal_recognizes_clipped_clro_starter_context() -> None:
    via = SimpleNamespace(
        execution_readiness_json={
            "extra": {
                "ross_signals": {
                    "CLRO": {
                        "ticker": "CLRO",
                        "source": "ross_audio_transcript warrior ross 5 pillars",
                        "scanner_source": "ross_audio_transcript",
                        "signal_type": "ross_transcript_mention",
                        "transcript_text": (
                            "The LRO, I got a starter in the small account looking for "
                            "the squeeze through five"
                        ),
                    }
                }
            }
        }
    )

    assert _ross_transcript_starter_signal(via, "CLRO") is True


def test_ross_transcript_starter_signal_rejects_plain_tape_delta_context() -> None:
    via = SimpleNamespace(
        execution_readiness_json={
            "extra": {
                "ross_signals": {
                    "CLRO": {
                        "ticker": "CLRO",
                        "source": "tape_delta_ignite",
                        "signal_type": "running_up_ignite",
                    }
                }
            }
        }
    )

    assert _ross_transcript_starter_signal(via, "CLRO") is False


def test_ross_tick_profile_signal_uses_snapshot_backfill_for_tape_delta(monkeypatch) -> None:
    import app.services.trading.momentum_neural.ross_event_admission as admission_mod

    seen: dict[str, object] = {}

    def _prove(symbol, *, signal, snapshot_row, snapshot_provider):
        seen["symbol"] = symbol
        seen["signal"] = signal
        seen["snapshot_row"] = snapshot_row
        seen["snapshot_provider"] = snapshot_provider
        return True, "ross_universe_profile_ok", {"snapshot_backfill_used": True}, {"ticker": symbol}

    monkeypatch.setattr(admission_mod, "_prove_ross_universe", _prove)
    via = SimpleNamespace(
        execution_readiness_json={
            "extra": {
                "ross_signals": {
                    "CLRO": {
                        "ticker": "CLRO",
                        "price": 5.215,
                        "todays_change_perc": 10.8395,
                        "source": "tape_delta_ignite",
                        "signal_type": "tape_delta_ignite",
                    }
                }
            }
        }
    )

    sig = _ross_tick_profile_signal(via, "CLRO")

    assert sig is not None
    assert sig["ticker"] == "CLRO"
    assert seen["symbol"] == "CLRO"
    assert seen["snapshot_row"] is None


def test_legacy_breakout_bailout_excludes_ross_tick_tape_entries() -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    source = inspect.getsource(live_runner_mod.tick_live_session)
    assert "_ross_tick_tape_entry = _is_ross_tick_tape_entry(le)" in source
    assert 'getattr(settings, "chili_momentum_smart_hold_enabled", False)) or _ross_tick_tape_entry' in source
    assert "and not _ross_tick_tape_entry" in source


def test_daily_trade_budget_block_clears_tick_scalp_stranded_state() -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    source = inspect.getsource(live_runner_mod.tick_live_session)
    budget_idx = source.index("live_entry_blocked_daily_trade_count_budget")
    block = source[budget_idx: budget_idx + 900]

    assert "_clear_pre_submit_pending_marker(" in block
    assert 'reason="daily_trade_count_budget_reached"' in block
    assert "_reset_rejected_tick_scalp_fire(" in block
    assert "require_trigger_reason=False" in block
    assert 'le["entry_source_wait_reason"] = "daily_trade_count_budget_reached"' in block


def test_daily_trade_budget_receives_ross_tick_proof_context() -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    source = inspect.getsource(live_runner_mod.tick_live_session)
    call_idx = source.index("daily_trade_count_budget_decision(")
    block = source[call_idx: call_idx + 1700]

    assert "entry_context={" in block
    assert '"asset_class": asset_class_for_symbol(sess.symbol)' in block
    assert '"ross_universe_ok": _budget_universe_ok' in block
    assert '"ross_entry_shape_ok": _budget_shape_ok' in block
    assert '"micro_frame_used": _budget_micro_frame' in block
    assert '"setup_coverage": _setup_coverage_for_trigger(_budget_trigger)' in block
    assert "live_entry_daily_trade_count_budget_overflow_allowed" in source


def test_entry_budget_risk_uses_wider_structural_stop_distance() -> None:
    dist, meta = _entry_candidate_budget_stop_distance(
        le={"structural_stop_price": 2.30},
        guarded_ask=2.38,
        sizing_meta={"stop_distance": 0.037},
    )

    assert dist == pytest.approx(0.08)
    assert meta["basis"] == "structural"
    assert meta["sizing_stop_distance"] == pytest.approx(0.037)
    assert meta["structural_stop_distance"] == pytest.approx(0.08)


def test_entry_budget_risk_falls_back_to_sizing_when_structural_stop_missing() -> None:
    dist, meta = _entry_candidate_budget_stop_distance(
        le={},
        guarded_ask=2.38,
        sizing_meta={"stop_distance": 0.037},
    )

    assert dist == pytest.approx(0.037)
    assert meta["basis"] == "sizing"


def test_boundary_risk_block_rearms_stranded_tick_scalp_source_invariant() -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    source = inspect.getsource(live_runner_mod.tick_live_session)
    block_idx = source.index("if not ok_b:")
    block = source[block_idx: block_idx + 1600]

    assert "_reset_stranded_tick_scalp_before_no_order_block(" in source
    assert 'reason="boundary_risk_block"' in block


@pytest.mark.parametrize(
    ("errors", "expected_reason"),
    [
        (["Viability snapshot stale (age 9202s > max 600.0s)."], "viability_snapshot_stale"),
        (
            [
                "Not live-eligible per neural viability.",
                "Ross equity lane blocks faded/thin small-cap candidate below profile.",
            ],
            "ross_profile_below_profile",
        ),
        (
            [
                "Not live-eligible per neural viability.",
                "Ross equity lane blocks sub-dollar/non-profile equity candidate.",
            ],
            "ross_profile_sub_dollar_or_non_profile",
        ),
        (["Not live-eligible per neural viability."], "neural_viability_not_live_eligible"),
    ],
)
def test_boundary_risk_block_payload_normalizes_live_error_reasons(errors, expected_reason) -> None:
    payload = _boundary_risk_block_payload({"severity": "block", "errors": errors})

    assert payload["reason"] == expected_reason
    assert payload["severity"] == "block"
    assert payload["errors"] == errors
    assert payload["failed_check_ids"] == []


def test_boundary_risk_block_payload_prefers_structured_failed_checks() -> None:
    payload = _boundary_risk_block_payload(
        {
            "severity": "block",
            "errors": ["free text remains for diagnostics"],
            "checks": [
                {"id": "daily_loss", "ok": True},
                {"id": "viability_freshness", "ok": False},
                {"id": "live_eligible", "ok": False},
            ],
        }
    )

    assert payload["reason"] == "boundary_viability_freshness_and_live_eligible"
    assert payload["failed_check_ids"] == ["viability_freshness", "live_eligible"]
    assert payload["errors"] == ["free text remains for diagnostics"]


def test_a_setup_size_floor_hard_reducer_is_not_labeled_order_blocker() -> None:
    _loss, meta = apply_a_setup_combined_size_floor(
        base_loss_usd=100.0,
        effective_loss_usd=20.0,
        floor_fraction=0.5,
        enabled=True,
        is_equity=True,
        trigger_reason="abcd_break_tick_ok",
        viability_score=0.90,
        viability_floor=0.70,
        frontside_mult=1.0,
        frontside_floor=0.50,
        hard_reducer_mults={"severe_liquidity": 0.50},
        soft_floor_mults={"streak": 0.50},
    )

    assert meta["applied"] is False
    assert meta["reason"] == "hard_reducer_respected"
    assert meta["hard_reducers"] == {"severe_liquidity": 0.5}
    assert "hard_blockers" not in meta


def test_a_setup_notional_floor_lifts_soft_crushed_live_size_toward_cash_fraction() -> None:
    loss, meta = apply_a_setup_notional_floor_budget(
        base_loss_usd=120.0,
        effective_loss_usd=6.0,
        target_notional_usd=2000.0,
        entry_price=5.0,
        stop_distance_usd=0.10,
        target_fraction=1.0,
        enabled=True,
        is_equity=True,
        trigger_reason="ross_breakout_starter_tick",
        viability_score=0.92,
        viability_floor=0.70,
        frontside_mult=1.0,
        frontside_floor=0.50,
        hard_reducer_mults={},
    )

    assert loss == 40.0
    assert meta["applied"] is True
    assert meta["reason"] == "lifted_to_notional_floor"
    assert meta["target_notional_usd"] == 2000.0
    assert meta["post_floor_notional_est"] == 2000.0


def test_a_setup_notional_floor_caps_at_base_risk_when_stop_is_wide() -> None:
    loss, meta = apply_a_setup_notional_floor_budget(
        base_loss_usd=120.0,
        effective_loss_usd=6.0,
        target_notional_usd=2000.0,
        entry_price=5.0,
        stop_distance_usd=0.50,
        target_fraction=1.0,
        enabled=True,
        is_equity=True,
        trigger_reason="ross_breakout_starter_tick",
        viability_score=0.92,
        viability_floor=0.70,
        frontside_mult=1.0,
        frontside_floor=0.50,
        hard_reducer_mults={},
    )

    assert loss == 120.0
    assert meta["applied"] is True
    assert meta["capped_by_base_loss"] is True
    assert meta["post_floor_notional_est"] == 1200.0


def test_a_setup_notional_floor_respects_market_toxicity_hard_reducers() -> None:
    loss, meta = apply_a_setup_notional_floor_budget(
        base_loss_usd=120.0,
        effective_loss_usd=6.0,
        target_notional_usd=2000.0,
        entry_price=5.0,
        stop_distance_usd=0.10,
        target_fraction=1.0,
        enabled=True,
        is_equity=True,
        trigger_reason="ross_breakout_starter_tick",
        viability_score=0.92,
        viability_floor=0.70,
        frontside_mult=1.0,
        frontside_floor=0.50,
        hard_reducer_mults={"severe_liquidity": 0.50},
    )

    assert loss == 6.0
    assert meta["applied"] is False
    assert meta["reason"] == "hard_reducer_respected"
    assert meta["hard_reducers"] == {"severe_liquidity": 0.5}


def test_a_setup_notional_floor_respects_red_intraday_hard_reducer() -> None:
    loss, meta = apply_a_setup_notional_floor_budget(
        base_loss_usd=120.0,
        effective_loss_usd=6.0,
        target_notional_usd=2000.0,
        entry_price=5.0,
        stop_distance_usd=0.10,
        target_fraction=1.0,
        enabled=True,
        is_equity=True,
        trigger_reason="ross_breakout_starter_tick",
        viability_score=0.92,
        viability_floor=0.70,
        frontside_mult=1.0,
        frontside_floor=0.50,
        hard_reducer_mults={"red_intraday": 0.50},
    )

    assert loss == 6.0
    assert meta["applied"] is False
    assert meta["reason"] == "hard_reducer_respected"
    assert meta["hard_reducers"] == {"red_intraday": 0.5}


def test_live_notional_floor_call_keeps_red_intraday_hard() -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    source = inspect.getsource(live_runner_mod.tick_live_session)
    call_idx = source.index("apply_a_setup_notional_floor_budget(")
    block = source[call_idx: call_idx + 2200]

    assert '"red_intraday": _red_intraday_mult' in block
    assert '"prior_day": _prior_day_mult' not in block
    assert '"time_fatigue": _time_fatigue_mult' not in block
    assert '"meta_label": _meta_mult' not in block


def test_ross_instant_bid_cut_suppresses_first_wick_above_structure() -> None:
    meta = _ross_instant_bid_cut_suppressed(
        {
            "entry_trigger_reason": "tick_first_pullback_scalp",
            "structural_stop_price": 1.48,
        },
        bid=1.53,
    )

    assert meta is not None
    assert meta["reason"] == "ross_tick_tape_above_structural_stop"


def test_ross_instant_bid_cut_does_not_suppress_structural_stop_breach() -> None:
    assert _ross_instant_bid_cut_suppressed(
        {
            "entry_trigger_reason": "tick_first_pullback_scalp",
            "structural_stop_price": 1.48,
        },
        bid=1.47,
    ) is None


def test_ross_instant_bid_cut_does_not_suppress_generic_entry() -> None:
    assert _ross_instant_bid_cut_suppressed(
        {
            "entry_trigger_reason": "momentum_ok_rel_vol",
            "structural_stop_price": 1.48,
        },
        bid=1.53,
    ) is None


def test_adaptive_max_spread_bps_floor_and_loosening() -> None:
    from app.services.trading.momentum_neural.risk_policy import adaptive_max_spread_bps

    base = 12.0
    # Unknown / non-finite / non-positive expected move -> base floor (no loosen).
    assert adaptive_max_spread_bps(base, None, 0.5) == base
    assert adaptive_max_spread_bps(base, 0.0, 0.5) == base
    assert adaptive_max_spread_bps(base, -5.0, 0.5) == base
    assert adaptive_max_spread_bps(base, float("nan"), 0.5) == base
    # Bad ratio -> base floor.
    assert adaptive_max_spread_bps(base, 400.0, 0.0) == base
    assert adaptive_max_spread_bps(base, 400.0, -1.0) == base
    # Low-vol instrument: ratio*move below the floor -> keep the floor (never tighten).
    assert adaptive_max_spread_bps(base, 10.0, 0.5) == base  # 0.5*10 = 5 < 12
    # Explosive instrument: ratio*move above the floor -> loosen proportionally.
    assert adaptive_max_spread_bps(base, 400.0, 0.5) == pytest.approx(200.0)


def test_adaptive_max_spread_bps_absolute_cap() -> None:
    """Ross 'skip if the spread is too wide': the adaptive tolerance never exceeds
    the absolute cap, no matter how explosive the name."""
    from app.services.trading.momentum_neural.risk_policy import adaptive_max_spread_bps

    base = 12.0
    # Explosive name (INHD-like): 0.5*1678 = 839 bps uncapped; the cap holds it to 300.
    assert adaptive_max_spread_bps(base, 1678.0, 0.5) == pytest.approx(839.0)
    assert adaptive_max_spread_bps(base, 1678.0, 0.5, abs_cap_bps=300.0) == pytest.approx(300.0)
    # Below the cap -> unaffected.
    assert adaptive_max_spread_bps(base, 400.0, 0.5, abs_cap_bps=300.0) == pytest.approx(200.0)
    # The cap never forces tolerance BELOW the documented floor.
    assert adaptive_max_spread_bps(base, 1678.0, 0.5, abs_cap_bps=5.0) == base
    # No cap -> prior behavior preserved.
    assert adaptive_max_spread_bps(base, 1678.0, 0.5, abs_cap_bps=None) == pytest.approx(839.0)


def test_adaptive_live_max_spread_bps_reads_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5)
    assert _adaptive_live_max_spread_bps(None) == 12.0  # no move -> floor
    assert _adaptive_live_max_spread_bps(10.0) == 12.0  # quiet -> floor
    assert _adaptive_live_max_spread_bps(300.0) == pytest.approx(150.0)  # mover -> loosen


def test_limit_entry_spread_ceiling_uses_move_not_fixed_abs_cap() -> None:
    # AHMA class: marketable-limit entry keeps a wider wall than the economic
    # spread cap, but it is still derived from the symbol's expected move. It
    # must not silently become the old fixed 300bps absolute cap.
    assert _limit_entry_spread_ceiling_bps(
        expected_move_bps=262.9344,
        fallback_em_bps=None,
        adaptive_max_spread_bps=131.4672,
    ) == pytest.approx(262.9344)


def test_limit_entry_spread_ceiling_uses_fallback_when_frame_is_cold() -> None:
    assert _limit_entry_spread_ceiling_bps(
        expected_move_bps=None,
        fallback_em_bps=420.0,
        adaptive_max_spread_bps=210.0,
    ) == pytest.approx(420.0)


def test_expected_move_bps_from_ohlcv() -> None:
    import pandas as pd

    assert _expected_move_bps_from_ohlcv(None) is None
    assert _expected_move_bps_from_ohlcv(pd.DataFrame()) is None
    # Steady ~2% per-bar range around 100 -> ~200 bps expected move (ATR/close).
    price = 100.0
    rows = [
        {"High": price * 1.01, "Low": price * 0.99, "Close": price, "Volume": 1000.0}
        for _ in range(30)
    ]
    em = _expected_move_bps_from_ohlcv(pd.DataFrame(rows))
    assert em is not None
    assert em == pytest.approx(200.0, rel=0.1)


def test_quote_quality_block_adaptive_override_allows_wide_spread_on_mover() -> None:
    fresh = _fresh()
    tick = NormalizedTicker(
        product_id="MOV-USD",
        bid=99.65,
        ask=100.35,
        mid=100.0,
        spread_bps=70.0,
        freshness=fresh,
    )
    # Base floor (12 bps) blocks a 70 bps spread...
    blocked = _quote_quality_block(tick, fresh, max_spread_bps=12.0)
    assert blocked is not None and blocked["reason"] == "wide_bbo_spread"
    # ...but an adaptive tolerance from a high expected move (0.5 * 300 = 150)
    # lets the explosive mover through.
    assert _quote_quality_block(tick, fresh, max_spread_bps=150.0) is None


def test_quote_quality_block_allows_ahma_class_spread_with_entry_ceiling() -> None:
    fresh = _fresh()
    tick = NormalizedTicker(
        product_id="AHMA",
        bid=1.94,
        ask=2.003,
        mid=1.9715,
        spread_bps=317.0,
        freshness=fresh,
    )

    stale_fixed_cap = _quote_quality_block(tick, fresh, max_spread_bps=300.0)
    assert stale_fixed_cap is not None
    assert stale_fixed_cap["reason"] == "wide_bbo_spread"

    assert _quote_quality_block(tick, fresh, max_spread_bps=426.83) is None


def test_c1_iqfeed_phantom_loss_uses_symbol_spread_scale(monkeypatch) -> None:
    from app.services.trading.momentum_neural import nbbo_tape

    monkeypatch.setattr(settings, "chili_momentum_max_loss_phantom_divergence_spread_mult", 3.0, raising=False)

    def narrow_tape(_db, _symbol, *, window_s, max_rows, now_utc):
        return [(4.20, 8.0), (4.21, 10.0), (4.22, 12.0)]

    monkeypatch.setattr(nbbo_tape, "recent_bid_spread_tape", narrow_tape)
    phantom, debug = _c1_iqfeed_phantom_loss(MagicMock(), "CELZ", in_process_bid=3.20)

    assert phantom is True
    assert debug["checked"] is True
    assert debug["tolerance_basis"] == "recent_median_spread"
    assert debug["iqfeed_truth_bid"] == pytest.approx(4.22)
    assert debug["divergence_bps"] > debug["tolerance_bps"]

    def wide_tape(_db, _symbol, *, window_s, max_rows, now_utc):
        return [(4.20, 200.0), (4.21, 240.0), (4.22, 260.0)]

    monkeypatch.setattr(nbbo_tape, "recent_bid_spread_tape", wide_tape)
    wide_market_phantom, wide_debug = _c1_iqfeed_phantom_loss(MagicMock(), "CELZ", in_process_bid=4.00)

    assert wide_market_phantom is False
    assert wide_debug["tolerance_basis"] == "recent_median_spread"
    assert wide_debug["divergence_bps"] < wide_debug["tolerance_bps"]


def test_c1_iqfeed_phantom_loss_fails_closed_without_confirming_tape(monkeypatch) -> None:
    from app.services.trading.momentum_neural import nbbo_tape

    monkeypatch.setattr(nbbo_tape, "recent_bid_spread_tape", lambda *_args, **_kwargs: [])
    phantom, debug = _c1_iqfeed_phantom_loss(MagicMock(), "CELZ", in_process_bid=3.20)

    assert phantom is False
    assert debug["checked"] is False

    monkeypatch.setattr(
        nbbo_tape,
        "recent_bid_spread_tape",
        lambda *_args, **_kwargs: [(3.18, 10.0), (3.20, 10.0)],
    )
    confirming_phantom, confirming_debug = _c1_iqfeed_phantom_loss(MagicMock(), "CELZ", in_process_bid=3.20)

    assert confirming_phantom is False
    assert confirming_debug["checked"] is True
    assert confirming_debug["divergence_bps"] <= confirming_debug["tolerance_bps"]


def test_c1_phantom_loss_skip_keeps_live_runner_entered(monkeypatch, db: Session) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    from app.services.trading.momentum_neural import nbbo_tape
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_bail_on_no_confirmation_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_max_loss_fresh_quote_guard_enabled", True)
    monkeypatch.setattr(live_runner_mod, "_venue_broker_connected", lambda _ef: True)
    monkeypatch.setattr(live_runner_mod, "_reconcile_venue_position", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        nbbo_tape,
        "recent_bid_spread_tape",
        lambda *_args, **_kwargs: [(4.20, 8.0), (4.21, 10.0), (4.22, 12.0)],
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_emit", lambda _db, _sess, event_type, payload: events.append((event_type, payload)))

    vid = _variant_id_for_live_test(db, symbol="CELZ")
    uid = _uid(db, "c1_phantom_skip")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="CELZ",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_ENTERED,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": datetime.now(timezone.utc).isoformat()},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {
                "max_loss_per_trade_usd": 5.0,
                "max_notional_per_trade_usd": 500.0,
                "max_hold_seconds": 3600,
            },
            "momentum_live_execution": {
                "max_loss_circuit_fired": True,
                "position": {
                    "quantity": 10.0,
                    "avg_entry_price": 4.50,
                    "stop_price": 2.00,
                    "target_price": 5.10,
                    "high_water_mark": 4.60,
                    "opened_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            },
        },
    )
    db.commit()

    ad = _mk_adapter()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="CELZ",
            bid=3.20,
            ask=3.201,
            mid=3.2005,
            spread_bps=3.1,
            freshness=_fresh(),
        ),
        _fresh(),
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.refresh(sess)
    assert out.get("state") == STATE_LIVE_ENTERED
    assert sess.state == STATE_LIVE_ENTERED
    assert any(event_type == "max_loss_per_trade_phantom_skip" for event_type, _payload in events)
    assert not any(
        event_type == "live_bailout" and payload.get("reason") == "max_loss_per_trade"
        for event_type, payload in events
    )


def test_missing_viability_still_blocks_pre_entry_live_session(monkeypatch, db: Session) -> None:
    from app.models.trading import MomentumSymbolViability
    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(live_runner_mod, "_venue_broker_connected", lambda _ef: True)
    monkeypatch.setattr(
        live_runner_mod,
        "evaluate_proposed_momentum_automation",
        lambda *_args, **_kwargs: {"allowed": True, "checks": []},
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )

    vid = _variant_id_for_live_test(db, symbol="CELZ")
    (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "CELZ", MomentumSymbolViability.variant_id == vid)
        .delete(synchronize_session=False)
    )
    db.commit()
    uid = _uid(db, "pre_entry_no_viability")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="CELZ",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_ENTRY_CANDIDATE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": datetime.now(timezone.utc).isoformat()},
            "momentum_live_execution": {},
        },
    )
    db.commit()

    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.refresh(sess)
    assert out == {"ok": False, "error": "no_viability"}
    assert sess.state == STATE_LIVE_ERROR
    assert ("live_error", {"reason": "viability_missing"}) in events
    assert not any(event_type == "held_position_viability_missing_manage_position" for event_type, _payload in events)
    ad.place_market_order.assert_not_called()
    ad.place_limit_order_gtc.assert_not_called()


def test_live_exit_intent_records_packet_context(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    calls: list[tuple[int | None, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "record_packet_execution_intent",
        lambda _db, packet_id, payload: calls.append((packet_id, payload)),
    )
    sess = SimpleNamespace(
        id=42,
        state=STATE_LIVE_ENTERED,
        symbol="SOL-USD",
        variant_id=7,
        venue="coinbase",
        execution_family="coinbase_spot",
    )
    le = {
        "entry_decision_packet_id": 123,
        "position": {
            "quantity": 0.25,
            "avg_entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 106.0,
            "opened_at_utc": "2026-01-01T00:00:00",
        },
    }

    live_runner_mod._record_live_exit_intent_safe(
        MagicMock(),
        sess,
        le=le,
        reason="stop",
        product_id="SOL-USD",
        quantity=0.25,
        client_order_id="cid-exit",
        bid=97.5,
        ask=98.0,
        mid=97.75,
        extra={"stop_price": 98.0},
    )

    assert calls and calls[0][0] == 123
    payload = calls[0][1]
    assert payload["surface"] == "momentum_live_runner_exit"
    assert payload["side"] == "sell"
    assert payload["reason"] == "stop"
    assert payload["client_order_id"] == "cid-exit"
    assert payload["reference_notional_usd"] == pytest.approx(24.375)
    assert le["last_exit_intent"]["reason"] == "stop"
    assert le["exit_execution_intents"][-1]["product_id"] == "SOL-USD"


def test_live_exit_submit_failure_does_not_flatten_local_position(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=43,
        state=STATE_LIVE_ENTERED,
        risk_snapshot_json={},
        correlation_id="corr-exit-fail",
    )
    le = {
        "exit_client_order_id": "cid-exit",
        "position": {
            "quantity": 0.25,
            "avg_entry_price": 100.0,
        },
    }

    ok = live_runner_mod._live_exit_submit_succeeded(
        MagicMock(),
        sess,
        le=le,
        result={"ok": False, "error": "venue_down"},
        reason="stop",
    )

    assert ok is False
    assert le["position"]["quantity"] == 0.25
    assert le["last_exit_submit_failed"]["reason"] == "stop"
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"]["quantity"] == 0.25
    assert events and events[-1][0] == "live_exit_submit_failed"


def test_transient_exit_rail_failure_retries_past_submit_cap(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "crypto",
            "market_session": "regular",
            "is_tradable": True,
            "deferred_until_utc": None,
        },
    )
    adapter = SimpleNamespace(
        place_market_order=MagicMock(
            return_value={
                "ok": False,
                "error": "HTTPSConnectionPool: Max retries exceeded (Connection refused)",
            }
        )
    )
    sess = SimpleNamespace(
        id=431,
        state=STATE_LIVE_ENTERED,
        symbol="SOL-USD",
        execution_family="coinbase_spot",
        risk_snapshot_json={},
        correlation_id="corr-transient-cap",
    )
    le = {
        "exit_submit_attempts": live_runner_mod._EXIT_SUBMIT_MAX_ATTEMPTS,
        "last_exit_submit_failed": {
            "reason": "trail_stop",
            "result": {"ok": False, "error": "Connection refused"},
        },
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    out = live_runner_mod._submit_live_market_exit(
        MagicMock(),
        sess,
        adapter,
        le=le,
        product_id="SOL-USD",
        quantity=0.25,
        client_order_id="cid-transient-cap",
        reason="trail_stop",
        bid=99.0,
        ask=99.2,
        mid=99.1,
    )

    assert out.get("cap_exceeded") is not True
    adapter.place_market_order.assert_called_once()
    assert le["exit_submit_attempts"] == live_runner_mod._EXIT_SUBMIT_MAX_ATTEMPTS
    assert le["exit_next_retry_at_utc"]
    assert le["last_exit_rail_unhealthy_retry"]["last_error"] == "Connection refused"


def test_hard_exit_reject_still_stops_at_submit_cap(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    adapter = SimpleNamespace(place_market_order=MagicMock())
    sess = SimpleNamespace(
        id=432,
        state=STATE_LIVE_ENTERED,
        symbol="SOL-USD",
        execution_family="coinbase_spot",
        risk_snapshot_json={},
        correlation_id="corr-hard-cap",
    )
    le = {
        "exit_submit_attempts": live_runner_mod._EXIT_SUBMIT_MAX_ATTEMPTS,
        "last_exit_submit_failed": {
            "reason": "trail_stop",
            "result": {"ok": False, "error": "not enough shares to sell"},
        },
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    out = live_runner_mod._submit_live_market_exit(
        MagicMock(),
        sess,
        adapter,
        le=le,
        product_id="SOL-USD",
        quantity=0.25,
        client_order_id="cid-hard-cap",
        reason="trail_stop",
        bid=99.0,
        ask=99.2,
        mid=99.1,
    )

    assert out["cap_exceeded"] is True
    assert out["attempts"] == live_runner_mod._EXIT_SUBMIT_MAX_ATTEMPTS
    adapter.place_market_order.assert_not_called()


def test_live_exit_poll_waits_for_open_order(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(id=44, state=STATE_LIVE_ENTERED, risk_snapshot_json={}, correlation_id="corr-pending")
    le = {
        "exit_order_id": "ord-exit-open",
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            NormalizedOrder(
                order_id="ord-exit-open",
                client_order_id="cid-exit",
                product_id="SOL-USD",
                side="sell",
                status="OPEN",
                order_type="market",
                filled_size=0.0,
                average_filled_price=None,
            ),
            _fresh(),
        )
    )

    out = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=0.25,
    )

    assert out["pending"] is True
    assert le["position"]["quantity"] == 0.25
    assert le["last_exit_pending_confirmation"]["why"] == "exit_fill_pending"
    assert events and events[-1][0] == "live_exit_pending_confirmation"


def test_live_exit_missing_order_polls_backoff_and_dedupes(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=46,
        state=STATE_LIVE_BAILOUT,
        symbol="LGPS",
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={},
        correlation_id="corr-missing-order",
    )
    le = {
        "exit_order_id": "ord-missing",
        "exit_submit_attempts": 6,
        "pending_exit_reason": "kill_switch_flatten",
        "pending_exit_quantity": 44.0,
        "position": {"quantity": 44.0, "avg_entry_price": 1.16},
    }
    adapter = SimpleNamespace(
        get_order=MagicMock(return_value=(None, _fresh())),
        get_position_quantity=MagicMock(return_value=44.0),
    )

    first = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="kill_switch_flatten",
        quantity=44.0,
    )
    second = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="kill_switch_flatten",
        quantity=44.0,
    )

    assert first["pending"] is True
    assert first["why"] == "order_missing"
    assert second["pending"] is True
    assert second["why"] == "exit_poll_backoff"
    assert adapter.get_order.call_count == 1
    assert [event for event, _payload in events] == ["live_exit_pending_unconfirmed"]
    assert le["last_exit_pending_confirmation"]["why"] == "order_missing"


def test_agentic_exit_repeg_blocks_inside_spread_cost_band(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(settings, "chili_momentum_order_notional_guard_bps", 25.0, raising=False)
    adapter = SimpleNamespace(
        get_agentic_open_positions=MagicMock(return_value=[{"symbol": "LGPS", "quantity": 44.0}]),
        get_best_bid_ask=MagicMock(
            return_value=(
                NormalizedTicker(
                    product_id="LGPS",
                    bid=10.00,
                    ask=10.06,
                    mid=10.03,
                    spread_bps=59.8,
                    freshness=_fresh(),
                ),
                _fresh(),
            )
        ),
    )

    allowed, evidence = live_runner_mod._agentic_exit_limit_repeg_allowed(
        adapter,
        "LGPS",
        le={"exit_limit_price": 10.02},
    )

    assert allowed is False
    assert evidence["repeg_blocked_reason"] == "limit_above_bid_inside_cost_band"
    assert evidence["stale_gap_bps"] < evidence["min_stale_bps"]
    adapter.get_best_bid_ask.assert_called_once()


def test_agentic_exit_repeg_allows_when_stale_gap_exceeds_spread_cost_band(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(settings, "chili_momentum_order_notional_guard_bps", 25.0, raising=False)
    adapter = SimpleNamespace(
        get_agentic_open_positions=MagicMock(return_value=[{"symbol": "LGPS", "quantity": 44.0}]),
        get_best_bid_ask=MagicMock(
            return_value=(
                NormalizedTicker(
                    product_id="LGPS",
                    bid=10.00,
                    ask=10.02,
                    mid=10.01,
                    spread_bps=20.0,
                    freshness=_fresh(),
                ),
                _fresh(),
            )
        ),
    )

    allowed, evidence = live_runner_mod._agentic_exit_limit_repeg_allowed(
        adapter,
        "LGPS",
        le={"exit_limit_price": 10.20},
    )

    assert allowed is True
    assert evidence["repeg_reason"] == "limit_above_fresh_bid"
    assert evidence["stale_gap_bps"] > evidence["min_stale_bps"]


def test_live_exit_poll_defers_queued_exit_while_market_closed(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "post_market",
            "is_tradable": False,
            "deferred_until_utc": "2026-07-02T13:30:00+00:00",
        },
    )
    sess = SimpleNamespace(
        id=47,
        state=STATE_LIVE_ENTERED,
        symbol="LGPS",
        risk_snapshot_json={},
        correlation_id="corr-queued",
    )
    le = {
        "exit_order_id": "ord-queued",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 12.0,
        "position": {"quantity": 12.0, "avg_entry_price": 3.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            _exit_order(order_id="ord-queued", status="working", raw_state="queued"),
            _fresh(),
        )
    )

    out = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=12.0,
    )
    second = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=12.0,
    )

    assert out["pending"] is True
    assert out["deferred"] is True
    assert out["why"] == "exit_queued_non_tradable"
    assert out["market_session"] == "post_market"
    assert out["deferred_until_utc"] == "2026-07-02T13:30:00+00:00"
    assert out["expected_quantity"] == 12.0
    assert out["filled_size"] == 0.0
    assert out["order_status"] == "working"
    assert out["broker_order_status"] == "queued"
    assert second["deferred"] is True
    assert le["position"]["quantity"] == 12.0
    assert le["pending_exit_deferred_until_utc"] == "2026-07-02T13:30:00+00:00"
    assert le["pending_exit_market_session"] == "post_market"
    assert le["pending_exit_order_status"] == "queued"
    assert le["last_exit_deferred"]["why"] == "exit_queued_non_tradable"
    assert [event for event, _payload in events].count("live_exit_queued_deferred") == 1


def test_live_exit_poll_skips_broker_poll_for_deferred_non_tradable_exit(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "pre_market",
            "is_tradable": False,
            "deferred_until_utc": "2026-07-02T13:30:00+00:00",
        },
    )
    sess = SimpleNamespace(
        id=52,
        state=STATE_LIVE_BAILOUT,
        symbol="LGPS",
        risk_snapshot_json={},
        correlation_id="corr-skip-poll",
    )
    le = {
        "exit_order_id": "ord-old-queued",
        "pending_exit_reason": "kill_switch_flatten",
        "pending_exit_quantity": 12.0,
        "pending_exit_deferred_until_utc": "2026-07-02T13:30:00+00:00",
        "pending_exit_order_status": "existing_order_unpolled",
        "last_exit_deferred": {"why": "exit_existing_order_deferred_non_tradable"},
        "position": {"quantity": 12.0, "avg_entry_price": 3.0},
    }
    adapter = SimpleNamespace(get_order=MagicMock(side_effect=AssertionError("broker poll should be deferred")))

    out = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="kill_switch_flatten",
        quantity=12.0,
    )
    second = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="kill_switch_flatten",
        quantity=12.0,
    )

    adapter.get_order.assert_not_called()
    assert out["pending"] is True
    assert out["deferred"] is True
    assert second["deferred"] is True
    assert out["why"] == "exit_existing_order_deferred_non_tradable"
    assert out["broker_order_status"] == "existing_order_unpolled"
    assert le["last_exit_pending_confirmation"]["exit_lifecycle_state"] == "queued_deferred"
    assert [event for event, _payload in events].count("live_exit_queued_deferred") == 1


def test_live_exit_submit_defers_before_broker_when_equity_not_tradable(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "pre_market",
            "is_tradable": False,
            "deferred_until_utc": "2026-07-02T13:30:00+00:00",
        },
    )
    adapter = SimpleNamespace(place_market_order=MagicMock(side_effect=AssertionError("broker should not be called")))
    sess = SimpleNamespace(
        id=51,
        state=STATE_LIVE_BAILOUT,
        symbol="LGPS",
        risk_snapshot_json={},
        correlation_id="corr-presubmit-defer",
    )
    le = {
        "position": {"quantity": 12.0, "avg_entry_price": 3.0},
        "last_exit_submit_failed": {"error": "stale_broker_error"},
    }

    out = live_runner_mod._submit_live_market_exit(
        MagicMock(),
        sess,
        adapter,
        le=le,
        product_id="LGPS",
        quantity=12.0,
        client_order_id="cid-presubmit-defer",
        reason="kill_switch_flatten",
        bid=2.9,
        ask=3.0,
        mid=2.95,
    )

    adapter.place_market_order.assert_not_called()
    assert out["deferred"] is True
    assert out["why"] == "exit_submit_deferred_non_tradable"
    assert out["exit_lifecycle_state"] == "presubmit_deferred"
    assert out["deferred_until_utc"] == "2026-07-02T13:30:00+00:00"
    assert le["pending_exit_reason"] == "kill_switch_flatten"
    assert le["pending_exit_presubmit_deferred"] is True
    assert le["pending_exit_product_id"] == "LGPS"
    assert le["pending_exit_client_order_id"] == "cid-presubmit-defer"
    assert le["pending_exit_order_status"] == "not_submitted"
    assert "last_exit_submit_failed" not in le
    assert le["last_exit_deferred"]["why"] == "exit_submit_deferred_non_tradable"
    assert sess.risk_snapshot_json["momentum_live_execution"]["pending_exit_presubmit_deferred"] is True
    assert [event for event, _payload in events] == ["live_exit_queued_deferred"]


def test_live_exit_transport_outage_deferred_without_retry_cap_burn(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "regular_hours",
            "is_tradable": True,
            "deferred_until_utc": None,
        },
    )
    adapter = SimpleNamespace(
        get_position_quantity=MagicMock(return_value=2.0),
        get_agentic_open_orders=MagicMock(return_value=[]),
        place_limit_order_gtc=MagicMock(
            return_value={
                "ok": False,
                "code": "rail_transport_unavailable",
                "error": "Robinhood Agentic MCP transport unavailable",
                "retry_after_seconds": 15.0,
                "client_order_id": "cid-outage",
            }
        ),
    )
    sess = SimpleNamespace(
        id=53,
        state=STATE_LIVE_ENTERED,
        symbol="PRGS",
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={},
        correlation_id="corr-rh-outage",
    )
    le = {
        "position": {"quantity": 2.0, "avg_entry_price": 39.3},
        "exit_submit_attempts": 0,
    }

    out = live_runner_mod._submit_live_market_exit(
        MagicMock(),
        sess,
        adapter,
        le=le,
        product_id="PRGS",
        quantity=2.0,
        client_order_id="cid-outage",
        reason="trail_stop",
        bid=39.8,
        ask=39.9,
        mid=39.85,
    )

    assert out["deferred"] is True
    assert out["code"] == "rail_transport_unavailable"
    assert out["attempts"] == 0
    assert le["exit_submit_attempts"] == 0
    assert le["last_exit_deferred"]["code"] == "rail_transport_unavailable"
    assert le["last_exit_deferred"]["retry_after_seconds"] == pytest.approx(15.0)
    assert "last_exit_submit_failed" not in le
    assert "exit_order_id" not in le


def test_live_exit_transport_outage_uses_configured_backoff_when_prior_attempts_high(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "regular_hours",
            "is_tradable": True,
            "deferred_until_utc": None,
        },
    )
    prior_attempts = max(1, live_runner_mod._EXIT_SUBMIT_MAX_ATTEMPTS - 1)
    expected_retry_s = live_runner_mod._exit_submit_backoff_seconds(prior_attempts + 1)
    adapter_retry_s = max(expected_retry_s / 10.0, 0.001)
    outage_result = {
        "ok": False,
        "code": "rail_transport_unavailable",
        "error": "Robinhood Agentic MCP transport unavailable",
        "retry_after_seconds": adapter_retry_s,
        "client_order_id": "cid-outage-high",
    }
    adapter = SimpleNamespace(
        get_position_quantity=MagicMock(return_value=2.0),
        get_agentic_open_orders=MagicMock(return_value=[]),
        place_limit_order_gtc=MagicMock(return_value=outage_result),
        place_market_order=MagicMock(return_value=outage_result),
    )
    sess = SimpleNamespace(
        id=54,
        state=STATE_LIVE_ENTERED,
        symbol="PRGS",
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={},
        correlation_id="corr-rh-outage-high",
    )
    le = {
        "position": {"quantity": 2.0, "avg_entry_price": 39.3},
        "exit_submit_attempts": prior_attempts,
    }

    out = live_runner_mod._submit_live_market_exit(
        MagicMock(),
        sess,
        adapter,
        le=le,
        product_id="PRGS",
        quantity=2.0,
        client_order_id="cid-outage-high",
        reason="trail_stop",
        bid=39.8,
        ask=39.9,
        mid=39.85,
    )

    assert out["deferred"] is True
    assert out["attempts"] == prior_attempts
    assert le["exit_submit_attempts"] == prior_attempts
    assert le["last_exit_deferred"]["retry_after_seconds"] == pytest.approx(expected_retry_s)
    assert le["last_exit_deferred"]["retry_after_seconds"] > adapter_retry_s
    assert "last_exit_submit_failed" not in le


def test_live_exit_poll_adopts_deferred_order_when_active(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "regular_hours",
            "is_tradable": True,
            "deferred_until_utc": None,
        },
    )
    sess = SimpleNamespace(
        id=48,
        state=STATE_LIVE_ENTERED,
        symbol="LGPS",
        risk_snapshot_json={},
        correlation_id="corr-adopt",
    )
    le = {
        "exit_order_id": "ord-active",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 12.0,
        "pending_exit_deferred_until_utc": "2026-07-02T13:30:00+00:00",
        "pending_exit_market_session": "post_market",
        "pending_exit_order_status": "queued",
        "position": {"quantity": 12.0, "avg_entry_price": 3.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            _exit_order(order_id="ord-active", status="OPEN", raw_state="confirmed"),
            _fresh(),
        )
    )

    out = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=12.0,
    )

    assert out["pending"] is True
    assert out.get("deferred") is None
    assert out["why"] == "exit_fill_pending"
    assert out["exit_lifecycle_state"] == "active_pending"
    assert "pending_exit_deferred_until_utc" not in le
    assert "pending_exit_market_session" not in le
    assert le["last_exit_deferred_adopted"]["why"] == "exit_deferred_order_active"
    assert le["last_exit_pending_confirmation"]["market_session"] == "regular_hours"
    assert [event for event, _payload in events] == [
        "live_exit_deferred_adopted",
        "live_exit_pending_confirmation",
    ]


def test_live_exit_poll_rejected_no_fill_clears_for_retry(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "regular_hours",
            "is_tradable": True,
            "deferred_until_utc": None,
        },
    )
    sess = SimpleNamespace(
        id=49,
        state=STATE_LIVE_ENTERED,
        symbol="LGPS",
        risk_snapshot_json={},
        correlation_id="corr-rejected",
    )
    le = {
        "exit_order_id": "ord-rejected",
        "exit_client_order_id": "cid-rejected",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 12.0,
        "pending_exit_deferred_until_utc": "2026-07-02T13:30:00+00:00",
        "position": {"quantity": 12.0, "avg_entry_price": 3.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            _exit_order(order_id="ord-rejected", status="rejected", raw_state="rejected"),
            _fresh(),
        )
    )

    out = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=12.0,
    )

    assert out["failed"] is True
    assert out["why"] == "terminal_no_fill"
    assert out["exit_lifecycle_state"] == "terminal_no_fill_retry_ready"
    assert out["expected_quantity"] == 12.0
    assert out["filled_size"] == 0.0
    assert out["broker_order_status"] == "rejected"
    assert le["position"]["quantity"] == 12.0
    assert "pending_exit_reason" not in le
    assert "pending_exit_quantity" not in le
    assert "pending_exit_deferred_until_utc" not in le
    assert "exit_order_id" not in le
    assert "exit_client_order_id" not in le
    assert le["last_exit_terminal_no_fill"]["order_id"] == "ord-rejected"
    assert events and events[-1][0] == "live_exit_terminal_no_fill"


def test_live_exit_poll_terminal_partial_then_updates_remainder(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "regular_hours",
            "is_tradable": True,
            "deferred_until_utc": None,
        },
    )
    sess = SimpleNamespace(
        id=50,
        state=STATE_LIVE_ENTERED,
        mode="live",
        symbol="LGPS",
        risk_snapshot_json={},
        correlation_id="corr-partial-poll",
    )
    le = {
        "exit_order_id": "ord-partial",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            _exit_order(
                order_id="ord-partial",
                status="cancelled",
                raw_state="cancelled",
                filled_size=0.1,
                average_filled_price=99.0,
            ),
            _fresh(),
        )
    )

    poll = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=0.25,
    )
    assert poll["partial"] is True
    assert poll["filled_size"] == pytest.approx(0.1)

    live_runner_mod._apply_confirmed_live_partial_exit(
        MagicMock(),
        sess,
        le=le,
        filled_quantity=float(poll["filled_size"]),
        entry_price=100.0,
        fill_price=float(poll["fill_price"]),
        reason="stop",
    )

    assert sess.state == STATE_LIVE_ENTERED
    assert le["position"]["quantity"] == pytest.approx(0.15)
    assert "pending_exit_reason" not in le
    assert events[-1][0] == "live_partial_exit_filled"


def test_live_exit_poll_raw_robinhood_filled_then_flattens(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(live_runner_mod, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        live_runner_mod,
        "_exit_market_window",
        lambda _symbol: {
            "asset_class": "stock",
            "market_session": "regular_hours",
            "is_tradable": True,
            "deferred_until_utc": None,
        },
    )
    sess = SimpleNamespace(
        id=51,
        state=STATE_LIVE_ENTERED,
        mode="live",
        symbol="LGPS",
        risk_snapshot_json={},
        correlation_id="corr-filled-poll",
    )
    le = {
        "exit_order_id": "ord-filled",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            _exit_order(
                order_id="ord-filled",
                status="open",
                raw_state="filled",
                filled_size=0.25,
                average_filled_price=99.0,
            ),
            _fresh(),
        )
    )

    poll = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=0.25,
    )
    assert poll["filled"] is True
    assert poll["broker_order_status"] == "filled"

    live_runner_mod._complete_confirmed_live_exit(
        MagicMock(),
        sess,
        le=le,
        quantity=0.25,
        entry_price=100.0,
        fill_price=float(poll["fill_price"]),
        reason="stop",
        slip_bps=6.0,
    )

    assert sess.state == STATE_LIVE_EXITED
    assert le["position"] is None
    assert "pending_exit_reason" not in le
    assert events[-1][0] == "live_exit_filled"


def test_confirmed_live_exit_is_the_only_flatten_path(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(live_runner_mod, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=45,
        state=STATE_LIVE_ENTERED,
        mode="live",
        symbol="BTC-USD",
        risk_snapshot_json={},
        correlation_id="corr-confirmed",
    )
    le = {
        "exit_order_id": "ord-exit-filled",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    pnl = live_runner_mod._complete_confirmed_live_exit(
        MagicMock(),
        sess,
        le=le,
        quantity=0.25,
        entry_price=100.0,
        fill_price=99.0,
        reason="stop",
        slip_bps=6.0,
    )

    assert pnl == pytest.approx(-0.25)
    assert sess.state == STATE_LIVE_EXITED
    assert le["position"] is None
    assert "pending_exit_reason" not in le
    assert le["last_exit_notional_basis_usd"] == pytest.approx(25.0)
    assert le["last_exit_return_bps"] == pytest.approx(-100.0)
    assert sess.risk_snapshot_json["momentum_live_execution"]["last_exit_reason"] == "stop"
    assert events and events[-1][0] == "live_exit_filled"


def test_terminal_partial_live_exit_reduces_position_without_flattening(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=46,
        state=STATE_LIVE_SCALING_OUT,
        mode="live",
        risk_snapshot_json={},
        correlation_id="corr-partial",
    )
    le = {
        "exit_order_id": "ord-exit-partial",
        "pending_exit_reason": "target",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    pnl = live_runner_mod._apply_confirmed_live_partial_exit(
        MagicMock(),
        sess,
        le=le,
        filled_quantity=0.1,
        entry_price=100.0,
        fill_price=101.0,
        reason="target",
    )

    assert pnl == pytest.approx(0.1)
    assert sess.state == STATE_LIVE_SCALING_OUT
    assert le["position"]["quantity"] == pytest.approx(0.15)
    assert "pending_exit_reason" not in le
    assert le["last_partial_exit_notional_basis_usd"] == pytest.approx(10.0)
    assert le["last_partial_exit_return_bps"] == pytest.approx(100.0)
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"]["quantity"] == pytest.approx(0.15)
    assert events and events[-1][0] == "live_partial_exit_filled"


def test_list_runnable_live_ignores_paper(db: Session) -> None:
    from app.models.trading import MomentumStrategyVariant
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
        ensure_momentum_strategy_variants,
    )
    from app.services.trading.momentum_neural.paper_fsm import STATE_QUEUED as PQ

    uid = _uid(db, "mix")
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    blocked_a = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="PAP-USD",
        variant_id=v.id,
        mode="paper",
        state=PQ,
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
    )
    blocked_b = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LIV-USD",
        variant_id=v.id,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
    )
    db.commit()
    live_rows = list_runnable_live_sessions(db, limit=50)
    assert all(r.mode == "live" for r in live_rows)
    paper_rows = list_runnable_paper_sessions(db, limit=50)
    assert all(r.mode == "paper" for r in paper_rows)


def test_tick_live_armed_to_watching(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid = _variant_id_for_live_test(db, symbol="TL1-USD")
    uid = _uid(db, "tl1")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="TL1-USD",
        variant_id=vid,
        mode="live",
        state="armed_pending_runner",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": "2026-01-01T00:00:00+00:00"},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
        },
        correlation_id="c-live-1",
    )
    db.commit()
    ad = _mk_adapter()

    def factory():
        return ad

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        r1 = tick_live_session(db, sess.id, adapter_factory=factory)
    assert r1.get("ok")
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_QUEUED_LIVE

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        r2 = tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_WATCHING_LIVE


def test_kill_switch_blocks_before_entry(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid = _variant_id_for_live_test(db, symbol="KS-USD")
    uid = _uid(db, "ks")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="KS-USD",
        variant_id=vid,
        mode="live",
        state="armed_pending_runner",
            risk_snapshot_json={
                RISK_SNAPSHOT_KEY: {"allowed": True},
                "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
                "entry_pending_place_utc": datetime.now(timezone.utc).isoformat(),
            },
        )
    db.commit()
    ad = _mk_adapter()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=True):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert out.get("blocked") or sess.state == STATE_LIVE_ERROR
    assert sess.state == STATE_LIVE_ERROR
    ad.place_market_order.assert_not_called()


def test_live_cooldown_finishes_instead_of_same_session_recycle(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_same_session_reentry_enabled", False, raising=False)
    vid = _variant_id_for_live_test(db, symbol="CDN-USD")
    uid = _uid(db, "cooldown-finish")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="CDN-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_COOLDOWN,
        execution_family="coinbase_spot",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
            "momentum_live_execution": {
                "cooldown_until_utc": "2026-01-01T00:00:00+00:00",
                "realized_pnl_usd": -1.23,
                "last_exit_reason": "stop",
            },
        },
        correlation_id="c-cooldown-finish",
    )
    db.commit()
    ad = _mk_adapter()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]

    assert out.get("ok") is True
    assert sess.state == STATE_LIVE_FINISHED
    assert "cooldown_until_utc" not in le
    ad.place_market_order.assert_not_called()


def test_ross_equity_live_cooldown_finishes_even_when_generic_reentry_enabled(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_same_session_reentry_enabled", True, raising=False)
    vid = _variant_id_for_live_test(db, symbol="TC")
    uid = _uid(db, "ross-equity-cooldown-finish")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="TC",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_COOLDOWN,
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
            "momentum_live_execution": {
                "cooldown_until_utc": "2026-01-01T00:00:00+00:00",
                "realized_pnl_usd": 0.01,
                "last_exit_reason": "target",
                "trade_cycles": 1,
                "entry_order_id": "filled-entry",
                "entry_submitted": True,
            },
        },
        correlation_id="c-ross-equity-cooldown-finish",
    )
    db.commit()
    ad = _mk_adapter()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]

    assert out.get("ok") is True
    assert sess.state == STATE_LIVE_FINISHED
    assert le["trade_cycles"] == 1
    assert "cooldown_until_utc" not in le
    ad.place_market_order.assert_not_called()


def test_ross_equity_finished_session_is_inert_before_adapter_resolution(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_same_session_reentry_enabled", True, raising=False)
    vid = _variant_id_for_live_test(db, symbol="TC")
    uid = _uid(db, f"ross-equity-finished-inert-{uuid4().hex[:8]}")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="TC",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_FINISHED,
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_live_execution": {
                "trade_cycles": 1,
                "entry_order_id": "filled-entry",
                "entry_submitted": True,
                "last_exit_reason": "target",
                "realized_pnl_usd": 0.01,
            },
        },
        correlation_id="c-ross-equity-finished-inert",
    )
    db.commit()
    adapter_factory = MagicMock()

    out = tick_live_session(db, sess.id, adapter_factory=adapter_factory)

    db.commit()
    db.refresh(sess)
    assert out == {"ok": True, "skipped": "not_runnable", "state": STATE_LIVE_FINISHED}
    assert sess.state == STATE_LIVE_FINISHED
    adapter_factory.assert_not_called()


def test_expired_pre_entry_session_terminalizes_before_adapter_resolution(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid = _variant_id_for_live_test(db, symbol="JEM")
    uid = _uid(db, f"expired-pre-entry-{uuid4().hex[:8]}")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        variant_id=vid,
        mode="live",
        state=STATE_QUEUED_LIVE,
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "expires_at_utc": expired,
        },
        correlation_id="c-expired-pre-entry-event-runner",
    )
    db.commit()
    adapter_factory = MagicMock()

    out = tick_live_session(db, sess.id, adapter_factory=adapter_factory)

    db.commit()
    db.refresh(sess)
    assert out["ok"] is True
    assert out["skipped"] == "expired_pre_entry"
    assert sess.state == STATE_LIVE_CANCELLED
    adapter_factory.assert_not_called()


def test_unexpired_pre_entry_session_still_reaches_adapter(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid = _variant_id_for_live_test(db, symbol="JEM")
    uid = _uid(db, f"unexpired-pre-entry-{uuid4().hex[:8]}")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        variant_id=vid,
        mode="live",
        state=STATE_QUEUED_LIVE,
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "expires_at_utc": future,
        },
        correlation_id="c-unexpired-pre-entry-event-runner",
    )
    db.commit()
    ad = _mk_adapter()
    ad.is_enabled.return_value = False

    out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.commit()
    db.refresh(sess)
    assert out["skipped"] == "venue_adapter_unavailable"
    assert sess.state == STATE_QUEUED_LIVE
    assert ad.is_enabled.called


def test_ross_equity_prior_completed_cycle_in_watching_cannot_reenter(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_same_session_reentry_enabled", True, raising=False)
    vid = _variant_id_for_live_test(db, symbol="TC")
    uid = _uid(db, f"ross-equity-prior-cycle-watch-{uuid4().hex[:8]}")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="TC",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        execution_family="robinhood_agentic_mcp",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                "trade_cycles": 1,
                "last_exit_reason": "target",
                "realized_pnl_usd": 0.0123,
            },
        },
        correlation_id="c-ross-equity-prior-cycle-watch",
    )
    db.commit()
    ad = _mk_adapter()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.commit()
    db.refresh(sess)

    assert out == {
        "ok": True,
        "session_id": sess.id,
        "state": STATE_LIVE_FINISHED,
        "skipped": "ross_equity_prior_trade_cycle",
    }
    assert sess.state == STATE_LIVE_FINISHED
    ad.place_market_order.assert_not_called()
    ad.place_limit_order_gtc.assert_not_called()


def test_live_cooldown_recycle_clears_prior_entry_lifecycle(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_same_session_reentry_enabled", True, raising=False)
    vid = _variant_id_for_live_test(db, symbol="RCY-USD")
    uid = _uid(db, "cooldown-recycle-reset")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    prior_order_id = "old-entry-order"
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="RCY-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_COOLDOWN,
        execution_family="coinbase_spot",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
            "momentum_live_execution": {
                "cooldown_until_utc": "2026-01-01T00:00:00+00:00",
                "realized_pnl_usd": 0.12,
                "last_exit_reason": "target",
                "trade_cycles": 1,
                "entry_order_id": prior_order_id,
                "entry_order_ids_all": [prior_order_id],
                "entry_orders_resolved": {prior_order_id: "filled"},
                "entry_submitted": True,
                "position": {"quantity": 10, "avg_entry_price": 1.0},
                "entry_pending_place_utc": "2026-07-01T17:31:53+00:00",
                "watch_break_level": 1.05,
                "structural_stop_price": 0.95,
            },
        },
        correlation_id="c-cooldown-recycle-reset",
    )
    db.commit()
    ad = _mk_adapter()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]

    assert out.get("ok") is True
    assert sess.state == STATE_WATCHING_LIVE
    assert le["trade_cycles"] == 2
    for key in (
        "entry_order_id",
        "entry_order_ids_all",
        "entry_orders_resolved",
        "entry_submitted",
        "position",
        "entry_pending_place_utc",
        "watch_break_level",
        "structural_stop_price",
    ):
        assert key not in le
    ad.place_market_order.assert_not_called()


def test_unavailable_adapter_blocks_preentry_but_not_held_position(monkeypatch, db: Session) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(live_runner_mod, "_venue_broker_connected", lambda _ef: False)
    monkeypatch.setattr(live_runner_mod, "_reconcile_venue_position", lambda *_args, **_kwargs: None)
    vid = _variant_id_for_live_test(db, symbol="HLD-USD")
    uid = _uid(db, "held_adapter_down")
    base_snapshot = {
        RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": "2026-01-01T00:00:00+00:00"},
        "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        "momentum_policy_caps": {"max_notional_per_trade_usd": 80, "max_hold_seconds": 3600},
    }
    pre = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="HLD-USD",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json=dict(base_snapshot),
    )
    held = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="HLD-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_ENTERED,
        risk_snapshot_json={
            **base_snapshot,
            "momentum_live_execution": {
                "position": {
                    "quantity": 0.25,
                    "avg_entry_price": 100.0,
                    "stop_price": 95.0,
                    "target_price": 110.0,
                    "high_water_mark": 100.0,
                    "opened_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
        },
    )
    db.commit()
    ad = _mk_adapter()
    ad.is_enabled.return_value = False
    ad.place_limit_order_gtc.return_value = {"ok": False, "error": "venue_down"}
    ad.place_stop_market_order.return_value = {"ok": False, "error": "venue_down"}
    ad.place_stop_limit_order.return_value = {"ok": False, "error": "venue_down"}

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        pre_out = tick_live_session(db, pre.id, adapter_factory=lambda: ad)
        held_out = tick_live_session(db, held.id, adapter_factory=lambda: ad)

    assert pre_out.get("skipped") == "venue_adapter_unavailable"
    assert held_out.get("skipped") not in {"venue_adapter_unavailable", "venue_broker_not_connected"}
    assert ad.get_best_bid_ask.called
    assert ad.is_enabled.call_count == 1


def test_batch_prefilter_adapter_unavailable_does_not_starve_later_equity(monkeypatch, db: Session) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    from app.services.trading.execution_family_registry import (
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
    )
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(live_runner_mod, "_venue_broker_connected", lambda _ef: True)
    vid = _variant_id_for_live_test(db, symbol="STARVE-USD")
    uid = _uid(db, "batch_starvation")

    def _snapshot(score: float) -> dict:
        return {
            RISK_SNAPSHOT_KEY: {"allowed": True, "viability_score": score},
            "viability_score": score,
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 80, "max_hold_seconds": 3600},
        }

    # These crypto rows rank ahead on quality, but their adapter is unavailable.
    blocked_a = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="AAA-USD",
        variant_id=vid,
        mode="live",
        state=STATE_QUEUED_LIVE,
        execution_family=EXECUTION_FAMILY_COINBASE_SPOT,
        risk_snapshot_json=_snapshot(0.99),
    )
    blocked_b = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="BBB-USD",
        variant_id=vid,
        mode="live",
        state=STATE_QUEUED_LIVE,
        execution_family=EXECUTION_FAMILY_COINBASE_SPOT,
        risk_snapshot_json=_snapshot(0.98),
    )
    equity = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_ENTERED,
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        risk_snapshot_json={
            **_snapshot(0.25),
            "momentum_live_execution": {
                "position": {
                    "quantity": 10,
                    "avg_entry_price": 2.0,
                    "stop_price": 1.9,
                    "target_price": 2.2,
                    "opened_at_utc": "2026-07-01T11:05:00+00:00",
                }
            },
        },
    )
    db.commit()

    unavailable = _mk_adapter()
    unavailable.is_enabled.return_value = False
    available = _mk_adapter()
    available.is_enabled.return_value = True

    def _factory_for_family(ef: str):
        if ef == EXECUTION_FAMILY_COINBASE_SPOT:
            return lambda: unavailable
        if ef == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
            return lambda: available
        raise AssertionError(f"unexpected execution_family={ef}")

    monkeypatch.setattr(live_runner_mod, "resolve_live_spot_adapter_factory", _factory_for_family)

    plan = live_runner_mod.plan_live_runner_batch_sessions(
        db,
        limit=3,
        session_ids={blocked_a.id, blocked_b.id, equity.id},
    )

    assert plan["session_ids"] == [equity.id]
    assert plan["symbols"] == ["JEM"]
    assert plan["capacity_limit"] == 3
    assert plan["availability_probe_families"] == [
        EXECUTION_FAMILY_COINBASE_SPOT,
    ]
    unavailable_skips = [
        r for r in plan["prefilter_results"]
        if r.get("skipped") == "venue_adapter_unavailable"
    ]
    assert len(unavailable_skips) >= 1
    assert all(r.get("batch_capacity_consumed") is False for r in unavailable_skips)
    assert plan["candidate_count"] == 3
    assert unavailable.is_enabled.call_count == 1
    assert available.is_enabled.call_count == 0

    from app.models.trading import TradingAutomationEvent

    snapshot = (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.event_type == "live_replay_scheduler_snapshot")
        .order_by(TradingAutomationEvent.id.desc())
        .first()
    )
    assert snapshot is not None
    payload = snapshot.payload_json
    assert payload["source"] == "plan_live_runner_batch_sessions"
    assert payload["capacity_limit"] == 3
    assert payload["selected_session_ids"] == [equity.id]
    assert {int(row["session_id"]) for row in payload["rows"]} == {blocked_a.id, blocked_b.id, equity.id}
    assert any(
        state["execution_family"] == EXECUTION_FAMILY_COINBASE_SPOT
        and state["adapter_available"] is False
        for state in payload["venue_states"]
    )


def test_batch_prefilter_wrong_venue_does_not_starve_later_equity(monkeypatch, db: Session) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    from app.services.trading.execution_family_registry import (
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
    )
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(live_runner_mod, "_venue_broker_connected", lambda _ef: True)
    vid = _variant_id_for_live_test(db, symbol="WRONGVENUE-USD")
    uid = _uid(db, "batch_wrong_venue")

    def _snapshot(score: float) -> dict:
        return {
            RISK_SNAPSHOT_KEY: {"allowed": True, "viability_score": score},
            "viability_score": score,
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 80, "max_hold_seconds": 3600},
        }

    wrong_venue = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="AAPL",
        variant_id=vid,
        mode="live",
        state=STATE_QUEUED_LIVE,
        execution_family=EXECUTION_FAMILY_COINBASE_SPOT,
        risk_snapshot_json=_snapshot(0.99),
    )
    equity = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        variant_id=vid,
        mode="live",
        state=STATE_QUEUED_LIVE,
        execution_family=EXECUTION_FAMILY_ROBINHOOD_SPOT,
        risk_snapshot_json=_snapshot(0.40),
    )
    db.commit()

    available = _mk_adapter()
    available.is_enabled.return_value = True

    def _factory_for_family(ef: str):
        if ef == EXECUTION_FAMILY_ROBINHOOD_SPOT:
            return lambda: available
        raise AssertionError(f"wrong-venue row should not resolve adapter, execution_family={ef}")

    monkeypatch.setattr(live_runner_mod, "resolve_live_spot_adapter_factory", _factory_for_family)

    plan = live_runner_mod.plan_live_runner_batch_sessions(
        db,
        limit=1,
        session_ids={wrong_venue.id, equity.id},
    )

    assert plan["session_ids"] == [equity.id]
    assert plan["symbols"] == ["JEM"]
    skipped = [
        r for r in plan["prefilter_results"]
        if r.get("skipped") == "venue_asset_class_mismatch"
    ]
    assert len(skipped) == 1
    assert skipped[0]["session_id"] == wrong_venue.id
    assert skipped[0]["batch_capacity_consumed"] is False
    assert plan["capacity_limit"] == 1
    assert plan["candidate_count"] == 2


def test_batch_planner_skips_ross_equity_preentry_for_scheduler_wall(monkeypatch, db: Session) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    from app.services.trading.execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid = _variant_id_for_live_test(db, symbol="TC")
    uid = _uid(db, "ross_scheduler_entry_wall")

    def _snapshot(score: float) -> dict:
        return {
            RISK_SNAPSHOT_KEY: {"allowed": True, "viability_score": score},
            "viability_score": score,
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 80, "max_hold_seconds": 3600},
        }

    queued = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="TC",
        variant_id=vid,
        mode="live",
        state=STATE_QUEUED_LIVE,
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        risk_snapshot_json=_snapshot(0.90),
    )
    pending = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LHAI",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        risk_snapshot_json={
            **_snapshot(0.80),
            "momentum_live_execution": {"entry_pending_place_utc": "2026-07-01T11:05:00+00:00"},
        },
    )
    held = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="JEM",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_ENTERED,
        execution_family=EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        risk_snapshot_json={
            **_snapshot(0.70),
            "momentum_live_execution": {
                "position": {
                    "quantity": 10,
                    "avg_entry_price": 2.0,
                    "stop_price": 1.9,
                    "target_price": 2.2,
                    "opened_at_utc": "2026-07-01T11:05:00+00:00",
                }
            },
        },
    )
    db.commit()

    plan = live_runner_mod.plan_live_runner_batch_sessions(
        db,
        limit=10,
        session_ids={queued.id, pending.id, held.id},
    )

    assert plan["session_ids"] == [held.id]
    skipped = {
        int(r["session_id"]): r
        for r in plan["prefilter_results"]
        if r.get("skipped") == "ross_equity_scheduler_entry_wall"
    }
    assert set(skipped) == {queued.id, pending.id}
    assert all(r.get("batch_capacity_consumed") is False for r in skipped.values())


def test_wide_live_bbo_blocks_market_entry_without_error(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
    monkeypatch.setattr(settings, "chili_momentum_skip_spread_gate_for_limit_entry", False, raising=False)
    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    monkeypatch.setattr(live_runner_mod, "_entry_pending_place_max_age_seconds", lambda: 600.0)
    vid, _ = _seed_live_eligible_row(db, symbol="WID-USD")
    uid = _uid(db, "wide")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="WID-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                "entry_pending_place_utc": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    db.commit()
    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="WID-USD",
            bid=99.0,
            ask=101.0,
            mid=100.0,
            spread_bps=200.0,
            freshness=fresh,
        ),
        fresh,
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert out == {"ok": True, "blocked": True, "reason": "wide_bbo_spread"}
    assert sess.state == STATE_WATCHING_LIVE
    ad.place_market_order.assert_not_called()
    gate = (sess.risk_snapshot_json or {})["momentum_live_execution"]["last_quote_quality_gate"]
    assert gate["spread_bps"] == 200.0


def test_boundary_risk_block_still_refreshes_live_tick_heartbeat(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="RISK")
    uid = _uid(db, "risk-heartbeat")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="RISK",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_live_execution": {"tick_count": 4, "last_mid": 0.91},
        },
    )
    db.commit()
    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="RISK",
            bid=1.09,
            ask=1.11,
            mid=1.10,
            spread_bps=181.8,
            freshness=fresh,
        ),
        fresh,
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False), patch(
        "app.services.trading.momentum_neural.live_runner._replay_aware_fetch_ohlcv_df",
        return_value=None,
    ), patch(
        "app.services.trading.momentum_neural.live_runner.runner_boundary_risk_ok",
        return_value=(False, {"severity": "block", "errors": ["test_risk_block"]}),
    ):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert out["blocked"] is True
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert le["tick_count"] == 5
    assert le["last_mid"] == pytest.approx(1.10)
    assert le.get("last_tick_utc")


def test_live_execution_summary_persisted(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid = _variant_id_for_live_test(db, symbol="SNP-USD")
    uid = _uid(db, "snp")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="SNP-USD",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 80, "max_hold_seconds": 3600},
        },
    )
    db.commit()
    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
        db.commit()
        db.refresh(sess)
    snap = sess.risk_snapshot_json or {}
    assert "momentum_live_execution" in snap
    assert int(snap["momentum_live_execution"].get("tick_count") or 0) >= 1


def test_watching_live_persists_score_wait_gate(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", False)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", False)

    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    variant = MomentumStrategyVariant(
        family="impulse_breakout",
        variant_key=f"score_wait_gate_{datetime.utcnow().timestamp()}",
        version=1,
        label="Score wait gate test",
        params_json={},
        execution_family="robinhood_spot",
    )
    db.add(variant)
    db.flush()
    via = MomentumSymbolViability(
        symbol="CLRO",
        variant_id=int(variant.id),
        scope="symbol",
        viability_score=0.48,
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=datetime.utcnow(),
        regime_snapshot_json={"volatility_regime": "normal", "atr_pct": 0.02},
        execution_readiness_json={"spread_bps": 10.0, "extra": {"ross_signals": {}}},
        explain_json={"test": "score_wait_gate"},
        evidence_window_json={},
        source_node_id="test_score_wait_gate",
        correlation_id="test-score-wait-gate",
    )
    db.add(via)
    db.flush()
    sess = create_trading_automation_session(
        db,
        user_id=_uid(db, "score-wait-gate"),
        symbol="CLRO",
        variant_id=int(variant.id),
        mode="live",
        state=STATE_WATCHING_LIVE,
        execution_family="robinhood_spot",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
        },
    )
    db.commit()

    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="CLRO",
            bid=3.62,
            ask=3.64,
            mid=3.63,
            spread_bps=55.1,
            freshness=fresh,
        ),
        fresh,
    )
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False), patch(
        "app.services.trading.momentum_neural.live_runner._replay_aware_fetch_ohlcv_df",
        return_value=None,
    ), patch.object(live_runner_mod, "_venue_broker_connected", lambda _ef: True):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert out["ok"] is True
    gate = (sess.risk_snapshot_json or {})["momentum_live_execution"]["last_entry_wait_gate"]
    assert gate["stage"] == "score"
    assert gate["reason"] == "score_below_entry_min"
    assert gate["viability_score"] == pytest.approx(0.48)
    assert gate["live_eligible_ok"] is True
    assert gate["ross_audio_starter"] is False


def test_tick_scalp_reclaim_submits_entry_same_runner_tick(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_tick_first_pullback_enabled", True)
    monkeypatch.setattr(settings, "chili_autotrader_allow_extended_hours", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", True)
    monkeypatch.setattr(settings, "chili_momentum_decouple_watching_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_atomic_risk_budget_enabled", False)
    monkeypatch.setattr(settings, "chili_feature_parity_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_sticky_backside_bench_enabled", False)

    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    import app.services.trading.momentum_neural.market_profile as market_profile_mod
    from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    monkeypatch.setattr(live_runner_mod, "_venue_broker_connected", lambda _ef: True)
    monkeypatch.setattr(live_runner_mod, "_entry_pricebook_snapshot", lambda _symbol: None)
    monkeypatch.setattr(live_runner_mod, "_l2_entry_confirm", lambda *a, **k: ("confirm", {}))
    decision_ledger = MagicMock(side_effect=AssertionError("tick scalps must not block on decision ledger"))
    monkeypatch.setattr(live_runner_mod, "run_momentum_entry_decision", decision_ledger)
    monkeypatch.setattr(market_profile_mod, "market_open_now", lambda *a, **k: True)
    monkeypatch.setattr(market_profile_mod, "schedule_window_now", lambda *a, **k: "hot")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )

    variant = MomentumStrategyVariant(
        family="impulse_breakout",
        variant_key=f"tick_scalp_test_{datetime.utcnow().timestamp()}",
        version=1,
        label="Tick scalp test",
        params_json={},
        execution_family="robinhood_spot",
    )
    db.add(variant)
    db.flush()
    vid = int(variant.id)
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "CANF", MomentumSymbolViability.variant_id == vid)
        .one_or_none()
    )
    readiness = {
        "spread_bps": 5.0,
        "slippage_estimate_bps": 6.0,
        "extra": {
            "ross_signals": {
                "CANF": {
                    "ticker": "CANF",
                    "price": 6.79,
                    "daily_change_pct": 128.62,
                    "gap_pct": 119.19,
                    "dollar_volume": 12_000_000,
                    "volume": 1_800_000,
                    "rvol_pace": 23.76,
                    "float_shares": 2_120_000,
                    "scanner_source": "Ross's 5 Pillars Alert (Online)",
                }
            }
        },
    }
    if via is None:
        via = MomentumSymbolViability(
            symbol="CANF",
            variant_id=vid,
            scope="symbol",
            viability_score=0.48,
            paper_eligible=True,
            live_eligible=False,
            freshness_ts=datetime.utcnow(),
            regime_snapshot_json={"volatility_regime": "normal", "atr_pct": 0.02},
            execution_readiness_json=readiness,
            explain_json={"test": "tick_scalp"},
            evidence_window_json={},
            source_node_id="test_tick_scalp",
            correlation_id="test-canf-scalp",
        )
        db.add(via)
    else:
        via.viability_score = 0.48
        via.paper_eligible = True
        via.live_eligible = False
        via.freshness_ts = datetime.utcnow()
        via.regime_snapshot_json = {"volatility_regime": "normal", "atr_pct": 0.02}
        via.execution_readiness_json = readiness
    db.commit()
    uid = _uid(db, "canf-scalp")
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="CANF",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        execution_family="robinhood_spot",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
            "momentum_live_execution": {
                "tick_scalp_state": {
                    "symbol": "CANF",
                    "phase": "pullback",
                    "high": 6.79,
                    "pullback_low": 6.04,
                    "last_price": 6.04,
                },
                "entry_pending_place_utc": "2026-07-02T13:00:06.022287",
                "entry_pending_place_source": "legacy_pre_submit_pipeline",
                "entry_pre_submit_internal_latency_s": 30.458,
            },
        },
        correlation_id="c-canf-scalp",
    )
    db.commit()

    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="CANF",
            bid=6.08,
            ask=6.09,
            mid=6.085,
            spread_bps=16.4,
            freshness=fresh,
        ),
        fresh,
    )
    ad.place_limit_order_gtc.return_value = {"ok": True, "order_id": "ord-entry-1", "client_order_id": "cid-e1"}

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]

    assert out.get("ok") is True
    assert sess.state == STATE_LIVE_PENDING_ENTRY, events
    assert le["entry_trigger_reason"] == "tick_first_pullback_scalp"
    assert le["entry_score_bypassed"]["reason"] == "ross_tick_profile_hot_path"
    assert le["entry_score_bypassed"]["viability_score"] == pytest.approx(0.48)
    assert le["entry_submitted"] is True
    assert le["entry_order_id"] == "ord-entry-1"
    assert le["entry_pending_place_source"] == "broker_submit_boundary"
    assert le["entry_pending_place_cleared_reason"] == "fresh_pre_submit_pipeline"
    assert "entry_pre_submit_internal_latency_s" not in le
    assert le["tick_scalp_max_hold_seconds"] == 12.0
    assert le["entry_decision_packet_bypassed"]["reason"] == "ross_tick_tape_hot_path"
    event_types = [event_type for event_type, _payload in events]
    assert event_types.index("live_entry_pre_submit_pipeline") < event_types.index("live_entry_pending_place")
    assert event_types.index("live_entry_pending_place") < event_types.index("live_entry_submitted")
    decision_ledger.assert_not_called()
    ad.place_limit_order_gtc.assert_called_once()
    ad.place_market_order.assert_not_called()


def test_tick_scalp_broker_place_timeout_rewatches_when_intent_goes_stale(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_tick_first_pullback_enabled", True)
    monkeypatch.setattr(settings, "chili_autotrader_allow_extended_hours", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", True)
    monkeypatch.setattr(settings, "chili_momentum_decouple_watching_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_atomic_risk_budget_enabled", False)
    monkeypatch.setattr(settings, "chili_feature_parity_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_sticky_backside_bench_enabled", False)

    import app.services.trading.momentum_neural.live_runner as live_runner_mod
    import app.services.trading.momentum_neural.market_profile as market_profile_mod
    from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    monkeypatch.setattr(live_runner_mod, "_venue_broker_connected", lambda _ef: True)
    monkeypatch.setattr(live_runner_mod, "_entry_pricebook_snapshot", lambda _symbol: None)
    monkeypatch.setattr(live_runner_mod, "_l2_entry_confirm", lambda *a, **k: ("confirm", {}))
    monkeypatch.setattr(live_runner_mod, "_entry_pending_place_max_age_seconds", lambda: 0.25)
    monkeypatch.setattr(live_runner_mod, "run_momentum_entry_decision", MagicMock())
    monkeypatch.setattr(market_profile_mod, "market_open_now", lambda *a, **k: True)
    monkeypatch.setattr(market_profile_mod, "schedule_window_now", lambda *a, **k: "hot")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )

    variant = MomentumStrategyVariant(
        family="impulse_breakout",
        variant_key=f"tick_scalp_timeout_test_{datetime.utcnow().timestamp()}",
        version=1,
        label="Tick scalp timeout test",
        params_json={},
        execution_family="robinhood_spot",
    )
    db.add(variant)
    db.flush()
    readiness = {
        "spread_bps": 5.0,
        "slippage_estimate_bps": 6.0,
        "extra": {
            "ross_signals": {
                "CANF": {
                    "ticker": "CANF",
                    "price": 6.79,
                    "daily_change_pct": 128.62,
                    "gap_pct": 119.19,
                    "dollar_volume": 12_000_000,
                    "volume": 1_800_000,
                    "rvol_pace": 23.76,
                    "float_shares": 2_120_000,
                    "scanner_source": "Ross's 5 Pillars Alert (Online)",
                }
            }
        },
    }
    db.add(
        MomentumSymbolViability(
            symbol="CANF",
            variant_id=int(variant.id),
            scope="symbol",
            viability_score=0.48,
            paper_eligible=True,
            live_eligible=False,
            freshness_ts=datetime.utcnow(),
            regime_snapshot_json={"volatility_regime": "normal", "atr_pct": 0.02},
            execution_readiness_json=readiness,
            explain_json={"test": "tick_scalp_timeout"},
            evidence_window_json={},
            source_node_id="test_tick_scalp_timeout",
            correlation_id="test-canf-scalp-timeout",
        )
    )
    db.commit()
    sess = create_trading_automation_session(
        db,
        user_id=_uid(db, "canf-scalp-timeout"),
        symbol="CANF",
        variant_id=int(variant.id),
        mode="live",
        state=STATE_WATCHING_LIVE,
        execution_family="robinhood_spot",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
            "momentum_live_execution": {
                "tick_scalp_state": {
                    "symbol": "CANF",
                    "phase": "pullback",
                    "high": 6.79,
                    "pullback_low": 6.04,
                    "last_price": 6.04,
                },
            },
        },
        correlation_id="c-canf-scalp-timeout",
    )
    db.commit()

    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="CANF",
            bid=6.08,
            ask=6.09,
            mid=6.085,
            spread_bps=16.4,
            freshness=fresh,
        ),
        fresh,
    )
    original_utcnow = live_runner_mod._utcnow

    def _slow_place_timeout(**_kwargs):
        late = original_utcnow() + timedelta(seconds=10)
        monkeypatch.setattr(live_runner_mod, "_utcnow", lambda: late)
        raise RuntimeError("broker place timeout")

    ad.place_limit_order_gtc.side_effect = _slow_place_timeout

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]

    assert out["skipped"] == "broker_place_exception_stale"
    assert sess.state == STATE_WATCHING_LIVE
    assert le["entry_place_exception"]["pending_age_s"] > le["entry_place_exception"]["max_age_s"]
    assert le["entry_tick_scalp_fire_rearmed_reason"] == "broker_place_exception_stale"
    assert "entry_pending_place_utc" not in le
    assert le.get("entry_submitted") is not True
    assert any(event_type == "live_entry_place_exception" for event_type, _payload in events)
    ad.place_limit_order_gtc.assert_called_once()


def test_dev_tick_endpoint_gated(client) -> None:
    r = client.post("/api/trading/momentum/live-runner/tick", json={"session_id": 1})
    assert r.status_code == 404
