"""Re-promote pattern 585 to lifecycle_stage='promoted'.

Reasoning: pattern 585 was producing all 1,284 pattern_breakout_imminent
alerts in the last 7 days. It got demoted by the daily auto-demote audit
at 2026-05-09 00:04 UTC with reason 'thin_evidence_low_realized_wr',
8 minutes after its last alert. CPCV evidence supports the pattern
(median sharpe 1.40, dsr 1.0, pbo 0.0, gate passed). The auto-demote
fired on 8 realized trades — too small a sample to reliably distinguish
signal from autotrader-gate noise.

Re-promotion restores alert flow. Downstream gates (rule floor 8%,
LLM revalidation, PDT, cost-aware) continue to protect capital.

NOTE: the daily audit at 02:15 PT may re-demote tomorrow. Operator
should consider raising the realized-WR sample-size threshold or
biasing the auto-demote toward CPCV evidence.
"""
from datetime import datetime, timezone

from sqlalchemy import text
from app.db import SessionLocal

PATTERN_ID = 585

db = SessionLocal()
try:
    # Pre-state
    row = db.execute(text("""
        SELECT id, name, lifecycle_stage, promotion_status, active,
               demoted_at, lifecycle_changed_at, promotion_demote_reason,
               win_rate, trade_count, cpcv_median_sharpe, deflated_sharpe
        FROM scan_patterns WHERE id = :pid
    """), {"pid": PATTERN_ID}).fetchall()
    db.rollback()
    if not row:
        print(f"ABORT: pattern {PATTERN_ID} not found")
        exit(1)
    pre = dict(row[0]._mapping)
    print(f"PRE-state for pattern {PATTERN_ID}:")
    for k, v in pre.items():
        print(f"  {k}: {v}")
    print()

    if pre.get("lifecycle_stage") == "promoted":
        print("Already promoted; nothing to do.")
        exit(0)

    # Apply: lifecycle_stage = 'promoted', clear demote markers, set timestamp
    now = datetime.now(timezone.utc)
    db.execute(text("""
        UPDATE scan_patterns
        SET lifecycle_stage = 'promoted',
            demoted_at = NULL,
            lifecycle_changed_at = :now,
            updated_at = :now
        WHERE id = :pid
    """), {"now": now, "pid": PATTERN_ID})
    db.commit()
    print(f"UPDATED pattern {PATTERN_ID}: lifecycle_stage -> 'promoted', demoted_at cleared")

    # Post-state
    row = db.execute(text("""
        SELECT id, lifecycle_stage, promotion_status, active,
               demoted_at, lifecycle_changed_at
        FROM scan_patterns WHERE id = :pid
    """), {"pid": PATTERN_ID}).fetchall()
    db.rollback()
    print()
    print("POST-state:")
    for k, v in dict(row[0]._mapping).items():
        print(f"  {k}: {v}")

    # Confirm eligibility for pattern_imminent
    row = db.execute(text("""
        SELECT count(*) FROM scan_patterns
        WHERE active = true
          AND (LOWER(COALESCE(lifecycle_stage, '')) IN ('promoted','live')
               OR LOWER(COALESCE(promotion_status, '')) = 'promoted')
    """)).fetchall()
    db.rollback()
    print()
    print(f"ELIGIBLE patterns now: {row[0][0]}")
finally:
    db.close()
