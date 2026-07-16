"""Pure, fail-closed first-dip certificate regression tests.

The detector only identifies a keyed opportunity.  It neither reserves nor
consumes that opportunity; durable once-per-ET-day semantics belong at the
atomic final pre-submit / broker-fill boundary.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import inspect
from types import SimpleNamespace
from typing import Callable
import uuid

import pandas as pd
import pandas.testing as pdt
import pytest

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import paper_runner as prun
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.first_dip_tape_decision import (
    FirstDipTapeDecisionRequest,
    _installed_captured_db_paper_first_dip_tape_decision_authority,
    _installed_captured_first_dip_detector_retention_provider,
    _installed_exact_bound_test_first_dip_tape_decision_authority,
    _installed_sealed_replay_first_dip_tape_decision_authority,
    _make_exact_bound_test_first_dip_tape_decision_authority,
)
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapeEvaluation,
    FirstDipTapePolicy,
)
from tests.first_dip_test_support import captured_first_dip_detector_authority


def _plsm_shape(*, start: str = "2026-07-13 12:00:00+00:00") -> pd.DataFrame:
    """Base -> vertical ignition -> orderly pullback -> tail flush -> curl."""
    rows = [
        (5.50, 5.60, 5.45, 5.55, 20_000),
        (5.55, 5.65, 5.50, 5.60, 22_000),
        (5.60, 5.70, 5.55, 5.65, 21_000),
        (5.65, 5.75, 5.60, 5.70, 23_000),
        (5.70, 5.80, 5.65, 5.75, 22_000),
        (5.75, 5.85, 5.70, 5.80, 24_000),
        (5.80, 8.90, 5.80, 8.80, 900_000),
        (8.85, 12.80, 8.70, 12.40, 1_400_000),
        (12.30, 12.50, 10.00, 10.10, 500_000),
        (10.10, 10.30, 9.40, 9.60, 400_000),
        (9.60, 9.70, 9.00, 9.10, 350_000),
        (9.10, 9.20, 8.70, 8.80, 330_000),
        (8.80, 8.90, 8.50, 8.60, 320_000),
        (8.60, 8.70, 8.40, 8.50, 310_000),
        (8.50, 8.60, 8.30, 8.45, 300_000),
        (8.45, 8.50, 7.70, 8.35, 600_000),
        (8.35, 8.55, 8.20, 8.50, 300_000),
    ]
    idx = pd.date_range(start=start, periods=len(rows), freq="1min")
    opn, high, low, close, volume = zip(*rows)
    return pd.DataFrame(
        {"Open": opn, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


def _settings(*, first_dip: bool, policy_mode: str = "baseline") -> SimpleNamespace:
    return SimpleNamespace(
        chili_momentum_flush_dip_buy_enabled=True,
        chili_momentum_first_dip_reclaim_enabled=first_dip,
        chili_momentum_first_dip_reclaim_policy_mode=policy_mode,
        chili_momentum_first_dip_min_leg_atr_multiple=3.0,
        chili_momentum_first_dip_max_retrace_fraction=0.618,
        chili_momentum_dip_buy_rth_only_enabled=False,
        chili_momentum_reclaim_max_hours_after_open=24.0,
        chili_momentum_pattern_tape_gate_enabled=True,
        chili_momentum_l2_confirm_window_s=5.0,
        chili_momentum_first_dip_tape_max_source_age_seconds=1.0,
        chili_momentum_l2_confirm_tick_rate_floor_pctile=0.25,
        chili_momentum_first_dip_tape_minimum_prints=3,
    )


def _evaluation(
    request: FirstDipTapeDecisionRequest,
    *,
    status: str = "valid_positive",
    reason: str | None = None,
    symbol: str | None = None,
    decision_at=None,
    policy_sha256: str | None = None,
    newest_source_age_seconds: float | None = 0.25,
) -> FirstDipTapeEvaluation:
    positive = status == "valid_positive"
    features = (
        {
            "signed_tape_accel": 12.0 if positive else -12.0,
            "tick_rate": 8.0,
            "tick_rate_floor": 4.0,
            "n_ticks": 3,
        }
        if status in {"valid_positive", "valid_negative"}
        else None
    )
    if reason is None:
        reason = {
            "valid_positive": "first_dip_tape_confirmed",
            "valid_negative": "first_dip_tape_not_confirmed",
            "coverage_unavailable": "first_dip_tape_source_stale",
            "invalid": "first_dip_tape_source_from_future",
        }[status]
    return FirstDipTapeEvaluation(
        symbol=symbol or request.symbol,
        decision_at=decision_at or request.decision_at,
        read_id="read:first-dip:plsm:121629.5",
        result_sha256="a" * 64,
        source_event_sha256s=("b" * 64, "c" * 64, "d" * 64),
        policy_sha256=policy_sha256 or request.policy.policy_sha256,
        status=status,
        reason=reason,
        confirmed=positive,
        features=features,
        newest_source_age_seconds=newest_source_age_seconds,
    )


def _positive_provider(
    request: FirstDipTapeDecisionRequest,
) -> FirstDipTapeEvaluation:
    return _evaluation(request)


def _call(
    monkeypatch,
    df: pd.DataFrame,
    *,
    first_dip: bool = False,
    policy_mode: str = "candidate",
    db=object(),
    now="2026-07-13 12:16:30+00:00",
    l2_as_of="2026-07-13 12:16:29.500000+00:00",
    provider: Callable[
        [FirstDipTapeDecisionRequest], FirstDipTapeEvaluation
    ]
    | None = _positive_provider,
    sealed_runtime: bool = False,
    tmp_path=None,
    first_dip_execution_surface: str | None = None,
):
    settings_obj = _settings(first_dip=first_dip, policy_mode=policy_mode)
    monkeypatch.setattr(eg, "settings", settings_obj)
    calls: list[FirstDipTapeDecisionRequest] = []

    def forbidden_mutable_tape_fallback(*args, **kwargs):
        raise AssertionError("first-dip detector must not read mutable DB tape")

    monkeypatch.setattr(eg, "tape_confirms_hold", forbidden_mutable_tape_fallback)

    request = FirstDipTapeDecisionRequest(
        symbol="PLSM",
        decision_at=pd.Timestamp(l2_as_of).to_pydatetime(),
        policy=FirstDipTapePolicy.from_settings(settings_obj),
    )
    adapter = None
    if sealed_runtime:
        assert provider is _positive_provider
        assert tmp_path is not None
        from tests import test_replay_v3_sealed_input_adapter as sealed_fixtures

        decision_at = request.decision_at
        monkeypatch.setattr(sealed_fixtures, "SYMBOL", request.symbol)
        monkeypatch.setattr(
            sealed_fixtures,
            "BASE",
            decision_at - pd.Timedelta(seconds=5.2).to_pytimedelta(),
        )
        work = tmp_path / f"pure-gate-{uuid.uuid4().hex}"
        work.mkdir()
        fixture = sealed_fixtures._sealed_fixture(
            work,
            first_dip_tape=True,
            no_order=True,
            first_dip_predecision_control_delay_seconds=0.001,
            decision_publication_delay_seconds=0.002,
            first_dip_policy=request.policy,
            decision_offset_seconds=5.2,
        )
        monkeypatch.setattr(
            rv3,
            "grade_replay_coverage",
            sealed_fixtures._complete_grade,
        )
        adapter = rv3.SealedReplayV3InputAdapter(
            fixture.capture,
            fixture.manifest,
            fixture.request,
        )
        checkpoint = fixture.manifest.decision_checkpoints[0]
        adapter.advance_to_frontier(
            checkpoint.decision_at,
            sequence_at_most=checkpoint.input_prefix_sequence,
        )
        decision_tick = adapter.decision_tick_for_frontier(
            checkpoint.decision_at,
            checkpoint.input_prefix_sequence,
        )
        assert decision_tick is not None
        adapter.begin_decision_read_plan(decision_tick)
        authority = adapter.prepare_first_dip_tape_decision_authority()
        scope = _installed_sealed_replay_first_dip_tape_decision_authority(
            authority
        )
    else:
        authority = (
            _make_exact_bound_test_first_dip_tape_decision_authority(
                request=request,
                evaluation=provider(request),
            )
            if provider is not None
            else None
        )
        scope = (
            _installed_exact_bound_test_first_dip_tape_decision_authority(
                authority
            )
            if authority is not None
            else nullcontext()
        )
    with scope:
        resolved_execution_surface = (
            "sealed_replay"
            if sealed_runtime and first_dip_execution_surface is None
            else first_dip_execution_surface
        )
        result = eg.flush_dip_buy_confirmation(
            df,
            entry_interval="1m",
            live_price=8.70,
            symbol="PLSM",
            now=now,
            db=db,
            l2_as_of=l2_as_of,
            first_dip_execution_surface=resolved_execution_surface,
        )
    if adapter is not None:
        adapter.ohlcv_provider("PLSM", interval="1m", period="1d")
        adapter.account_equity_provider(prefer_equity=True)
        adapter.complete_decision_read_plan()
    if provider is not None and "first_dip_tape" in result[2]:
        calls.append(request)
    return result, calls, settings_obj


def test_plsm_first_dip_fires_only_with_exact_typed_tape_evaluation(
    monkeypatch,
    tmp_path,
):
    db = object()
    as_of = pd.Timestamp("2026-07-13 12:16:29.500000+00:00")
    (ok, reason, debug), calls, settings_obj = _call(
        monkeypatch,
        _plsm_shape(),
        db=db,
        l2_as_of=as_of,
        sealed_runtime=True,
        tmp_path=tmp_path,
    )

    assert (ok, reason) == (True, "flush_dip_buy")
    assert debug["front_side_via"] == "first_dip_day_leg"
    assert debug["pullback_low"] == 7.70
    assert debug["first_dip_tape_confirmed"] is True
    certificate = debug["first_dip_certificate"]
    assert certificate["reason"] == "first_dip_day_leg_certified"
    assert certificate["eligible"] is True
    assert certificate["day_leg_pct"] >= certificate["required_day_leg_pct"]
    assert certificate["retrace_fraction"] <= certificate["max_retrace_fraction"]
    assert debug["opportunity_key"] == {
        "symbol": "PLSM",
        "trading_date": "2026-07-13",
        "setup_family": "first_dip_reclaim",
    }
    assert len(calls) == 1
    request = calls[0]
    assert request.symbol == "PLSM"
    assert request.decision_at == as_of.to_pydatetime()
    assert request.policy.policy_sha256 == debug["first_dip_tape_policy_sha256"]
    assert request.authority_scope == "detector_diagnostic_only"
    assert request.reservation_authority is False
    assert request.order_authority is False
    tape = debug["first_dip_tape"]
    assert tape["read_id"] == debug["first_dip_tape_read_id"]
    assert tape["policy_sha256"] == debug["first_dip_tape_policy_sha256"]
    assert tape["evaluation_sha256"] == debug["first_dip_tape_evaluation_sha256"]
    assert tape["status"] == "valid_positive"
    assert tape["reason"] == "first_dip_tape_confirmed"
    assert tape["features"]["signed_tape_accel"] > 0.0
    assert tape["run_bound"] is True
    assert tape["reservation_authority"] is False
    assert tape["order_authority"] is False
    receipt = debug["first_dip_tape_decision_receipt"]
    assert receipt["authority_source"] == "sealed_replay"
    assert receipt["run_bound"] is True
    assert receipt["run_id"]
    assert receipt["generation"] >= 1
    assert receipt["reservation_authority"] is False
    assert receipt["order_authority"] is False


def test_missing_provider_is_explicitly_unavailable_and_never_falls_back(monkeypatch):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        db=None,
        provider=None,
    )

    assert ok is False
    assert reason == "flush_dip_first_dip_tape_unavailable"
    assert debug["first_dip_tape_reject_reason"] == (
        "first_dip_tape_decision_provider_missing"
    )
    assert debug["first_dip_tape"]["status"] == "coverage_unavailable"
    assert debug["first_dip_tape"]["reservation_authority"] is False
    assert debug["first_dip_tape"]["order_authority"] is False
    assert "first_dip_tape_confirmed" not in debug
    assert calls == []


def test_exact_bound_test_receipt_cannot_confirm_the_production_gate(monkeypatch):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
    )

    assert (ok, reason) == (False, "flush_dip_first_dip_tape_invalid")
    assert len(calls) == 1
    assert debug["first_dip_tape"]["status"] == "valid_positive"
    assert debug["first_dip_tape"]["run_bound"] is False
    assert debug["first_dip_tape"]["decision_receipt"]["authority_source"] == (
        "exact_bound_test"
    )
    assert debug["first_dip_tape_reject_reason"] == (
        "first_dip_tape_decision_receipt_unbound"
    )
    assert "first_dip_tape_confirmed" not in debug


def test_sealed_replay_receipt_cannot_cross_into_db_paper_surface(
    monkeypatch,
    tmp_path,
):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        sealed_runtime=True,
        tmp_path=tmp_path,
        first_dip_execution_surface="captured_db_paper",
    )

    assert (ok, reason) == (False, "flush_dip_first_dip_tape_invalid")
    assert debug["first_dip_tape_reject_reason"] == (
        "first_dip_tape_execution_surface_mismatch"
    )
    assert debug["first_dip_expected_execution_surface"] == (
        "captured_db_paper"
    )
    assert "first_dip_tape_confirmed" not in debug
    assert len(calls) == 1


def test_run_bound_receipt_without_expected_surface_fails_closed(
    monkeypatch,
    tmp_path,
):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        sealed_runtime=True,
        tmp_path=tmp_path,
        first_dip_execution_surface="",
    )

    assert (ok, reason) == (False, "flush_dip_first_dip_tape_invalid")
    assert debug["first_dip_tape_reject_reason"] == (
        "first_dip_tape_execution_surface_missing"
    )
    assert debug["first_dip_expected_execution_surface"] is None
    assert "first_dip_tape_confirmed" not in debug
    assert len(calls) == 1


def test_generic_live_runner_surface_cannot_accept_replay_receipt(
    monkeypatch,
    tmp_path,
):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        sealed_runtime=True,
        tmp_path=tmp_path,
        first_dip_execution_surface="unsupported_live_runner",
    )

    assert (ok, reason) == (False, "flush_dip_first_dip_tape_invalid")
    assert debug["first_dip_expected_execution_surface"] == (
        "unsupported_live_runner"
    )
    assert debug["first_dip_tape_reject_reason"] == (
        "first_dip_tape_execution_surface_mismatch"
    )
    assert "first_dip_tape_confirmed" not in debug
    assert len(calls) == 1


def test_negative_tape_is_explicitly_not_confirmed(monkeypatch):
    def negative(request):
        return _evaluation(request, status="valid_negative")

    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        provider=negative,
    )

    assert ok is False
    assert reason == "flush_dip_first_dip_tape_not_confirmed"
    assert debug["first_dip_tape"]["status"] == "valid_negative"
    assert debug["first_dip_tape"]["features"]["signed_tape_accel"] == -12.0
    assert debug["first_dip_tape_reject_reason"] == "first_dip_tape_not_confirmed"
    assert "first_dip_tape_confirmed" not in debug
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("provider", "expected_status", "expected_reason", "expected_gate_reason"),
    [
        (
            lambda request: _evaluation(
                request,
                status="coverage_unavailable",
                newest_source_age_seconds=(
                    request.policy.max_source_age_seconds + 0.001
                ),
            ),
            "coverage_unavailable",
            "first_dip_tape_source_stale",
            "flush_dip_first_dip_tape_unavailable",
        ),
    ],
)
def test_stale_mismatched_or_untyped_evidence_never_sets_confirmation(
    monkeypatch,
    provider,
    expected_status,
    expected_reason,
    expected_gate_reason,
):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        provider=provider,
    )

    assert (ok, reason) == (False, expected_gate_reason)
    assert debug["first_dip_tape"]["status"] == expected_status
    assert debug["first_dip_tape"]["reason"] == expected_reason
    assert debug["first_dip_tape_reject_reason"] == expected_reason
    assert "first_dip_tape_confirmed" not in debug
    assert len(calls) == 1


def test_negative_decision_does_not_consume_a_later_valid_opportunity(
    monkeypatch,
    tmp_path,
):
    negative, negative_calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        provider=lambda request: _evaluation(request, status="valid_negative"),
    )
    positive, positive_calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        provider=_positive_provider,
        sealed_runtime=True,
        tmp_path=tmp_path,
    )

    assert negative[:2] == (False, "flush_dip_first_dip_tape_not_confirmed")
    assert positive[:2] == (True, "flush_dip_buy")
    assert len(negative_calls) == len(positive_calls) == 1
    assert "first_dip_tape_confirmed" not in negative[2]
    assert positive[2]["first_dip_tape_confirmed"] is True


def test_nonvertical_leg_rejects_before_tape(monkeypatch):
    df = _plsm_shape()
    real_compute = eg.compute_all_from_df

    def large_atr(frame, *, needed):
        arrays = real_compute(frame, needed=needed)
        arrays["atr"][-1] = 5.0
        return arrays

    monkeypatch.setattr(eg, "compute_all_from_df", large_atr)
    (ok, reason, debug), calls, _ = _call(monkeypatch, df)

    assert (ok, reason) == (False, "flush_dip_not_front_side")
    assert debug["first_dip_certificate"]["reason"] == "first_dip_day_leg_not_vertical"
    assert calls == []


def test_broken_leg_rejects_before_tape(monkeypatch):
    df = _plsm_shape()
    flush_idx = len(df) - 2
    df.iloc[flush_idx, df.columns.get_loc("Low")] = 5.80
    df.iloc[flush_idx, df.columns.get_loc("Close")] = 6.00

    (ok, reason, debug), calls, _ = _call(monkeypatch, df)

    assert (ok, reason) == (False, "flush_dip_not_front_side")
    assert debug["first_dip_certificate"]["reason"] == "first_dip_day_leg_broken"
    assert debug["first_dip_certificate"]["retrace_fraction"] > 0.618
    assert calls == []


def test_flag_off_retains_exact_legacy_decline_and_never_reads_tape(monkeypatch):
    (result, calls, _) = _call(
        monkeypatch,
        _plsm_shape(),
        first_dip=False,
        policy_mode="baseline",
    )

    assert result == (
        False,
        "flush_dip_not_front_side",
        {"entry_interval": "1m", "pattern": "flush_dip"},
    )
    assert calls == []


def test_explicit_candidate_mode_exercises_same_detector_without_legacy_override(
    monkeypatch,
    tmp_path,
):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        first_dip=False,
        policy_mode="candidate",
        sealed_runtime=True,
        tmp_path=tmp_path,
    )

    assert (ok, reason) == (True, "flush_dip_buy")
    assert debug["first_dip_policy"] == {
        "configured_mode": "candidate",
        "effective_mode": "candidate",
        "legacy_input_true_ignored": False,
        "legacy_true_migrated_to_candidate": False,
        "independent_legacy_override": False,
        "promotion_authority_verified": False,
    }
    assert debug["first_dip_tape_confirmed"] is True
    assert len(calls) == 1


def test_legacy_true_is_provenance_only_and_cannot_activate_candidate(
    monkeypatch,
):
    result, calls, settings_obj = _call(
        monkeypatch,
        _plsm_shape(),
        first_dip=True,
        policy_mode="baseline",
        provider=None,
    )

    assert result == (
        False,
        "flush_dip_not_front_side",
        {"entry_interval": "1m", "pattern": "flush_dip"},
    )
    assert eg._first_dip_policy_mode(settings_obj) == (
        "baseline",
        "baseline",
        True,
    )
    assert calls == []


def test_configured_promoted_cannot_self_attest_oos_promotion(
    monkeypatch,
):
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        _plsm_shape(),
        first_dip=False,
        policy_mode="promoted",
        provider=None,
    )

    assert (ok, reason) == (
        False,
        "flush_dip_first_dip_promotion_unavailable",
    )
    assert debug["first_dip_policy"] == {
        "configured_mode": "promoted",
        "effective_mode": "promoted",
        "legacy_input_true_ignored": False,
        "legacy_true_migrated_to_candidate": False,
        "independent_legacy_override": False,
        "promotion_authority_verified": False,
    }
    assert debug["front_side_via"] == "first_dip_day_leg"
    assert debug["first_dip_promotion_receipt_status"] == "unavailable"
    assert calls == []


def test_detector_is_pure_and_has_no_session_state_input(monkeypatch):
    df = _plsm_shape()
    before = df.copy(deep=True)
    db = {"untouched": [1, 2, 3]}
    db_before = {"untouched": [1, 2, 3]}

    first, _, _ = _call(monkeypatch, df, db=db)
    second, _, _ = _call(monkeypatch, df, db=db)

    assert first == second
    pdt.assert_frame_equal(df, before)
    assert db == db_before
    assert "first_dip_state" not in inspect.signature(eg.flush_dip_buy_confirmation).parameters


def test_opportunity_key_uses_et_date_across_utc_boundary(monkeypatch, tmp_path):
    # 00:16 UTC on July 14 is still July 13 in New York.
    df = _plsm_shape(start="2026-07-14 00:00:00+00:00")
    (ok, reason, debug), calls, _ = _call(
        monkeypatch,
        df,
        now="2026-07-14 00:16:30+00:00",
        l2_as_of="2026-07-14 00:16:29.500000+00:00",
        sealed_runtime=True,
        tmp_path=tmp_path,
    )

    assert (ok, reason) == (True, "flush_dip_buy")
    assert debug["first_dip_certificate"]["trading_date_et"] == "2026-07-13"
    assert debug["opportunity_key"]["trading_date"] == "2026-07-13"
    assert len(calls) == 1


def _configure_db_paper_first_dip_call_graph(monkeypatch, frame: pd.DataFrame):
    settings_obj = _settings(first_dip=False, policy_mode="candidate")
    settings_obj.chili_momentum_entry_gates_enabled = True
    settings_obj.chili_momentum_pullback_entry_interval = "1m"
    monkeypatch.setattr(eg, "settings", settings_obj)

    warmup = pd.DataFrame(
        {"Close": [float(value) for value in range(30)]},
        index=pd.date_range(
            "2026-07-10 12:00:00+00:00",
            periods=30,
            freq="15min",
        ),
    )

    def captured_frames(_symbol, *, interval, period):
        assert period == "5d"
        return warmup if interval == "15m" else frame

    generic_calls: list[tuple[object, ...]] = []

    def generic_reject(*args, **kwargs):
        generic_calls.append((args, kwargs))
        return False, "waiting_for_generic_pullback", {}

    monkeypatch.setattr(eg, "fetch_ohlcv_df", captured_frames)
    monkeypatch.setattr(
        eg,
        "regime_entry_allowed",
        lambda *args, **kwargs: (True, "regime_ok"),
    )
    monkeypatch.setattr(
        eg,
        "family_regime_prefilter_allows",
        lambda *args, **kwargs: (True, "family_regime_ok"),
    )
    monkeypatch.setattr(
        eg,
        "evaluate_pattern_conditions_for_variant",
        lambda *args, **kwargs: (True, "pattern_ok"),
    )
    monkeypatch.setattr(eg, "momentum_pullback_trigger", generic_reject)
    return settings_obj, generic_calls


def _captured_db_paper_first_dip_scope(
    *,
    request: FirstDipTapeDecisionRequest,
):
    authority = captured_first_dip_detector_authority(request)

    def retain(resolution, opportunity_key):
        return hashlib.sha256(
            (
                resolution.receipt.binding_sha256
                + repr(sorted(opportunity_key.items()))
                + ":mechanics-only-retention"
            ).encode("utf-8")
        ).hexdigest()

    @contextmanager
    def installed():
        with _installed_captured_first_dip_detector_retention_provider(retain):
            with _installed_captured_db_paper_first_dip_tape_decision_authority(
                authority
            ):
                yield

    return installed()


def _run_db_paper_gate(decision_at):
    return eg.run_paper_entry_gates(
        object(),
        symbol="PLSM",
        variant=None,
        regime_snapshot={},
        family_id="momentum_pullback",
        live_price=8.70,
        decision_at=decision_at,
    )


def test_vertical_leg_without_full_first_dip_shape_does_not_own_generic_tick(
    monkeypatch,
):
    frame = _plsm_shape()
    flush_idx = len(frame) - 2
    frame.iloc[flush_idx, frame.columns.get_loc("Open")] = 8.00
    frame.iloc[flush_idx, frame.columns.get_loc("High")] = 8.50
    frame.iloc[flush_idx, frame.columns.get_loc("Low")] = 7.70
    frame.iloc[flush_idx, frame.columns.get_loc("Close")] = 8.30
    _settings_obj, _old_calls = _configure_db_paper_first_dip_call_graph(
        monkeypatch,
        frame,
    )
    generic_calls: list[bool] = []

    def generic_positive(*_args, **_kwargs):
        generic_calls.append(True)
        return True, "generic_pullback_break", {
            "pullback_low": 8.10,
            "pullback_high": 8.60,
        }

    monkeypatch.setattr(eg, "momentum_pullback_trigger", generic_positive)
    allowed, reason, debug = _run_db_paper_gate(
        pd.Timestamp("2026-07-13 12:16:29.500000+00:00").to_pydatetime()
    )

    assert (allowed, reason) == (True, "all_gates_pass")
    assert debug["trigger"] == "generic_pullback_break"
    assert "first_dip_reject" not in debug
    assert generic_calls == [True]


def test_full_first_dip_missing_receipt_owns_tick_before_generic_positive(
    monkeypatch,
):
    _settings_obj, _old_calls = _configure_db_paper_first_dip_call_graph(
        monkeypatch,
        _plsm_shape(),
    )
    generic_calls: list[bool] = []

    def forbidden_generic(*_args, **_kwargs):
        generic_calls.append(True)
        return True, "must_not_bypass_first_dip", {}

    monkeypatch.setattr(eg, "momentum_pullback_trigger", forbidden_generic)
    allowed, reason, debug = _run_db_paper_gate(
        pd.Timestamp("2026-07-13 12:16:29.500000+00:00").to_pydatetime()
    )

    assert (allowed, reason) == (
        False,
        "flush_dip_first_dip_tape_unavailable",
    )
    assert debug["first_dip_reject"]["front_side_via"] == (
        "first_dip_day_leg"
    )
    assert generic_calls == []


def test_unexpected_post_classification_failure_preserves_first_dip_ownership(
    monkeypatch,
):
    from app.services.trading.momentum_neural import (
        first_dip_tape_decision as tape_decision,
    )

    settings_obj, _old_calls = _configure_db_paper_first_dip_call_graph(
        monkeypatch,
        _plsm_shape(),
    )
    generic_calls: list[bool] = []

    def forbidden_generic(*_args, **_kwargs):
        generic_calls.append(True)
        return True, "must_not_bypass_first_dip", {}

    monkeypatch.setattr(eg, "momentum_pullback_trigger", forbidden_generic)
    monkeypatch.setattr(
        tape_decision,
        "first_dip_tape_decision_debug",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("post-classification debug failure")
        ),
    )
    decision_at = pd.Timestamp(
        "2026-07-13 12:16:29.500000+00:00"
    ).to_pydatetime()
    request = FirstDipTapeDecisionRequest(
        symbol="PLSM",
        decision_at=decision_at,
        policy=FirstDipTapePolicy.from_settings(settings_obj),
    )
    with _captured_db_paper_first_dip_scope(request=request):
        allowed, reason, debug = _run_db_paper_gate(decision_at)

    assert (allowed, reason) == (
        False,
        "flush_dip_first_dip_tape_unavailable",
    )
    rejected = debug["first_dip_reject"]
    assert rejected["front_side_via"] == "first_dip_day_leg"
    assert rejected["first_dip_tape_reject_reason"] == (
        "first_dip_tape_decision_provider_error"
    )
    assert generic_calls == []


def test_db_paper_first_dip_is_classified_before_generic_and_uses_paper_receipt(
    monkeypatch,
    tmp_path,
):
    settings_obj, generic_calls = _configure_db_paper_first_dip_call_graph(
        monkeypatch,
        _plsm_shape(),
    )
    decision_at = pd.Timestamp(
        "2026-07-13 12:16:29.500000+00:00"
    ).to_pydatetime()
    request = FirstDipTapeDecisionRequest(
        symbol="PLSM",
        decision_at=decision_at,
        policy=FirstDipTapePolicy.from_settings(settings_obj),
    )
    scope = _captured_db_paper_first_dip_scope(
        request=request,
    )

    with scope:
        allowed, reason, debug = _run_db_paper_gate(decision_at)

    assert allowed is True
    assert reason == "all_gates_pass"
    assert generic_calls == []
    assert debug["trigger"] == "flush_dip_buy"
    assert debug["first_dip_policy"] == {
        "configured_mode": "candidate",
        "effective_mode": "candidate",
        "legacy_input_true_ignored": False,
        "legacy_true_migrated_to_candidate": False,
        "independent_legacy_override": False,
        "promotion_authority_verified": False,
    }
    assert debug["first_dip_tape_confirmed"] is True
    assert debug["first_dip_tape"]["run_bound"] is True
    assert debug["first_dip_tape"]["decision_at"] == (
        decision_at.isoformat().replace("+00:00", "Z")
    )
    assert debug["first_dip_tape"]["policy_sha256"] == request.policy.policy_sha256
    assert debug["first_dip_tape_policy"] == request.policy.to_dict()
    assert debug["first_dip_tape_policy_sha256"] == request.policy.policy_sha256
    evaluation_fields = {
        "schema_version",
        "symbol",
        "decision_at",
        "read_id",
        "result_sha256",
        "source_event_sha256s",
        "policy_sha256",
        "status",
        "reason",
        "confirmed",
        "features",
        "newest_source_age_seconds",
    }
    assert debug["first_dip_tape_evaluation"] == {
        key: value
        for key, value in debug["first_dip_tape"].items()
        if key in evaluation_fields
    }
    assert debug["first_dip_tape_evaluation_sha256"] == debug[
        "first_dip_tape"
    ]["evaluation_sha256"]
    assert debug["first_dip_tape_read_id"] == debug["first_dip_tape"]["read_id"]
    assert debug["first_dip_tape_run_bound"] is True
    assert debug["first_dip_tape_decision_receipt"] == debug["first_dip_tape"][
        "decision_receipt"
    ]
    assert debug["first_dip_tape_decision_receipt_binding_sha256"] == debug[
        "first_dip_tape"
    ]["decision_receipt_binding_sha256"]
    assert debug["first_dip_tape_decision_receipt"]["authority_source"] == (
        "captured_db_paper"
    )


def test_db_paper_missing_receipt_vetoes_only_that_decision_and_is_reusable(
    monkeypatch,
    tmp_path,
):
    settings_obj, generic_calls = _configure_db_paper_first_dip_call_graph(
        monkeypatch,
        _plsm_shape(),
    )
    decision_at = pd.Timestamp(
        "2026-07-13 12:16:29.500000+00:00"
    ).to_pydatetime()

    allowed, reason, debug = _run_db_paper_gate(decision_at)

    assert allowed is False
    assert reason == "flush_dip_first_dip_tape_unavailable"
    rejected = debug["first_dip_reject"]
    assert rejected["first_dip_tape"]["reason"] == (
        "first_dip_tape_decision_provider_missing"
    )
    assert "first_dip_tape_confirmed" not in rejected

    request = FirstDipTapeDecisionRequest(
        symbol="PLSM",
        decision_at=decision_at,
        policy=FirstDipTapePolicy.from_settings(settings_obj),
    )
    scope = _captured_db_paper_first_dip_scope(
        request=request,
    )
    with scope:
        retry_allowed, retry_reason, retry_debug = _run_db_paper_gate(decision_at)

    assert retry_allowed is True
    assert retry_reason == "all_gates_pass"
    assert retry_debug["first_dip_tape_confirmed"] is True
    assert generic_calls == []


def test_db_paper_baseline_non_first_dip_reject_keeps_primary_reason(monkeypatch):
    settings_obj, generic_calls = _configure_db_paper_first_dip_call_graph(
        monkeypatch,
        _plsm_shape(),
    )
    settings_obj.chili_momentum_first_dip_reclaim_policy_mode = "baseline"

    allowed, reason, debug = _run_db_paper_gate(
        pd.Timestamp("2026-07-13 12:16:29.500000+00:00").to_pydatetime()
    )

    assert allowed is False
    assert reason == "waiting_for_generic_pullback"
    assert debug == {"trigger": True, "interval": "1m"}
    assert len(generic_calls) == 1


def test_db_paper_tick_forwards_executable_mid_and_one_decision_clock():
    source = inspect.getsource(prun.tick_paper_session)
    call_start = source.index("ok_g, reason_g, dbg = run_paper_entry_gates(")
    call_end = source.index("\n            if not ok_g:", call_start)
    call = source[call_start:call_end]

    assert source.index("_entry_gate_decision_at = _utcnow()") < call_start
    assert "live_price=mid" in call
    assert "decision_at=_entry_gate_decision_at" in call


def test_live_first_dip_owned_veto_stops_before_additive_ladder():
    owned = (
        False,
        "first_dip_final_typed_capture_receipt_unavailable",
        {
            "front_side_via": "first_dip_day_leg",
            "opportunity_key": {
                "symbol": "PLSM",
                "trading_date": "2026-07-13",
                "setup_family": "first_dip_reclaim",
            },
        },
    )

    with pytest.raises(lr._FirstDipOwnedDecisionSkipAdditive):
        lr._guard_first_dip_owned_decision_from_additives(owned)
    assert lr._guard_first_dip_owned_decision_from_additives(None) is None

    # Keep a narrow wiring assertion: the behavior-tested guard must precede
    # the entire additive region, while the classified result is restored after
    # that region.  New additive families automatically remain behind the same
    # executable boundary instead of needing another copied condition.
    source = inspect.getsource(lr.tick_live_session)
    guard = source.index("_guard_first_dip_owned_decision_from_additives(")
    start = source.index("# HVM101 (C): two ADDITIVE entry triggers")
    end = source.index(
        "# Restore the classified decision after every additive",
        start,
    )

    assert guard < start < end
    assert "current_first_dip_tape_authority_surface()" in source
    assert "unsupported_live_runner" not in source
