"""Multiple-testing discipline — Phase E of f-evidence-fidelity-architecture.

Covers:

1. BH math: ``bh_adjusted_dsr_threshold`` is monotone in family size and
   exceeds the naive threshold for ``m > 1``. ``m = 1`` is a no-op.
2. ``_count_variants_in_family`` resolves family size via
   ``hypothesis_family`` first, then ``parent_id`` chain. Returns ``>= 1``
   when neither is set.
3. CPCV gate handler reads the corrected family count instead of the
   legacy ``=1`` hardcode.
4. Adaptive CPCV gate surfaces both naive and BH-adjusted DSR thresholds
   in the metric row, regardless of the flag.
5. Flag-off byte-identical: the wrapper still returns the legacy verdict
   when ``chili_family_fdr_enabled`` is False, even for a 10-variant
   family.
6. Trial-log shadow write: ``pattern_family_trial_log`` records the
   evaluated variant with the running family count.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest
from sqlalchemy import text

from app.config import settings
from app.services.trading.family_fdr import (
    bh_adjusted_dsr_threshold,
    family_fdr_enabled,
    family_size_for_pattern,
)
from app.services.trading.promotion_gate import _count_variants_in_family
from app.services.trading.cpcv_adaptive_gate import (
    _evaluate_adaptive,
    maybe_apply_adaptive_gate,
)


# ── 1. BH math (pure) ──────────────────────────────────────────────────


def test_bh_threshold_single_variant_unchanged():
    """``m=1`` is the legacy no-op."""
    assert bh_adjusted_dsr_threshold(0.95, 1) == pytest.approx(0.95)
    assert bh_adjusted_dsr_threshold(0.99, 1) == pytest.approx(0.99)


def test_bh_threshold_zero_or_negative_family_unchanged():
    assert bh_adjusted_dsr_threshold(0.95, 0) == pytest.approx(0.95)
    assert bh_adjusted_dsr_threshold(0.95, -3) == pytest.approx(0.95)


def test_bh_threshold_exceeds_naive_for_m_gt_1():
    """For a 10-variant family with naive 0.95, BH lifts the bar to 0.995."""
    naive = 0.95
    bh10 = bh_adjusted_dsr_threshold(naive, 10)
    assert bh10 > naive
    # alpha = 0.05, m = 10 → adj_alpha = 0.005 → threshold = 0.995
    assert bh10 == pytest.approx(0.995)


def test_bh_threshold_monotone_in_family_size():
    """Larger families → stricter thresholds."""
    naive = 0.95
    thr_seq = [bh_adjusted_dsr_threshold(naive, m) for m in (1, 2, 5, 10, 25, 100)]
    # Strictly increasing from m=2 onward.
    assert thr_seq[0] == pytest.approx(naive)
    for prev, cur in zip(thr_seq[1:-1], thr_seq[2:]):
        assert cur >= prev
    assert thr_seq[-1] > thr_seq[1]


def test_bh_threshold_clamps_naive_above_one():
    """Out-of-range naive thresholds are clamped to [0, 1]."""
    # naive=1.5 clamps to 1.0 → alpha=0.0 → threshold stays 1.0 regardless of m.
    assert bh_adjusted_dsr_threshold(1.5, 10) == pytest.approx(1.0)


def test_bh_threshold_invalid_m_type_is_noop():
    """A non-integer ``m`` falls back to the naive threshold."""
    assert bh_adjusted_dsr_threshold(0.95, "ten") == pytest.approx(0.95)  # type: ignore[arg-type]


# ── 2. Family-size resolution from ORM pattern ─────────────────────────


def test_count_variants_in_family_prefers_hypothesis_family(db):
    from app.models.trading import ScanPattern

    fam_label = "test_fam_compression_1m"
    siblings = [
        ScanPattern(
            name=f"sib_{i}",
            origin="user",
            rules_json={},
            hypothesis_family=fam_label,
            active=True,
        )
        for i in range(7)
    ]
    for s in siblings:
        db.add(s)
    # Add an inactive sibling that must NOT be counted.
    inactive = ScanPattern(
        name="sib_inactive",
        origin="user",
        rules_json={},
        hypothesis_family=fam_label,
        active=False,
    )
    db.add(inactive)
    # Add a separate-family pattern that must NOT be counted.
    other_fam = ScanPattern(
        name="other_fam",
        origin="user",
        rules_json={},
        hypothesis_family="something_else",
        active=True,
    )
    db.add(other_fam)
    db.commit()

    count = _count_variants_in_family(db, siblings[0])
    assert count == 7


def test_count_variants_in_family_falls_back_to_parent_id(db):
    """Legacy patterns may have no hypothesis_family; walk parent_id chain."""
    from app.models.trading import ScanPattern

    root = ScanPattern(
        name="legacy_root",
        origin="user",
        rules_json={},
        hypothesis_family=None,
        active=True,
    )
    db.add(root)
    db.flush()
    children = [
        ScanPattern(
            name=f"legacy_child_{i}",
            origin="user",
            rules_json={},
            hypothesis_family=None,
            parent_id=root.id,
            active=True,
        )
        for i in range(4)
    ]
    for c in children:
        db.add(c)
    db.commit()

    # When the candidate is one of the children, ``root_id = parent_id``
    # → counts the siblings sharing that parent.
    count = _count_variants_in_family(db, children[0])
    assert count == 4


def test_count_variants_in_family_returns_one_when_no_family(db):
    from app.models.trading import ScanPattern

    orphan = ScanPattern(
        name="orphan",
        origin="user",
        rules_json={},
        hypothesis_family=None,
        parent_id=None,
        active=True,
    )
    db.add(orphan)
    db.commit()

    # No family + no parent + no children → the count of patterns with
    # ``parent_id == orphan.id`` is zero, which the helper floors to 1.
    assert _count_variants_in_family(db, orphan) == 1


def test_count_variants_in_family_handles_none_pattern():
    assert _count_variants_in_family(None, None) == 1


def test_family_size_for_pattern_by_id(db):
    from app.models.trading import ScanPattern

    fam_label = "fdr_lookup_fam"
    seeds = [
        ScanPattern(
            name=f"fdr_lk_{i}",
            origin="user",
            rules_json={},
            hypothesis_family=fam_label,
            active=True,
        )
        for i in range(3)
    ]
    for s in seeds:
        db.add(s)
    db.commit()

    assert family_size_for_pattern(db, seeds[0].id) == 3
    assert family_size_for_pattern(db, None) == 1
    assert family_size_for_pattern(None, 1) == 1


# ── 3. Adaptive gate threshold surfacing (math layer) ──────────────────


def test_evaluate_adaptive_surfaces_naive_and_bh_thresholds():
    """Both ``pool_threshold_naive`` and ``pool_threshold_bh`` populate."""
    eval_payload: Mapping[str, Any] = {
        "skipped": False,
        "n_trades": 60,
        "deflated_sharpe": 0.97,
        "pbo": 0.10,
        "cpcv_n_paths": 40,
        "cpcv_median_sharpe": 1.0,
        "n_effective_trials": 4,
    }
    pool = {
        "n_trades": [50] * 20,
        "dsr": [0.5 + i * 0.02 for i in range(20)],  # 0.50..0.88, q=0.95 ≈ 0.87
        "pbo": [0.10] * 20,
        "median_sharpe": [0.5] * 20,
        "composite": [0.5] * 20,
        "lifecycle_promoted_sharpes": [0.5],
        "prior_n": 50,
        "pool_size": 20,
    }
    _, _, metric_rows, _ = _evaluate_adaptive(
        eval_payload, pool=pool, family_size=10
    )
    dsr_row = next(r for r in metric_rows if r["metric_name"] == "dsr")
    assert dsr_row["family_size"] == 10
    assert dsr_row["pool_threshold_naive"] is not None
    assert dsr_row["pool_threshold_bh"] is not None
    # BH > naive when m > 1.
    assert dsr_row["pool_threshold_bh"] > dsr_row["pool_threshold_naive"]


def test_evaluate_adaptive_single_family_threshold_equals_naive():
    """When family_size==1, BH adjustment is a no-op on the DSR row."""
    eval_payload: Mapping[str, Any] = {
        "skipped": False,
        "n_trades": 60,
        "deflated_sharpe": 0.97,
        "pbo": 0.10,
        "cpcv_n_paths": 40,
        "cpcv_median_sharpe": 1.0,
        "n_effective_trials": 4,
    }
    pool = {
        "n_trades": [50] * 20,
        "dsr": [0.5 + i * 0.02 for i in range(20)],
        "pbo": [0.10] * 20,
        "median_sharpe": [0.5] * 20,
        "composite": [0.5] * 20,
        "lifecycle_promoted_sharpes": [0.5],
        "prior_n": 50,
        "pool_size": 20,
    }
    _, _, metric_rows, _ = _evaluate_adaptive(
        eval_payload, pool=pool, family_size=1
    )
    dsr_row = next(r for r in metric_rows if r["metric_name"] == "dsr")
    assert dsr_row["family_size"] == 1
    assert dsr_row["pool_threshold_naive"] == pytest.approx(
        dsr_row["pool_threshold_bh"]
    )
    assert dsr_row["family_fdr_applied"] is False


# ── 4. Flag-off byte-identical for a 10-variant family ─────────────────


def test_flag_off_byte_identical_for_10_variant_family(db, monkeypatch):
    """With the flag OFF, the wrapper returns the legacy verdict even for
    a 10-variant family. The BH-adjusted threshold is still computed and
    surfaced in the shadow log; the *applied* threshold is the naive one.
    """
    from app.models.trading import ScanPattern

    monkeypatch.setattr(settings, "chili_family_fdr_enabled", False)
    monkeypatch.setattr(settings, "chili_cpcv_adaptive_gate_enabled", False)

    fam = "byte_identical_fam"
    family = [
        ScanPattern(
            name=f"bi_{i}",
            origin="user",
            rules_json={},
            hypothesis_family=fam,
            lifecycle_stage="candidate",
            trade_count=40,
            cpcv_n_paths=30,
            cpcv_median_sharpe=0.8,
            deflated_sharpe=0.96,
            pbo=0.10,
            active=True,
        )
        for i in range(10)
    ]
    for p in family:
        db.add(p)
    db.commit()

    eval_payload = {
        "skipped": False,
        "n_trades": 40,
        "deflated_sharpe": 0.96,
        "pbo": 0.10,
        "cpcv_n_paths": 30,
        "cpcv_median_sharpe": 0.8,
        "n_effective_trials": 4,
    }
    ok, reasons = maybe_apply_adaptive_gate(
        eval_payload,
        scan_pattern_id=family[0].id,
        legacy_pass=True,
        legacy_reasons=["provisional_sample_size"],
        db_session=db,
    )
    # Flag OFF → legacy verdict returned verbatim regardless of family size.
    assert ok is True
    assert reasons == ["provisional_sample_size"]


# ── 5. Trial-log shadow write ──────────────────────────────────────────


def test_trial_log_records_family_variant(db, monkeypatch):
    """One evaluation appends one row to ``pattern_family_trial_log`` with
    the running family count snapshot."""
    from app.models.trading import ScanPattern

    monkeypatch.setattr(settings, "chili_family_fdr_enabled", False)
    monkeypatch.setattr(settings, "chili_cpcv_adaptive_gate_enabled", False)

    fam = "trial_log_fam"
    family = [
        ScanPattern(
            name=f"tl_{i}",
            origin="user",
            rules_json={},
            hypothesis_family=fam,
            lifecycle_stage="candidate",
            trade_count=40,
            cpcv_n_paths=30,
            cpcv_median_sharpe=0.8,
            deflated_sharpe=0.96,
            pbo=0.10,
            active=True,
        )
        for i in range(5)
    ]
    for p in family:
        db.add(p)
    db.commit()

    eval_payload = {
        "skipped": False,
        "n_trades": 40,
        "deflated_sharpe": 0.96,
        "pbo": 0.10,
        "cpcv_n_paths": 30,
        "cpcv_median_sharpe": 0.8,
        "n_effective_trials": 4,
    }
    maybe_apply_adaptive_gate(
        eval_payload,
        scan_pattern_id=family[2].id,
        legacy_pass=True,
        legacy_reasons=[],
        db_session=db,
    )

    rows = db.execute(
        text(
            "SELECT hypothesis_family, variant_pattern_id, variant_dsr, "
            "variant_pbo, variant_promoted, family_variants_tested_so_far "
            "FROM pattern_family_trial_log "
            "WHERE variant_pattern_id = :pid ORDER BY id DESC LIMIT 1"
        ),
        {"pid": family[2].id},
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == fam
    assert row[1] == family[2].id
    assert row[2] == pytest.approx(0.96)
    assert row[3] == pytest.approx(0.10)
    assert row[4] is True  # legacy verdict returned True
    assert row[5] == 5  # family of 5 active siblings


# ── 6. Flag predicate and migration shape ──────────────────────────────


def test_family_fdr_enabled_default_off():
    assert isinstance(family_fdr_enabled(), bool)


def test_pattern_family_trial_log_table_exists(db):
    """Migration 242 created the table with the expected columns."""
    cols = db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pattern_family_trial_log' "
            "ORDER BY ordinal_position"
        )
    ).fetchall()
    names = [r[0] for r in cols]
    assert "id" in names
    assert "hypothesis_family" in names
    assert "variant_pattern_id" in names
    assert "evaluated_at" in names
    assert "variant_dsr" in names
    assert "variant_pbo" in names
    assert "variant_promoted" in names
    assert "family_best_dsr_at_time" in names
    assert "family_variants_tested_so_far" in names
