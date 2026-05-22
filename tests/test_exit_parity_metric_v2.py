"""Tests for the f-exit-parity-metric-v2 (Migration 230) decomposition.

Two layers:

1. Helper-level tests (1-9) for ``compute_parity_v2_fields`` -- the
   pure helper that both ``live_exit_engine`` and ``backtest_service``
   call to derive the four new ExitParityLog columns.

2. Verdict-gate logic tests (10-14) -- a Python re-implementation of
   the SQL gate query in ``scripts/dispatch-exit-parity-cutover-gate.ps1``,
   exercised against synthetic data so we can pin the threshold
   precedence (INSUFFICIENT_DATA -> FAIL_BIAS_SIGNIFICANT ->
   FAIL_TRACKING_ERROR_HIGH -> FAIL_ASYMMETRIC_AGGRESSIVE -> PASS).

All tests are sub-second -- no DB, no broker.
"""
from __future__ import annotations

import math
from typing import Iterable

import pytest

from app.services.trading.exit_parity_metric import (
    ParityV2Fields,
    compute_parity_v2_fields,
    should_persist_parity_row,
)


# ---------------------------------------------------------------------------
# 1. action_class = both_hold when both engines hold
# ---------------------------------------------------------------------------

def test_action_class_both_hold():
    v2 = compute_parity_v2_fields(
        legacy_action="hold",
        canonical_action="hold",
        legacy_exit_price=None,
        canonical_exit_price=None,
        canonical_reason_code=None,
    )
    assert v2.action_class == "both_hold"
    assert v2.label_match is None
    assert v2.exit_price_drift_bps is None
    assert v2.priority_winner is None


# ---------------------------------------------------------------------------
# 2. action_class = both_close + label_match=True when same action fired
# ---------------------------------------------------------------------------

def test_action_class_both_close_labels_match():
    v2 = compute_parity_v2_fields(
        legacy_action="stop_loss",
        canonical_action="stop_loss",
        legacy_exit_price=99.0,
        canonical_exit_price=99.0,
        canonical_reason_code="stop_loss",
    )
    assert v2.action_class == "both_close"
    assert v2.label_match is True
    # Same exit price -> 0bps drift
    assert v2.exit_price_drift_bps == pytest.approx(0.0, abs=1e-9)
    # Labels match -> priority_winner stays None
    assert v2.priority_winner is None


# ---------------------------------------------------------------------------
# 3. action_class = both_close + label_match=False when actions disagree
# ---------------------------------------------------------------------------

def test_action_class_both_close_labels_disagree():
    v2 = compute_parity_v2_fields(
        legacy_action="trail",
        canonical_action="bos",
        legacy_exit_price=100.0,
        canonical_exit_price=100.5,
        canonical_reason_code="bos",
    )
    assert v2.action_class == "both_close"
    assert v2.label_match is False
    # canonical exited 0.5/100 = 0.005 = 50 bps higher (long -> better)
    assert v2.exit_price_drift_bps == pytest.approx(50.0, abs=1e-6)
    # priority_winner = canonical's reason_code on label-mismatch
    assert v2.priority_winner == "bos"


# ---------------------------------------------------------------------------
# 4. action_class = canonical_only_close
# ---------------------------------------------------------------------------

def test_action_class_canonical_only_close():
    v2 = compute_parity_v2_fields(
        legacy_action="hold",
        canonical_action="time_decay",
        legacy_exit_price=None,
        canonical_exit_price=101.5,
        canonical_reason_code="time_decay",
    )
    assert v2.action_class == "canonical_only_close"
    # label_match is meaningless here -> NULL
    assert v2.label_match is None
    # drift only meaningful when both close
    assert v2.exit_price_drift_bps is None
    # priority_winner = canonical's reason
    assert v2.priority_winner == "time_decay"


# ---------------------------------------------------------------------------
# 5. action_class = legacy_only_close
# ---------------------------------------------------------------------------

def test_action_class_legacy_only_close():
    v2 = compute_parity_v2_fields(
        legacy_action="stop_loss",
        canonical_action="hold",
        legacy_exit_price=98.0,
        canonical_exit_price=None,
        canonical_reason_code=None,
    )
    assert v2.action_class == "legacy_only_close"
    assert v2.label_match is None
    assert v2.exit_price_drift_bps is None
    # priority_winner = legacy's action
    assert v2.priority_winner == "stop_loss"


# ---------------------------------------------------------------------------
# 6. exit_price_drift_bps sign convention for LONG (canonical higher = positive)
# ---------------------------------------------------------------------------

def test_exit_price_drift_bps_sign_long():
    # Long position: canonical closes at $101, legacy at $100. Canonical
    # produced a higher exit -> better realized P/L for a LONG -> positive bps.
    v2 = compute_parity_v2_fields(
        legacy_action="trail",
        canonical_action="trail",
        legacy_exit_price=100.0,
        canonical_exit_price=101.0,
        canonical_reason_code="trail",
        direction="long",
    )
    assert v2.exit_price_drift_bps == pytest.approx(100.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 7. exit_price_drift_bps sign convention for SHORT (canonical lower = positive)
# ---------------------------------------------------------------------------

def test_exit_price_drift_bps_sign_short():
    # Short position: canonical closes at $99, legacy at $100. Canonical
    # produced a lower exit -> better realized P/L for a SHORT -> still
    # positive bps (the helper applies the direction-aware sign flip).
    v2 = compute_parity_v2_fields(
        legacy_action="trail",
        canonical_action="trail",
        legacy_exit_price=100.0,
        canonical_exit_price=99.0,
        canonical_reason_code="trail",
        direction="short",
    )
    assert v2.exit_price_drift_bps == pytest.approx(100.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 8. exit_price_drift_bps NULL when one or both prices are NULL
# ---------------------------------------------------------------------------

def test_exit_price_drift_bps_null_on_missing_price():
    # Both close, both labels match, but legacy exit price is None
    v2_a = compute_parity_v2_fields(
        legacy_action="trail",
        canonical_action="trail",
        legacy_exit_price=None,
        canonical_exit_price=101.0,
        canonical_reason_code="trail",
    )
    assert v2_a.action_class == "both_close"
    assert v2_a.exit_price_drift_bps is None

    # Symmetric: canonical price None
    v2_b = compute_parity_v2_fields(
        legacy_action="trail",
        canonical_action="trail",
        legacy_exit_price=100.0,
        canonical_exit_price=None,
        canonical_reason_code="trail",
    )
    assert v2_b.exit_price_drift_bps is None

    # Zero legacy price (would div-by-zero) -> None
    v2_c = compute_parity_v2_fields(
        legacy_action="trail",
        canonical_action="trail",
        legacy_exit_price=0.0,
        canonical_exit_price=101.0,
        canonical_reason_code="trail",
    )
    assert v2_c.exit_price_drift_bps is None


# ---------------------------------------------------------------------------
# 9. priority_winner population across all four action_class branches
# ---------------------------------------------------------------------------

def test_priority_winner_across_action_class_branches():
    # both_hold -> None
    assert compute_parity_v2_fields(
        legacy_action="hold", canonical_action="hold",
        legacy_exit_price=None, canonical_exit_price=None,
        canonical_reason_code=None,
    ).priority_winner is None

    # both_close + matching labels -> None
    assert compute_parity_v2_fields(
        legacy_action="bos", canonical_action="bos",
        legacy_exit_price=100.0, canonical_exit_price=100.0,
        canonical_reason_code="bos",
    ).priority_winner is None

    # both_close + mismatched labels -> canonical's reason_code
    assert compute_parity_v2_fields(
        legacy_action="trail", canonical_action="bos",
        legacy_exit_price=100.0, canonical_exit_price=100.5,
        canonical_reason_code="bos",
    ).priority_winner == "bos"

    # canonical_only_close -> canonical's reason_code
    assert compute_parity_v2_fields(
        legacy_action="hold", canonical_action="time_decay",
        legacy_exit_price=None, canonical_exit_price=101.0,
        canonical_reason_code="time_decay",
    ).priority_winner == "time_decay"

    # legacy_only_close -> legacy's action
    assert compute_parity_v2_fields(
        legacy_action="stop_loss", canonical_action="hold",
        legacy_exit_price=98.0, canonical_exit_price=None,
        canonical_reason_code=None,
    ).priority_winner == "stop_loss"


# ---------------------------------------------------------------------------
# Verdict-gate Python mirror (mirrors the SQL in
# scripts/dispatch-exit-parity-cutover-gate.ps1).
# ---------------------------------------------------------------------------

# Threshold constants (well-known quant defaults; same as the SQL gate).
T_STAT_CRITICAL = 1.96  # 95% CI z-score, two-sided
TE_MAX_BPS = 10.0       # 10 basis points
ASYM_LOW = 0.4
ASYM_HIGH = 0.6
MIN_SAMPLE_N = 1000


def _verdict(
    *,
    drifts_both_close_bps: Iterable[float],
    canonical_only_n: int,
    legacy_only_n: int,
) -> str:
    """Python mirror of the SQL gate. Returns one of:
    INSUFFICIENT_DATA / FAIL_BIAS_SIGNIFICANT / FAIL_TRACKING_ERROR_HIGH
    / FAIL_ASYMMETRIC_AGGRESSIVE / PASS.
    """
    drifts = list(drifts_both_close_bps)
    n = len(drifts)
    if n < MIN_SAMPLE_N:
        return "INSUFFICIENT_DATA"
    bias = sum(drifts) / n
    var = sum((d - bias) ** 2 for d in drifts) / max(1, n - 1)
    te = math.sqrt(var)
    se = te / math.sqrt(n) if te > 0 else float("inf")
    t_stat = bias / se if se > 0 else 0.0

    if abs(t_stat) > T_STAT_CRITICAL:
        return "FAIL_BIAS_SIGNIFICANT"
    if te > TE_MAX_BPS:
        return "FAIL_TRACKING_ERROR_HIGH"
    asym_total = canonical_only_n + legacy_only_n
    if asym_total > 0:
        share = canonical_only_n / asym_total
        if share < ASYM_LOW or share > ASYM_HIGH:
            return "FAIL_ASYMMETRIC_AGGRESSIVE"
    return "PASS"


def _balanced_drifts(*, n: int, mean_bps: float, te_bps: float) -> list[float]:
    """Synthetic dataset: alternating +/- so the empirical mean is exactly
    `mean_bps` and the empirical stddev is approximately `te_bps`."""
    half = n // 2
    return (
        [mean_bps + te_bps] * half
        + [mean_bps - te_bps] * (n - half)
    )


# ---------------------------------------------------------------------------
# 10. Verdict gate: PASS when t_stat ≈ 0, te ≤ 10 bps, share = 0.5, n ≥ 1000
# ---------------------------------------------------------------------------

def test_verdict_gate_pass():
    drifts = _balanced_drifts(n=1500, mean_bps=0.0, te_bps=8.0)
    v = _verdict(
        drifts_both_close_bps=drifts,
        canonical_only_n=50,
        legacy_only_n=50,
    )
    assert v == "PASS"


# ---------------------------------------------------------------------------
# 11. Verdict gate: FAIL_BIAS_SIGNIFICANT when |t_stat| > 1.96
# ---------------------------------------------------------------------------

def test_verdict_gate_fail_bias_significant():
    # Tiny TE + meaningful mean -> huge t_stat
    drifts = _balanced_drifts(n=1500, mean_bps=2.5, te_bps=8.0)
    v = _verdict(
        drifts_both_close_bps=drifts,
        canonical_only_n=50,
        legacy_only_n=50,
    )
    assert v == "FAIL_BIAS_SIGNIFICANT"


# ---------------------------------------------------------------------------
# 12. Verdict gate: FAIL_TRACKING_ERROR_HIGH when te > 10 bps
# ---------------------------------------------------------------------------

def test_verdict_gate_fail_tracking_error_high():
    # Mean exactly zero (so bias path doesn't fire) but TE 15 bps
    drifts = _balanced_drifts(n=1500, mean_bps=0.0, te_bps=15.0)
    v = _verdict(
        drifts_both_close_bps=drifts,
        canonical_only_n=50,
        legacy_only_n=50,
    )
    assert v == "FAIL_TRACKING_ERROR_HIGH"


# ---------------------------------------------------------------------------
# 13. Verdict gate: FAIL_ASYMMETRIC_AGGRESSIVE when share outside [0.4, 0.6]
# ---------------------------------------------------------------------------

def test_verdict_gate_fail_asymmetric_aggressive():
    drifts = _balanced_drifts(n=1500, mean_bps=0.0, te_bps=8.0)
    # 70% canonical-only, 30% legacy-only -> share=0.7 -> outside band
    v = _verdict(
        drifts_both_close_bps=drifts,
        canonical_only_n=70,
        legacy_only_n=30,
    )
    assert v == "FAIL_ASYMMETRIC_AGGRESSIVE"


# ---------------------------------------------------------------------------
# 14. Verdict gate: INSUFFICIENT_DATA when both_close_n < 1000
# ---------------------------------------------------------------------------

def test_verdict_gate_insufficient_data():
    drifts = _balanced_drifts(n=500, mean_bps=0.0, te_bps=8.0)
    v = _verdict(
        drifts_both_close_bps=drifts,
        canonical_only_n=50,
        legacy_only_n=50,
    )
    assert v == "INSUFFICIENT_DATA"


def test_parity_sampling_keeps_interesting_rows():
    assert should_persist_parity_row(
        sample_pct=0.0,
        action_class="canonical_only_close",
        agree_bool=False,
        legacy_action="hold",
        canonical_action="stop_loss",
        source="backtest",
        ticker="KEEP",
    ) is True

    assert should_persist_parity_row(
        sample_pct=0.0,
        action_class="both_close",
        agree_bool=True,
        legacy_action="stop_loss",
        canonical_action="stop_loss",
        source="live",
        ticker="KEEP",
    ) is True


def test_parity_sampling_can_drop_boring_agreed_holds():
    assert should_persist_parity_row(
        sample_pct=0.0,
        action_class="both_hold",
        agree_bool=True,
        legacy_action="hold",
        canonical_action="hold",
        source="backtest",
        ticker="DROP",
        bar_idx=1,
        config_hash="cfg",
    ) is False

    assert should_persist_parity_row(
        sample_pct=1.0,
        action_class="both_hold",
        agree_bool=True,
        legacy_action="hold",
        canonical_action="hold",
        source="backtest",
        ticker="DROP",
        bar_idx=1,
        config_hash="cfg",
    ) is True
