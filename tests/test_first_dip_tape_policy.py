from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapePolicy,
    FirstDipTapePolicyError,
    FirstDipTapeReadQuery,
    FirstDipTapeWindow,
    evaluate_first_dip_tape,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    captured_read_result_sha256,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 14, 13, 0, tzinfo=UTC)


def _window(
    *,
    event_offsets: tuple[float, ...] = (1.0, 3.0, 5.0, 9.0, 12.0, 15.0),
    sizes: tuple[float, ...] = (100.0, 100.0, 50.0, 400.0, 500.0, 600.0),
    prices: tuple[float, ...] = (10.00, 9.99, 10.00, 10.01, 10.02, 10.03),
    symbol: str = "VEEE",
    returned_offset: float = 15.0,
    sides: tuple[str, ...] | None = None,
) -> FirstDipTapeWindow:
    event_ats = tuple(BASE + timedelta(seconds=value) for value in event_offsets)
    resolved_sides = sides or tuple("buy" for _ in event_ats)
    rows = tuple(
        (
            price,
            size,
            price if side == "sell" else price - 0.01,
            price + 0.01 if side == "sell" else price,
            event_at.timestamp(),
        )
        for price, size, event_at, side in zip(
            prices, sizes, event_ats, resolved_sides
        )
    )
    return FirstDipTapeWindow(
        read_id="00000000-0000-0000-0000-000000000001",
        symbol=symbol,
        requested_at=BASE + timedelta(seconds=0.5),
        returned_at=BASE + timedelta(seconds=returned_offset),
        result_sha256="a" * 64,
        source_event_sha256s=tuple(f"{index:064x}" for index in range(1, len(rows) + 1)),
        provider_event_ats=event_ats,
        rows=rows,
    )


def _policy(**changes) -> FirstDipTapePolicy:
    values = {
        "window_seconds": 15.0,
        "max_source_age_seconds": 1.0,
        "tick_rate_floor_pctile": 0.0,
        "minimum_prints": 3,
    }
    values.update(changes)
    return FirstDipTapePolicy(**values)


def test_exact_positive_window_is_deterministic_and_policy_bound() -> None:
    decision_at = BASE + timedelta(seconds=15)
    first = evaluate_first_dip_tape(
        _window(),
        policy=_policy(),
        decision_at=decision_at,
        symbol="VEEE",
    )
    second = evaluate_first_dip_tape(
        _window(),
        policy=_policy(),
        decision_at=decision_at,
        symbol="VEEE",
    )

    assert first.status == "valid_positive"
    assert first.reason == "first_dip_tape_confirmed"
    assert first.confirmed is True
    assert first.features is not None
    assert first.features["signed_tape_accel"] > 0.0
    assert first.evaluation_sha256 == second.evaluation_sha256
    assert first.policy_sha256 == _policy().policy_sha256


def test_exact_nonconfirming_window_is_valid_negative_not_unavailable() -> None:
    window = _window(
        prices=(10.01, 10.02, 10.00, 9.99, 9.98, 9.97),
        sizes=(400.0, 500.0, 300.0, 400.0, 500.0, 600.0),
        sides=("buy", "buy", "sell", "sell", "sell", "sell"),
    )
    result = evaluate_first_dip_tape(
        window,
        policy=_policy(),
        decision_at=BASE + timedelta(seconds=15),
        symbol="VEEE",
    )

    assert result.status == "valid_negative"
    assert result.reason == "first_dip_tape_not_confirmed"
    assert result.confirmed is False


def test_thin_exact_window_rejects_decision_as_valid_negative() -> None:
    window = _window(
        event_offsets=(14.0, 15.0),
        prices=(10.00, 10.01),
        sizes=(100.0, 200.0),
    )
    result = evaluate_first_dip_tape(
        window,
        policy=_policy(),
        decision_at=BASE + timedelta(seconds=15),
        symbol="VEEE",
    )

    assert result.status == "valid_negative"
    assert result.reason == "first_dip_tape_insufficient_prints"
    assert result.confirmed is False


def test_complete_empty_window_is_valid_negative_not_coverage_unavailable() -> None:
    window = FirstDipTapeWindow(
        read_id="00000000-0000-0000-0000-000000000099",
        symbol="VEEE",
        requested_at=BASE + timedelta(seconds=14.5),
        returned_at=BASE + timedelta(seconds=15),
        result_sha256=captured_read_result_sha256(()),
        source_event_sha256s=(),
        provider_event_ats=(),
        rows=(),
    )

    result = evaluate_first_dip_tape(
        window,
        policy=_policy(),
        decision_at=BASE + timedelta(seconds=15),
        symbol="VEEE",
    )

    assert result.status == "valid_negative"
    assert result.reason == "first_dip_tape_no_prints"
    assert result.confirmed is False
    assert result.features is None
    assert result.newest_source_age_seconds is None


def test_stale_window_is_coverage_unavailable() -> None:
    window = _window(
        event_offsets=(1.0, 2.0, 3.0),
        prices=(10.00, 10.01, 10.02),
        sizes=(100.0, 200.0, 300.0),
        returned_offset=3.1,
    )
    result = evaluate_first_dip_tape(
        window,
        policy=_policy(max_source_age_seconds=1.0),
        decision_at=BASE + timedelta(seconds=15),
        symbol="VEEE",
    )

    assert result.status == "coverage_unavailable"
    assert result.reason == "first_dip_tape_source_stale"
    assert result.confirmed is False
    assert result.newest_source_age_seconds == 12.0


def test_symbol_or_future_receipt_mismatch_is_invalid() -> None:
    symbol_mismatch = evaluate_first_dip_tape(
        _window(symbol="WRONG"),
        policy=_policy(),
        decision_at=BASE + timedelta(seconds=15),
        symbol="VEEE",
    )
    future_receipt = evaluate_first_dip_tape(
        _window(returned_offset=16.0),
        policy=_policy(),
        decision_at=BASE + timedelta(seconds=15),
        symbol="VEEE",
    )

    assert symbol_mismatch.status == "invalid"
    assert symbol_mismatch.reason == "first_dip_tape_symbol_mismatch"
    assert future_receipt.status == "invalid"
    assert future_receipt.reason == "first_dip_tape_receipt_from_future"


def test_policy_hash_changes_with_explicit_percentile() -> None:
    assert _policy(tick_rate_floor_pctile=0.0).policy_sha256 != _policy(
        tick_rate_floor_pctile=1.0
    ).policy_sha256


@pytest.mark.parametrize(
    "invalid_frontier",
    (None, "not-an-integer", 1.5, float("inf"), True, 0, -1),
)
def test_typed_query_normalizes_malformed_frontier_to_policy_error(
    invalid_frontier,
) -> None:
    policy = _policy()
    valid = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=BASE,
        event_end_inclusive=BASE + timedelta(seconds=policy.window_seconds),
        decision_at=BASE + timedelta(seconds=policy.window_seconds),
        available_at_most=BASE + timedelta(seconds=policy.window_seconds),
        source_frontier_sequence=1,
        policy_sha256=policy.policy_sha256,
    ).to_dict()
    valid["source_frontier_sequence"] = invalid_frontier

    with pytest.raises(
        FirstDipTapePolicyError,
        match="source frontier must be positive",
    ):
        FirstDipTapeReadQuery.from_dict(valid)
