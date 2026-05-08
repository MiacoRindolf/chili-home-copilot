"""f-pattern-demote-on-thin-evidence (2026-05-08).

Pin the thin-evidence demote handler:

  * `_matches_thin_evidence_criteria` (helper-level): each criterion
    in isolation + the all-criteria match path.
  * `run_thin_evidence_demote` (DB-bound): seeds scan_patterns rows,
    runs the sweep, asserts the right rows get
    `lifecycle_stage='challenged'` + `demoted_at` not NULL +
    `promotion_demote_reason='thin_evidence_low_realized_wr'`. The
    healthy patterns (1011/1016 fingerprint) stay promoted.

Run with ``-p no:asyncio`` (workaround for pre-existing pytest-asyncio
plugin collection failure).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.services.trading.learning import (
    THIN_EVIDENCE_DEMOTE_REASON,
    THIN_EVIDENCE_MIN_TRADES,
    THIN_EVIDENCE_PROVISIONAL_GATE_REASON,
    THIN_EVIDENCE_WIN_RATE_FLOOR,
    _matches_thin_evidence_criteria,
    run_thin_evidence_demote,
)


# ── _matches_thin_evidence_criteria — helper-level matrix ────────────


def _stub_promoted_thin():
    """Stub matching ALL five criteria → predicate True."""
    return SimpleNamespace(
        id=585,
        lifecycle_stage="promoted",
        trade_count=4,
        win_rate=0.25,
        oos_win_rate=None,
        promotion_gate_reasons=["provisional_small_paths"],
        promotion_status="promoted",
    )


def test_all_criteria_matched_returns_true():
    assert _matches_thin_evidence_criteria(_stub_promoted_thin()) is True


def test_lifecycle_not_promoted_excludes():
    p = _stub_promoted_thin()
    p.lifecycle_stage = "challenged"
    assert _matches_thin_evidence_criteria(p) is False


def test_trade_count_at_or_above_min_excludes():
    """trade_count >= 10 (the brief's `< 10` floor)."""
    p = _stub_promoted_thin()
    p.trade_count = THIN_EVIDENCE_MIN_TRADES  # exactly 10
    assert _matches_thin_evidence_criteria(p) is False
    p.trade_count = THIN_EVIDENCE_MIN_TRADES + 1  # 11 — also excluded
    assert _matches_thin_evidence_criteria(p) is False


def test_win_rate_at_or_above_floor_excludes():
    """win_rate >= 0.33 (the brief's `< 0.33` floor)."""
    p = _stub_promoted_thin()
    p.win_rate = THIN_EVIDENCE_WIN_RATE_FLOOR  # exactly 0.33
    assert _matches_thin_evidence_criteria(p) is False
    p.win_rate = 0.50
    assert _matches_thin_evidence_criteria(p) is False


def test_win_rate_none_excludes():
    """No win-rate signal => no demote (sample is too thin to even
    score)."""
    p = _stub_promoted_thin()
    p.win_rate = None
    assert _matches_thin_evidence_criteria(p) is False


def test_oos_win_rate_present_excludes():
    """If the pattern has been OOS-validated, the brief considers the
    evidence sufficient regardless of live thinness."""
    p = _stub_promoted_thin()
    p.oos_win_rate = 0.60
    assert _matches_thin_evidence_criteria(p) is False


def test_provisional_gate_reason_absent_excludes():
    """Pattern was promoted via a stronger gate (or no gate-reasons
    record) — leave it alone."""
    p = _stub_promoted_thin()
    p.promotion_gate_reasons = ["sharpe_above_threshold"]
    assert _matches_thin_evidence_criteria(p) is False
    p.promotion_gate_reasons = None
    assert _matches_thin_evidence_criteria(p) is False
    p.promotion_gate_reasons = []
    assert _matches_thin_evidence_criteria(p) is False


def test_promotion_gate_reasons_str_json_decoded():
    """Some upstream paths surface promotion_gate_reasons as a JSON
    string instead of a list — predicate must still match."""
    p = _stub_promoted_thin()
    p.promotion_gate_reasons = json.dumps(["provisional_small_paths"])
    assert _matches_thin_evidence_criteria(p) is True


def test_pattern_585_audit_fingerprint_matches():
    """Replay of the 2026-05-08 audit's pattern-585 fingerprint.

    The brief lists this exact shape as the canonical case:
    promoted + 4 trades + 25% WR + no OOS + provisional gate.
    """
    p = SimpleNamespace(
        id=585,
        lifecycle_stage="promoted",
        trade_count=4,
        win_rate=0.25,
        oos_win_rate=None,
        promotion_gate_reasons=["provisional_small_paths"],
        promotion_status="promoted",
    )
    assert _matches_thin_evidence_criteria(p) is True


def test_pattern_1011_audit_fingerprint_kept():
    """Pattern 1011 from the audit: 409 trades / 63.2% WR — must
    survive the sweep regardless of OOS / gate reasons (healthy
    sample size)."""
    p = SimpleNamespace(
        id=1011,
        lifecycle_stage="promoted",
        trade_count=409,
        win_rate=0.632,
        oos_win_rate=None,  # NULL but trade_count is far above floor
        promotion_gate_reasons=["provisional_small_paths"],  # even with the flag
        promotion_status="promoted",
    )
    assert _matches_thin_evidence_criteria(p) is False


# ── run_thin_evidence_demote — DB-bound ──────────────────────────────


def _seed_scan_pattern(
    db,
    *,
    pid: int,
    lifecycle_stage: str = "promoted",
    trade_count: int = 4,
    win_rate: float = 0.25,
    oos_win_rate=None,
    promotion_gate_reasons=("provisional_small_paths",),
    promotion_status: str = "promoted",
    name: str = "Test Pattern",
) -> None:
    """Seed via the ORM so all NOT-NULL defaults (rules_json,
    oos_validation_json, paper_book_json, etc.) auto-populate.
    Avoids hand-maintaining the column list in an INSERT statement."""
    from app.models.trading import ScanPattern

    if db.query(ScanPattern).filter(ScanPattern.id == pid).first() is not None:
        return  # idempotent for back-to-back tests

    sp = ScanPattern(
        id=pid,
        name=name,
        lifecycle_stage=lifecycle_stage,
        trade_count=trade_count,
        win_rate=win_rate,
        oos_win_rate=oos_win_rate,
        promotion_gate_reasons=list(promotion_gate_reasons),
        promotion_status=promotion_status,
        active=True,
    )
    db.add(sp)
    db.commit()


def _read_pattern(db, pid: int):
    return db.execute(text("""
        SELECT id, lifecycle_stage, demoted_at, promotion_demote_reason
        FROM scan_patterns WHERE id = :id
    """), {"id": pid}).fetchone()


def test_sweep_demotes_audit_585_fingerprint(db):
    """Pattern 585's audit shape: thin evidence + provisional gate ->
    demoted to challenged with the brief's reason string."""
    _seed_scan_pattern(db, pid=585, name="Intraday Squeeze + Decl Vol")

    res = run_thin_evidence_demote(db)

    assert res["ok"] is True
    assert 585 in res["demoted_ids"]
    assert res["demoted"] >= 1

    row = _read_pattern(db, 585)
    assert row.lifecycle_stage == "challenged"
    assert row.demoted_at is not None
    assert row.promotion_demote_reason == THIN_EVIDENCE_DEMOTE_REASON


def test_sweep_keeps_healthy_promoted_patterns(db):
    """Pattern 1011/1016 audit fingerprints (healthy samples) must
    stay promoted even with the provisional flag set."""
    _seed_scan_pattern(
        db, pid=1011, name="Reddit IBS mean reversion (large)",
        trade_count=409, win_rate=0.632,
    )
    _seed_scan_pattern(
        db, pid=1016, name="Reddit IBS mean reversion (xlarge)",
        trade_count=565, win_rate=0.707,
    )

    res = run_thin_evidence_demote(db)

    assert 1011 not in res["demoted_ids"]
    assert 1016 not in res["demoted_ids"]

    for pid in (1011, 1016):
        row = _read_pattern(db, pid)
        assert row.lifecycle_stage == "promoted"
        assert row.demoted_at is None


def test_sweep_does_not_touch_already_challenged(db):
    """Pattern 1047 is already 'challenged' (audit says so). The
    sweep's lifecycle=='promoted' filter must skip it cleanly."""
    _seed_scan_pattern(
        db, pid=1047, name="rsi_bullish_divergence",
        lifecycle_stage="challenged", trade_count=4, win_rate=0.25,
    )

    res = run_thin_evidence_demote(db)
    assert 1047 not in res["demoted_ids"]

    row = _read_pattern(db, 1047)
    assert row.lifecycle_stage == "challenged"
    assert row.demoted_at is None  # not touched by THIS sweep


def test_sweep_idempotent_on_second_run(db):
    """Once a pattern is demoted, the second sweep must not re-touch
    it (lifecycle != 'promoted' short-circuits)."""
    _seed_scan_pattern(db, pid=585)

    run_thin_evidence_demote(db)
    first_row = _read_pattern(db, 585)
    first_demoted_at = first_row.demoted_at

    res2 = run_thin_evidence_demote(db)
    assert res2["demoted_ids"] == []

    row = _read_pattern(db, 585)
    # demoted_at unchanged on second pass.
    assert row.demoted_at == first_demoted_at


def test_provisional_gate_reason_constant_value():
    """Pin the magic string from the brief so a typo flips visibly red."""
    assert THIN_EVIDENCE_PROVISIONAL_GATE_REASON == "provisional_small_paths"
    assert THIN_EVIDENCE_DEMOTE_REASON == "thin_evidence_low_realized_wr"
    assert THIN_EVIDENCE_MIN_TRADES == 10
    assert THIN_EVIDENCE_WIN_RATE_FLOOR == pytest.approx(0.33)
