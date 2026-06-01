"""f-composite-quality-reweight-realized-evidence D5 tests.

Anti-correlation regression test (synthetic 10-pattern dataset where
the NEW formula produces Spearman > 0 and the OLD formula produces
Spearman < 0) is the load-bearing test of this brief. The remaining
tests cover the new component shapes (realized_pnl_score,
realized_evidence_score), the cohort-eligibility floor, mig 244
idempotency, and the re-normalization behavior when n_realized < 5.
"""
from __future__ import annotations

import math
import os
from types import SimpleNamespace
from typing import Optional

import pytest
from scipy.stats import spearmanr
from sqlalchemy import text

from app.services.trading.pattern_quality_score import (
    compute_quality_composite_score,
    _composite_weight_sum,
    _load_realized_pnl_map,
    _resolve_weights,
    realized_pnl_score,
    realized_evidence_score,
)


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _SequenceSession:
    def __init__(self, results):
        self.results = [_Rows(rows) for rows in results]
        self.sqls: list[str] = []
        self.params: list[dict] = []

    def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        self.params.append(dict(params or {}))
        if self.results:
            return self.results.pop(0)
        return _Rows([])


# ---------------------------------------------------------------------------
# realized_pnl_score: shape tests
# ---------------------------------------------------------------------------


def test_realized_pnl_score_full_credit_at_normalizer_positive():
    """avg_pnl_pct == +w_norm should map to 1.0."""
    assert realized_pnl_score(0.01, 0.01) == pytest.approx(1.0)


def test_realized_pnl_score_full_debit_at_normalizer_negative():
    """avg_pnl_pct == -w_norm should map to 0.0."""
    assert realized_pnl_score(-0.01, 0.01) == pytest.approx(0.0)


def test_realized_pnl_score_neutral_at_zero():
    """avg_pnl_pct == 0 should map to 0.5."""
    assert realized_pnl_score(0.0, 0.01) == pytest.approx(0.5)


def test_realized_pnl_score_saturates_above_normalizer():
    """Values above +w_norm clip to 1.0 (no extra credit for outsized
    PnL — a 2%/trade pattern doesn't dominate the score)."""
    assert realized_pnl_score(0.05, 0.01) == pytest.approx(1.0)


def test_realized_pnl_score_saturates_below_normalizer():
    """Symmetric: values below -w_norm clip to 0.0."""
    assert realized_pnl_score(-0.05, 0.01) == pytest.approx(0.0)


def test_realized_pnl_score_null_propagates():
    """avg_pnl_pct None -> None (no magic-default fallback)."""
    assert realized_pnl_score(None, 0.01) is None


def test_realized_pnl_score_zero_normalizer_propagates_null():
    """w_norm <= 0 returns None — degenerate input, no fallback."""
    assert realized_pnl_score(0.005, 0.0) is None
    assert realized_pnl_score(0.005, -0.01) is None


# ---------------------------------------------------------------------------
# realized_evidence_score: saturation tests
# ---------------------------------------------------------------------------


def test_realized_evidence_score_at_n_equals_tau():
    """At n == tau, the score is 1 - 1/e ≈ 0.632."""
    expected = 1 - math.exp(-1.0)
    assert realized_evidence_score(30, 30.0) == pytest.approx(expected, abs=1e-6)


def test_realized_evidence_score_low_n():
    """At n=1, the score should be small (~3.3%)."""
    s = realized_evidence_score(1, 30.0)
    assert 0.02 < s < 0.04


def test_realized_evidence_score_high_n_saturates():
    """At n=85 (pattern 585's actual sample), the score should be
    high (~94%) but not exactly 1.0."""
    s = realized_evidence_score(85, 30.0)
    assert 0.93 < s < 0.96


def test_realized_evidence_score_zero_n():
    """n=0 -> 0 (no evidence at all)."""
    assert realized_evidence_score(0, 30.0) == pytest.approx(0.0)


def test_realized_evidence_score_null_n():
    """None propagates to 0 (caller should have filtered, but be safe)."""
    # Implementation choice: realized_evidence_score expects int, not None.
    # If called with None, the formula 1 - exp(-None/tau) raises TypeError.
    # That is correct behavior; callers must propagate None upstream.
    with pytest.raises(TypeError):
        realized_evidence_score(None, 30.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_quality_composite_score: anti-correlation regression
# ---------------------------------------------------------------------------


class _FakePattern:
    """Minimal duck-type for ScanPattern with the fields the scorer reads."""
    def __init__(self, cpcv, dsr, pbo):
        self.cpcv_median_sharpe = cpcv
        self.deflated_sharpe = dsr
        self.pbo = pbo


_OLD_WEIGHTS = {
    "cpcv_sharpe": 0.30,
    "deflated_sharpe": 0.20,
    "pbo_inverse": 0.15,
    "directional_wr": 0.25,
    "decay_inverse": 0.10,
    "realized": 0.0,  # absent in old formula
}

_NEW_WEIGHTS = {
    "cpcv_sharpe": 0.10,
    "deflated_sharpe": 0.05,
    "pbo_inverse": 0.05,
    "directional_wr": 0.35,
    "decay_inverse": 0.10,
    "realized": 0.35,
}


def test_composite_weight_sum_ignores_non_weight_settings():
    settings = SimpleNamespace(
        chili_cohort_score_weight_cpcv_sharpe=0.10,
        chili_cohort_score_weight_deflated_sharpe=0.05,
        chili_cohort_score_weight_pbo_inverse=0.05,
        chili_cohort_score_weight_directional_wr=0.35,
        chili_cohort_score_weight_decay_inverse=0.10,
        chili_cohort_score_weight_realized=0.35,
        chili_cohort_score_realized_pnl_normalizer_pct=0.01,
        chili_cohort_score_realized_evidence_tau=30.0,
        chili_cohort_score_realized_window_days=90,
    )

    weights = _resolve_weights(settings)

    assert sum(weights.values()) == pytest.approx(121.01)
    assert _composite_weight_sum(weights) == pytest.approx(1.0)


def test_load_realized_pnl_map_counts_only_computable_return_samples() -> None:
    db = _SequenceSession([
        [(101, 3, 0.01, 30.0)],
        [(101, 2, 0.02, 20.0)],
    ])

    realized = _load_realized_pnl_map(
        db,
        45,
        include_autotrader_paper_dynamic=True,
    )

    assert realized[101]["n"] == 5
    assert realized[101]["live_n"] == 3
    assert realized[101]["paper_dynamic_n"] == 2
    assert realized[101]["avg_pnl_pct"] == pytest.approx(0.014)
    assert realized[101]["total_pnl"] == pytest.approx(50.0)
    assert db.params == [{"window_days": 45}, {"window_days": 45}]

    live_sql = " ".join(db.sqls[0].split())
    assert "WITH realized_samples AS" in live_sql
    assert "FROM trading_trades t" in live_sql
    assert "COUNT(realized_return_frac) AS n" in live_sql
    assert "AVG(realized_return_frac) AS avg_pnl_pct" in live_sql
    assert "WHERE realized_return_frac IS NOT NULL" in live_sql
    assert "t.filled_quantity" in live_sql
    assert "t.partial_taken_qty" in live_sql
    assert "COUNT(*)" not in live_sql

    paper_sql = " ".join(db.sqls[1].split())
    assert "WITH realized_samples AS" in paper_sql
    assert "FROM trading_paper_trades pt" in paper_sql
    assert "COUNT(realized_return_frac) AS n" in paper_sql
    assert "AVG(realized_return_frac) AS avg_pnl_pct" in paper_sql
    assert "WHERE realized_return_frac IS NOT NULL" in paper_sql
    assert "pt.partial_taken_qty" in paper_sql
    assert "COALESCE(pt.signal_json" in paper_sql
    assert "COUNT(*)" not in paper_sql


def _build_anti_corr_dataset():
    """10 synthetic patterns. The 5 with HIGH CPCV have NEGATIVE
    realized PnL; the 5 with LOW CPCV have POSITIVE realized PnL.

    This is the diagnostic pattern Cowork measured in production:
    backtest stats (CPCV/DSR) inversely correlate with realized outcomes
    because backtest selection-bias favours overfit patterns.

    Under the OLD formula (CPCV-dominant), high-CPCV patterns get high
    composite scores, producing Spearman(score, realized_pnl) < 0.
    Under the NEW formula (realized-dominant), this should flip.
    """
    patterns = []
    for i in range(5):
        # High CPCV cohort: cpcv 4.0-5.0, but realized avg_pnl_pct = -0.005
        # (consistently losing). DSR/PBO/WR/decay constant across cohort.
        p = _FakePattern(cpcv=4.0 + i * 0.25, dsr=1.0, pbo=0.0)
        patterns.append({
            "pat": p,
            "directional_wr": 0.90,
            "decay": 0.0,
            "realized_pnl_score": realized_pnl_score(-0.005, 0.01),
            "realized_n_trades": 30,
            "realized_total_pnl": -50.0 - i * 10.0,  # increasingly worse
        })
    for i in range(5):
        # Low CPCV cohort: cpcv 1.0-1.5, but realized avg_pnl_pct = +0.005
        # (consistently winning).
        p = _FakePattern(cpcv=1.0 + i * 0.125, dsr=1.0, pbo=0.0)
        patterns.append({
            "pat": p,
            "directional_wr": 0.55,
            "decay": 0.0,
            "realized_pnl_score": realized_pnl_score(0.005, 0.01),
            "realized_n_trades": 30,
            "realized_total_pnl": 50.0 + i * 10.0,
        })
    return patterns


def test_anti_correlation_old_formula_produces_negative_spearman():
    """Old formula (CPCV-dominant) ranks losers above winners."""
    ds = _build_anti_corr_dataset()
    scores_old = [
        compute_quality_composite_score(
            d["pat"], d["directional_wr"], d["decay"], _OLD_WEIGHTS,
            realized_pnl_score=None, realized_n_trades=0,
        )
        for d in ds
    ]
    pnls = [d["realized_total_pnl"] for d in ds]
    rho, _ = spearmanr(scores_old, pnls)
    assert rho < -0.5, f"old formula should anti-correlate; got rho={rho:.3f}"


def test_anti_correlation_new_formula_produces_positive_spearman():
    """New formula (realized-anchored) ranks winners above losers."""
    ds = _build_anti_corr_dataset()
    scores_new = [
        compute_quality_composite_score(
            d["pat"], d["directional_wr"], d["decay"], _NEW_WEIGHTS,
            realized_pnl_score=d["realized_pnl_score"],
            realized_n_trades=d["realized_n_trades"],
        )
        for d in ds
    ]
    pnls = [d["realized_total_pnl"] for d in ds]
    rho, _ = spearmanr(scores_new, pnls)
    assert rho > 0.5, f"new formula should correlate positively; got rho={rho:.3f}"


def test_anti_correlation_flips_sign():
    """Pair the previous two: sign-flip on the same dataset."""
    ds = _build_anti_corr_dataset()
    scores_old = [
        compute_quality_composite_score(
            d["pat"], d["directional_wr"], d["decay"], _OLD_WEIGHTS,
            realized_pnl_score=None, realized_n_trades=0,
        )
        for d in ds
    ]
    scores_new = [
        compute_quality_composite_score(
            d["pat"], d["directional_wr"], d["decay"], _NEW_WEIGHTS,
            realized_pnl_score=d["realized_pnl_score"],
            realized_n_trades=d["realized_n_trades"],
        )
        for d in ds
    ]
    pnls = [d["realized_total_pnl"] for d in ds]
    rho_old, _ = spearmanr(scores_old, pnls)
    rho_new, _ = spearmanr(scores_new, pnls)
    assert rho_old < 0 < rho_new, (
        f"expected sign flip; old rho={rho_old:.3f}, new rho={rho_new:.3f}"
    )


# ---------------------------------------------------------------------------
# Re-normalization when realized data absent
# ---------------------------------------------------------------------------


def test_renormalization_when_realized_absent():
    """When n_realized < 5, realized component contributes 0 and the
    other 5 weights re-normalize. Total still in [0, 1].

    Note: the production trade-off Cowork observed 2026-05-16 — this
    re-normalization can inflate patterns with no realized history
    above proven winners. Captured here as the EXPECTED behavior per
    the brief; a follow-up fix may revisit.
    """
    pat = _FakePattern(cpcv=2.0, dsr=1.0, pbo=0.0)
    score = compute_quality_composite_score(
        pat, directional_wr=1.0, decay=0.0, weights=_NEW_WEIGHTS,
        realized_pnl_score=None, realized_n_trades=0,
    )
    # cpcv 2.0 -> clipped 1.0 -> 0.10 contrib
    # dsr 1.0 -> clipped 1.0 -> 0.05 contrib
    # pbo 0.0 -> pbo_inv 1.0 -> 0.05 contrib
    # wr 1.0 -> 0.35 contrib
    # decay 0.0 -> decay_inv 1.0 -> 0.10 contrib
    # Sum = 0.65. Re-normalized: 0.65 / (1 - 0.35) = 0.65 / 0.65 = 1.0
    assert score == pytest.approx(1.0, abs=1e-6)


def test_realized_component_full_credit_path():
    """With realized data present, the realized component is included
    directly (no re-normalization). At full saturation (n=85,
    realized_pnl_score=1.0), the realized contribution is
    0.35 * 1.0 * (1 - exp(-85/30)) = ~0.330."""
    pat = _FakePattern(cpcv=2.0, dsr=1.0, pbo=0.0)
    score = compute_quality_composite_score(
        pat, directional_wr=1.0, decay=0.0, weights=_NEW_WEIGHTS,
        realized_pnl_score=1.0, realized_n_trades=85,
    )
    # Sum of non-realized contributions = 0.65 (as above).
    # Realized contrib = 0.35 * 1.0 * (1 - exp(-85/30)) ≈ 0.3295
    # Total ≈ 0.9795
    assert 0.97 < score < 0.99


def test_realized_component_zero_when_n_below_floor():
    """When n_realized < 5, realized component contributes 0
    regardless of realized_pnl_score."""
    pat = _FakePattern(cpcv=2.0, dsr=1.0, pbo=0.0)
    score_with_realized = compute_quality_composite_score(
        pat, directional_wr=1.0, decay=0.0, weights=_NEW_WEIGHTS,
        realized_pnl_score=1.0, realized_n_trades=3,  # below floor of 5
    )
    score_without_realized = compute_quality_composite_score(
        pat, directional_wr=1.0, decay=0.0, weights=_NEW_WEIGHTS,
        realized_pnl_score=None, realized_n_trades=0,
    )
    # Both go through the re-normalization path; result should be identical.
    assert score_with_realized == pytest.approx(score_without_realized, abs=1e-6)


# ---------------------------------------------------------------------------
# Cohort eligibility floor (integration test against test DB)
# ---------------------------------------------------------------------------


def _test_db_or_skip():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url or not url.endswith("_test"):
        pytest.skip("TEST_DATABASE_URL not set or doesn't end in _test")


def _seed_trade_user(db, user_id: int = 1) -> None:
    db.execute(
        text(
            """
            INSERT INTO users (id, name)
            VALUES (:user_id, :name)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"user_id": user_id, "name": f"test-user-{user_id}"},
    )


def test_cohort_eligibility_floor_blocks_negative_realized(db):
    """A pattern with >= 5 closed trades and negative avg_pnl_pct must
    NOT appear in select_cohort_candidates."""
    _test_db_or_skip()
    from app.services.trading.pattern_cohort_promote import select_cohort_candidates

    _seed_trade_user(db)
    # Seed a candidate pattern (gate-passed, CPCV-strong) with 6 losing trades.
    db.execute(text("""
        INSERT INTO scan_patterns (id, name, active, lifecycle_stage,
            promotion_gate_passed, cpcv_n_paths, cpcv_median_sharpe, deflated_sharpe, pbo)
        VALUES (9991, 'TEST_FLOOR_LOSER', TRUE, 'candidate', TRUE, 8, 2.0, 1.0, 0.0)
        ON CONFLICT (id) DO NOTHING
    """))
    for i in range(6):
        db.execute(text("""
            INSERT INTO trading_trades (user_id, scan_pattern_id, ticker,
                direction, entry_price, exit_price, quantity, pnl, status,
                entry_date, exit_date)
            VALUES (1, 9991, 'TST', 'long', 100.0, 99.0, 10.0, -10.0, 'closed',
                NOW() - INTERVAL '10 days', NOW() - INTERVAL '5 days')
        """))
    db.flush()

    cands = select_cohort_candidates(db)
    assert all(c.id != 9991 for c in cands), (
        "pattern with 6 losing trades must be blocked by the realized floor"
    )


def test_cohort_eligibility_floor_allows_few_trades(db):
    """A pattern with n_realized < 5 should NOT be blocked by the floor
    (sample too small to demote on)."""
    _test_db_or_skip()
    from app.services.trading.pattern_cohort_promote import select_cohort_candidates

    _seed_trade_user(db)
    db.execute(text("""
        INSERT INTO scan_patterns (id, name, active, lifecycle_stage,
            promotion_gate_passed, cpcv_n_paths, cpcv_median_sharpe, deflated_sharpe, pbo)
        VALUES (9992, 'TEST_FLOOR_THIN', TRUE, 'candidate', TRUE, 8, 2.0, 1.0, 0.0)
        ON CONFLICT (id) DO NOTHING
    """))
    for i in range(3):  # only 3 trades — below floor of 5
        db.execute(text("""
            INSERT INTO trading_trades (user_id, scan_pattern_id, ticker,
                direction, entry_price, exit_price, quantity, pnl, status,
                entry_date, exit_date)
            VALUES (1, 9992, 'TST', 'long', 100.0, 99.0, 10.0, -10.0, 'closed',
                NOW() - INTERVAL '10 days', NOW() - INTERVAL '5 days')
        """))
    db.flush()

    cands = select_cohort_candidates(db)
    assert any(c.id == 9992 for c in cands), (
        "pattern with only 3 trades should pass through the floor"
    )


def test_cohort_eligibility_floor_allows_positive_avg(db):
    """A pattern with >= 5 trades but POSITIVE avg_pnl_pct passes the floor."""
    _test_db_or_skip()
    from app.services.trading.pattern_cohort_promote import select_cohort_candidates

    _seed_trade_user(db)
    db.execute(text("""
        INSERT INTO scan_patterns (id, name, active, lifecycle_stage,
            promotion_gate_passed, cpcv_n_paths, cpcv_median_sharpe, deflated_sharpe, pbo)
        VALUES (9993, 'TEST_FLOOR_WINNER', TRUE, 'candidate', TRUE, 8, 2.0, 1.0, 0.0)
        ON CONFLICT (id) DO NOTHING
    """))
    for i in range(6):
        db.execute(text("""
            INSERT INTO trading_trades (user_id, scan_pattern_id, ticker,
                direction, entry_price, exit_price, quantity, pnl, status,
                entry_date, exit_date)
            VALUES (1, 9993, 'TST', 'long', 100.0, 101.0, 10.0, +10.0, 'closed',
                NOW() - INTERVAL '10 days', NOW() - INTERVAL '5 days')
        """))
    db.flush()

    cands = select_cohort_candidates(db)
    assert any(c.id == 9993 for c in cands), (
        "pattern with 6 winning trades should pass through the floor"
    )


def test_quality_realized_map_option_uses_contract_multiplier(db):
    _test_db_or_skip()
    _seed_trade_user(db)
    db.execute(text("""
        INSERT INTO scan_patterns (id, name, active, lifecycle_stage,
            promotion_gate_passed, cpcv_n_paths, cpcv_median_sharpe,
            deflated_sharpe, pbo)
        VALUES (9998, 'TEST_OPTION_REALIZED_MAP', TRUE, 'candidate',
            TRUE, 8, 2.0, 1.0, 0.0)
        ON CONFLICT (id) DO NOTHING
    """))
    db.execute(text("""
        INSERT INTO trading_trades (user_id, scan_pattern_id, ticker,
            direction, entry_price, exit_price, quantity, pnl, status,
            entry_date, exit_date, asset_kind)
        VALUES (1, 9998, 'SPY', 'long', 5.0, 6.0, 1.0, 100.0, 'closed',
            NOW() - INTERVAL '10 days', NOW() - INTERVAL '5 days', 'option')
    """))
    db.flush()

    realized = _load_realized_pnl_map(db, 90)

    assert realized[9998]["avg_pnl_pct"] == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# Migration 244 idempotency
# ---------------------------------------------------------------------------


def test_mig244_idempotent_on_second_run(db):
    """Running mig 244 twice should be a no-op the second time."""
    _test_db_or_skip()
    from app.migrations import _migration_244_composite_reweight_demote_losers

    _seed_trade_user(db)
    # Seed a candidate: pilot_promoted with 5 losing closed trades.
    db.execute(text("""
        INSERT INTO scan_patterns (id, name, active, lifecycle_stage,
            promotion_gate_passed, cpcv_n_paths, cpcv_median_sharpe, deflated_sharpe, pbo)
        VALUES (9994, 'TEST_MIG244', TRUE, 'pilot_promoted', TRUE, 8, 2.0, 1.0, 0.0)
        ON CONFLICT (id) DO NOTHING
    """))
    for i in range(5):
        db.execute(text("""
            INSERT INTO trading_trades (user_id, scan_pattern_id, ticker,
                direction, entry_price, exit_price, quantity, pnl, status,
                entry_date, exit_date)
            VALUES (1, 9994, 'TST', 'long', 100.0, 99.0, 10.0, -10.0, 'closed',
                NOW() - INTERVAL '10 days', NOW() - INTERVAL '5 days')
        """))
    db.flush()
    db.commit()

    conn = db.connection()
    _migration_244_composite_reweight_demote_losers(conn)

    stage_after_first = db.execute(
        text("SELECT lifecycle_stage FROM scan_patterns WHERE id=9994")
    ).scalar()
    assert stage_after_first == "challenged"

    # Second run should be a no-op (no rows matching the criteria).
    _migration_244_composite_reweight_demote_losers(conn)

    stage_after_second = db.execute(
        text("SELECT lifecycle_stage FROM scan_patterns WHERE id=9994")
    ).scalar()
    assert stage_after_second == "challenged"
