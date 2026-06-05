"""Tests for the work-ledger stall watchdog.

Catches the failure mode that stalled the backtest pipeline for 46h: a dedicated
worker dies/absent, its work type piles up unprocessed, and nothing notices. The
detector flags a type with overdue pending work AND zero recent processing (dead
processor) while NOT flagging benign cases (live handler with old/superseded
events, not-yet-overdue work, tiny backlogs, brand-new types).
"""
from sqlalchemy import text

from app.services.trading_scheduler import _detect_work_ledger_stalls

_ctr = [0]


def _ins(db, event_type, status, next_run_min_ago, processed_min_ago=None):
    _ctr[0] += 1
    db.execute(
        text(
            """
            INSERT INTO brain_work_events
                (domain, event_type, event_kind, dedupe_key, lease_scope, status,
                 attempts, max_attempts, next_run_at, processed_at, created_at, updated_at)
            VALUES ('trading', :et, 'work', :dk, 'default', :st, 0, 5,
                    now() - make_interval(mins => :nra),
                    CASE WHEN :pma IS NULL THEN NULL
                         ELSE now() - make_interval(mins => cast(:pma as int)) END,
                    now(), now())
            """
        ),
        {"et": event_type, "dk": f"wd-{_ctr[0]}", "st": status,
         "nra": int(next_run_min_ago), "pma": processed_min_ago},
    )


def test_detects_dead_processor(db):
    # overdue pending + done history but ZERO recent processing -> dead processor
    for _ in range(6):
        _ins(db, "dead_type", "pending", 300)
    for _ in range(15):
        _ins(db, "dead_type", "done", 300, processed_min_ago=200)  # done 200min ago (>120 window)
    db.flush()
    stalls = _detect_work_ledger_stalls(db, thr_min=120, min_pending=5)
    by_type = {s[0]: s for s in stalls}
    assert "dead_type" in by_type
    assert by_type["dead_type"][1] >= 6  # overdue count surfaced


def test_live_handler_not_flagged(db):
    # benign starvation: very old pending BUT handler is alive (recent done)
    for _ in range(10):
        _ins(db, "live_type", "pending", 5000)
    for _ in range(15):
        _ins(db, "live_type", "done", 5000, processed_min_ago=5)  # processed 5min ago
    db.flush()
    assert "live_type" not in {s[0] for s in _detect_work_ledger_stalls(db, 120, 5)}


def test_not_overdue_not_flagged(db):
    # pending but only 10min old (< 120 threshold)
    for _ in range(10):
        _ins(db, "fresh_type", "pending", 10)
    for _ in range(15):
        _ins(db, "fresh_type", "done", 300, processed_min_ago=200)
    db.flush()
    assert "fresh_type" not in {s[0] for s in _detect_work_ledger_stalls(db, 120, 5)}


def test_below_min_pending_not_flagged(db):
    # only 3 overdue (< min_pending=5) -> ignore tiny backlogs
    for _ in range(3):
        _ins(db, "few_type", "pending", 300)
    for _ in range(15):
        _ins(db, "few_type", "done", 300, processed_min_ago=200)
    db.flush()
    assert "few_type" not in {s[0] for s in _detect_work_ledger_stalls(db, 120, 5)}


def test_new_type_no_history_not_flagged(db):
    # done_ever <= 10 -> not a historically-real type, don't flag (avoid noise)
    for _ in range(6):
        _ins(db, "brandnew_type", "pending", 300)
    for _ in range(3):
        _ins(db, "brandnew_type", "done", 300, processed_min_ago=200)
    db.flush()
    assert "brandnew_type" not in {s[0] for s in _detect_work_ledger_stalls(db, 120, 5)}
