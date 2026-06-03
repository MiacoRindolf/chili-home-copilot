"""Runtime tab surfacing — Phase 4 of f-adaptive-promotion-architecture.

Read-only endpoint smoke tests:
- ``/api/brain/patterns/ptr-ready-but-ungated`` filters by PTR row count
  and excludes patterns that already have CPCV data.
- ``/api/brain/patterns/cpcv-verdict-diff`` folds the per-metric
  ``cpcv_adaptive_eval_log`` rows into one row per pattern and computes
  agree / disagree counts.
- ``/api/brain/dispatch-queue-depth`` aggregates ``brain_work_events`` by
  (event_kind, event_type, status) and emits a health colour.

All three endpoints handle empty tables gracefully.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from app.models import ScanPattern
from app.routers.brain import _ptr_ready_but_ungated_query


PTR_MIN = 30


def test_ptr_ready_query_is_threshold_bounded() -> None:
    sql = str(_ptr_ready_but_ungated_query()).lower()

    assert "join lateral" in sql
    assert "limit :min_rows" in sql
    assert "group by scan_pattern_id" not in sql


def _mk_pattern(db, name: str, *, cpcv_n_paths=None, lifecycle="candidate") -> int:
    sp = ScanPattern(
        name=name,
        rules_json={},
        origin="user",
        asset_class="stock",
        timeframe="1d",
        lifecycle_stage=lifecycle,
        cpcv_n_paths=cpcv_n_paths,
    )
    db.add(sp)
    db.flush()
    return int(sp.id)


def _insert_ptr_rows(db, scan_pattern_id: int, n: int) -> None:
    """Insert minimal PTR rows. Explicit values cover NOT NULL columns whose
    Python-side defaults don't materialize on raw SQL INSERTs (test schema
    is created via Base.metadata.create_all)."""
    insert_sql = text(
        """
        INSERT INTO trading_pattern_trades
          (scan_pattern_id, ticker, as_of_ts, timeframe, asset_class,
           features_json, source, feature_schema_version, created_at)
        VALUES
          (:pid, 'TEST', :as_of_ts, '1d', 'stock',
           '{}'::jsonb, 'unit_test', '1', :created_at)
        """
    )
    base_ts = datetime.utcnow()
    for idx in range(n):
        row_ts = base_ts + timedelta(seconds=idx)
        db.execute(
            insert_sql,
            {"pid": scan_pattern_id, "as_of_ts": row_ts, "created_at": row_ts},
        )


# ── /api/brain/patterns/ptr-ready-but-ungated ────────────────────────


def test_ptr_ready_but_ungated_empty_tables(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/api/brain/patterns/ptr-ready-but-ungated")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["count"] == 0
    assert d["patterns"] == []
    assert d["min_ptr_rows"] == PTR_MIN


def test_ptr_ready_but_ungated_filters_by_min_rows_and_cpcv_null(paired_client, db) -> None:
    client, _user = paired_client

    # Pattern A: enough PTRs, no CPCV → should appear
    pid_a = _mk_pattern(db, "stuck_pat", cpcv_n_paths=None, lifecycle="backtested")
    _insert_ptr_rows(db, pid_a, PTR_MIN)

    # Pattern B: enough PTRs but already has CPCV data → should NOT appear
    pid_b = _mk_pattern(db, "graded_pat", cpcv_n_paths=20, lifecycle="validated")
    _insert_ptr_rows(db, pid_b, PTR_MIN + 5)

    # Pattern C: too few PTRs, no CPCV → should NOT appear
    pid_c = _mk_pattern(db, "thin_pat", cpcv_n_paths=None, lifecycle="candidate")
    _insert_ptr_rows(db, pid_c, PTR_MIN - 1)

    db.commit()

    r = client.get("/api/brain/patterns/ptr-ready-but-ungated")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True

    returned_ids = [p["pattern_id"] for p in d["patterns"]]
    assert pid_a in returned_ids
    assert pid_b not in returned_ids
    assert pid_c not in returned_ids

    row = next(p for p in d["patterns"] if p["pattern_id"] == pid_a)
    assert row["ptr_rows"] == PTR_MIN
    assert row["has_cpcv_data"] is False
    assert row["lifecycle_stage"] == "backtested"


# ── /api/brain/patterns/cpcv-verdict-diff ────────────────────────────


def test_cpcv_verdict_diff_empty(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/api/brain/patterns/cpcv-verdict-diff")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["count"] == 0
    assert d["agree"] == 0
    assert d["disagree"] == 0
    assert d["patterns"] == []


def test_cpcv_verdict_diff_folds_metrics_and_counts_diffs(paired_client, db) -> None:
    client, _user = paired_client

    pid_agree = _mk_pattern(db, "agree_pat")
    pid_disagree = _mk_pattern(db, "disagree_pat")

    # Pattern A: both legacy and adaptive PASS (agree)
    for metric, raw_v, shrunk in (
        ("dsr", 0.95, 0.88),
        ("pbo", 0.10, 0.12),
        ("median_sharpe", 1.20, 1.10),
    ):
        db.execute(
            text(
                """
                INSERT INTO cpcv_adaptive_eval_log
                  (scan_pattern_id, metric_name, raw_value, shrunken_value,
                   eligible, pareto_dominant, legacy_verdict_pass,
                   adaptive_verdict_pass)
                VALUES (:pid, :m, :r, :s, TRUE, TRUE, TRUE, TRUE)
                """
            ),
            {"pid": pid_agree, "m": metric, "r": raw_v, "s": shrunk},
        )

    # Pattern B: legacy PASS, adaptive FAIL (disagree)
    for metric, raw_v, shrunk in (
        ("dsr", 0.80, 0.60),
        ("pbo", 0.30, 0.35),
        ("median_sharpe", 0.50, 0.40),
    ):
        db.execute(
            text(
                """
                INSERT INTO cpcv_adaptive_eval_log
                  (scan_pattern_id, metric_name, raw_value, shrunken_value,
                   eligible, pareto_dominant, legacy_verdict_pass,
                   adaptive_verdict_pass)
                VALUES (:pid, :m, :r, :s, FALSE, FALSE, TRUE, FALSE)
                """
            ),
            {"pid": pid_disagree, "m": metric, "r": raw_v, "s": shrunk},
        )

    db.commit()

    r = client.get("/api/brain/patterns/cpcv-verdict-diff")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["count"] == 2
    assert d["agree"] == 1
    assert d["disagree"] == 1

    by_id = {p["pattern_id"]: p for p in d["patterns"]}
    a = by_id[pid_agree]
    assert a["legacy_pass"] is True
    assert a["adaptive_pass"] is True
    assert a["pareto_dominant"] is True
    assert a["shrunken_dsr"] is not None
    assert a["shrunken_pbo"] is not None
    assert a["shrunken_med_sharpe"] is not None

    b = by_id[pid_disagree]
    assert b["legacy_pass"] is True
    assert b["adaptive_pass"] is False
    assert b["pareto_dominant"] is False


# ── /api/brain/dispatch-queue-depth ──────────────────────────────────


def test_dispatch_queue_depth_empty(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/api/brain/dispatch-queue-depth")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["totals"]["pending"] == 0
    assert d["totals"]["processing"] == 0
    assert d["totals"]["retry_wait"] == 0
    assert d["totals"]["dead"] == 0
    assert d["buckets"] == []
    assert d["health"] == "green"


def test_dispatch_queue_depth_aggregates_by_event_type_status(paired_client, db) -> None:
    client, _user = paired_client

    rows = [
        ("backtest_completed",       "outcome", "pending",    "k1"),
        ("backtest_completed",       "outcome", "pending",    "k2"),
        ("backtest_completed",       "outcome", "processing", "k3"),
        ("pattern_eligible_promotion","work",   "pending",    "k4"),
        ("pattern_eligible_promotion","work",   "dead",       "k5"),
    ]
    # Explicit values for non-null columns whose Python-side defaults
    # don't materialize on raw SQL INSERTs (e.g. when the schema is
    # created via Base.metadata.create_all in conftest).
    insert_sql = text(
        """
        INSERT INTO brain_work_events
          (domain, event_type, event_kind, status, dedupe_key, payload,
           attempts, max_attempts, next_run_at, lease_scope,
           created_at, updated_at)
        VALUES (
          'trading', :et, :kind, :st, :dk, '{}'::jsonb,
          0, 5, CURRENT_TIMESTAMP, 'general',
          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        """
    )
    for et, kind, status, dk in rows:
        db.execute(insert_sql, {"et": et, "kind": kind, "st": status, "dk": dk})
    # A 'done' row that should be excluded entirely
    db.execute(insert_sql, {
        "et": "backtest_completed", "kind": "outcome",
        "st": "done", "dk": "k_done",
    })
    db.commit()

    r = client.get("/api/brain/dispatch-queue-depth")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True

    # Totals: 3 pending, 1 processing, 0 retry, 1 dead
    assert d["totals"]["pending"] == 3
    assert d["totals"]["processing"] == 1
    assert d["totals"]["retry_wait"] == 0
    assert d["totals"]["dead"] == 1

    # Buckets: 4 distinct (event_type, kind, status) combos in the unfinished set
    assert len(d["buckets"]) == 4

    # Verify that 'done' was excluded
    for b in d["buckets"]:
        assert b["status"] in ("pending", "processing", "retry_wait", "dead")

    # Health is determined by oldest pending/retry_wait age. Fresh rows → green.
    assert d["health"] in ("green", "yellow")
