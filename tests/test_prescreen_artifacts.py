"""Prescreen DB artifacts and scan read path."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.trading import BrainBatchJob, PrescreenCandidate, PrescreenSnapshot
from app.services.trading.brain_batch_job_log import (
    brain_batch_job_begin,
    brain_batch_job_finish,
)
from app.services.trading.prescreen_job import (
    _global_candidates_by_ticker_norm,
    load_active_global_candidate_source_tags,
    load_active_global_candidate_tickers,
    run_daily_prescreen_job,
)
from app.services.trading import prescreener
from app.services.trading.prescreen_normalize import (
    iter_normalized_prescreen_tickers,
    normalize_prescreen_ticker,
)


def _poison_session(session) -> None:
    """Leave the session's transaction in the aborted state a transient Postgres
    drop produces. A failed statement makes PostgreSQL abort the current
    transaction, so the *next* ORM query raises ``PendingRollbackError`` ("Can't
    reconnect until invalid transaction is rolled back") — exactly the cascade in
    the prescreen tracebacks (psycopg2.OperationalError -> PendingRollbackError).
    """
    try:
        session.execute(text("SELECT 1 FROM __chili_nonexistent_table_zzz__"))
    except Exception:
        pass


def test_normalize_prescreen_ticker_crypto_bare() -> None:
    assert normalize_prescreen_ticker("btcusd") == "BTC-USD"
    assert normalize_prescreen_ticker("MSFT") == "MSFT"


def test_normalize_prescreen_ticker_rejects_scope_fragments() -> None:
    assert normalize_prescreen_ticker('["ACMR"') == ""
    assert normalize_prescreen_ticker('"INFQ"]') == ""
    assert normalize_prescreen_ticker("crypto") == ""


def test_iter_normalized_prescreen_tickers_parses_scope_json() -> None:
    assert iter_normalized_prescreen_tickers('["ACMR", "INFQ", "BTCUSD"]') == [
        "ACMR",
        "INFQ",
        "BTC-USD",
    ]


@patch("app.services.trading.prescreen_job.collect_internal_prescreen_tickers")
@patch("app.services.trading.prescreen_job.collect_prescreen_with_provenance")
def test_run_daily_prescreen_upsert_and_deactivate(
    mock_collect, mock_internal, db,
) -> None:
    mock_collect.return_value = (
        ["ZZZ", "AAA"],
        {"AAA": ["core_default"], "ZZZ": ["yf_most_actives"]},
        {"core_default": 1, "yf_most_actives": 1},
        0.05,
    )
    mock_internal.return_value = {"MSFT": [{"kind": "brain_prediction", "snapshot_id": 1}]}

    r1 = run_daily_prescreen_job(db)
    assert r1.get("ok") is True
    active = load_active_global_candidate_tickers(db)
    assert active == ["MSFT", "ZZZ", "AAA"]
    _aa = db.query(PrescreenCandidate).filter(PrescreenCandidate.ticker_norm == "AAA").first()
    assert _aa is not None and _aa.asset_universe == "stock"
    _jobs = db.query(BrainBatchJob).filter(BrainBatchJob.job_type == "daily_prescreen").all()
    assert len(_jobs) >= 1
    assert _jobs[-1].status == "ok"
    assert _jobs[-1].ended_at is not None

    mock_collect.return_value = (
        ["AAA"],
        {"AAA": ["core_default"]},
        {"core_default": 1},
        0.01,
    )
    mock_internal.return_value = {}
    r2 = run_daily_prescreen_job(db)
    assert r2.get("ok") is True
    active2 = load_active_global_candidate_tickers(db)
    assert active2 == ["AAA"]
    z = db.query(PrescreenCandidate).filter(PrescreenCandidate.ticker_norm == "ZZZ").first()
    assert z is not None and z.active is False


@patch("app.services.trading.prescreen_job.collect_internal_prescreen_tickers")
@patch("app.services.trading.prescreen_job.collect_prescreen_with_provenance")
def test_snapshot_row_written(mock_collect, mock_internal, db) -> None:
    mock_collect.return_value = (["XOM"], {"XOM": ["core_default"]}, {"core_default": 1}, 0.02)
    mock_internal.return_value = {}
    run_daily_prescreen_job(db)
    snap = db.query(PrescreenSnapshot).order_by(PrescreenSnapshot.id.desc()).first()
    assert snap is not None
    assert snap.candidate_count == 1
    assert snap.source_map_json == {"core_default": 1}


def test_load_active_global_empty(db) -> None:
    assert load_active_global_candidate_tickers(db) == []


def test_load_active_global_prioritizes_momentum_source_tags(db) -> None:
    db.add_all(
        [
            PrescreenCandidate(
                ticker="AAA",
                ticker_norm="AAA",
                asset_universe="stock",
                active=True,
                entry_reasons=[],
                sources_json={"tags": ["core_default"]},
            ),
            PrescreenCandidate(
                ticker="ZZZ",
                ticker_norm="ZZZ",
                asset_universe="stock",
                active=True,
                entry_reasons=[],
                sources_json={"tags": ["massive_momentum_gappers"]},
            ),
            PrescreenCandidate(
                ticker="BBB",
                ticker_norm="BBB",
                asset_universe="stock",
                active=True,
                entry_reasons=[],
                sources_json={"tags": ["massive_high_rel_volume"]},
            ),
        ]
    )
    db.commit()

    assert load_active_global_candidate_tickers(db) == ["ZZZ", "BBB", "AAA"]
    assert load_active_global_candidate_source_tags(db, {"ZZZ", "AAA"}) == {
        "AAA": ["core_default"],
        "ZZZ": ["massive_momentum_gappers"],
    }


def test_main_prescreen_includes_momentum_gappers_source() -> None:
    assert "massive_momentum_gappers" in prescreener._prescreen_source_callables()


def test_scheduler_prescreen_skips_when_disabled(monkeypatch) -> None:
    from app.config import settings
    from app.services.trading_scheduler import _run_daily_prescreen_job

    monkeypatch.setattr(settings, "brain_prescreen_scheduler_enabled", False)
    _run_daily_prescreen_job()


# ---------------------------------------------------------------------------
# Connection-drop resilience (FIX 46 rollback-on-error): a transient Postgres
# drop must not cascade into a failed prescreen run / noisy traceback storm.
# ---------------------------------------------------------------------------


def test_brain_batch_job_finish_recovers_from_aborted_transaction(db) -> None:
    """finish() rolls back an aborted tx and retries instead of raising PendingRollbackError."""
    job_id = brain_batch_job_begin(db, "daily_prescreen")
    db.commit()  # persist so the row survives the rollback finish() must perform

    _poison_session(db)  # simulate the connection drop -> aborted transaction

    # Pre-fix this raised PendingRollbackError; now it recovers and marks the job.
    brain_batch_job_finish(db, job_id, ok=True, meta={"recovered": True})
    db.commit()

    row = db.query(BrainBatchJob).filter(BrainBatchJob.id == job_id).first()
    assert row is not None
    assert row.status == "ok"
    assert row.ended_at is not None
    assert row.meta_json == {"recovered": True}


def test_global_candidates_lookup_rolls_back_aborted_transaction(db) -> None:
    """The lookup clears the aborted tx (re-raising) so the session stays usable."""
    db.add(
        PrescreenCandidate(
            ticker="AAPL",
            ticker_norm="AAPL",
            asset_universe="stock",
            active=True,
            entry_reasons=[],
            sources_json={"tags": ["core_default"]},
        )
    )
    db.commit()

    _poison_session(db)

    # It cannot safely upsert without the existing-row map, so it re-raises —
    # but only after rolling the dead transaction back.
    with pytest.raises(Exception):
        _global_candidates_by_ticker_norm(db, {"AAPL"})

    # The cascade is broken: a follow-up query works rather than hitting
    # PendingRollbackError.
    healthy = _global_candidates_by_ticker_norm(db, {"AAPL"})
    assert "AAPL" in healthy


@patch("app.services.trading.prescreen_job.collect_internal_prescreen_tickers")
@patch("app.services.trading.prescreen_job.collect_prescreen_with_provenance")
def test_run_daily_prescreen_recovers_from_connection_drop(
    mock_collect, mock_internal, db,
) -> None:
    """A mid-run connection drop fails the run gracefully and leaves the next run healthy."""
    mock_collect.return_value = (
        ["AAPL", "MSFT"],
        {"AAPL": ["massive_top_gainers"], "MSFT": ["core_default"]},
        {"massive_top_gainers": 1, "core_default": 1},
        0.05,
    )

    def _drop_connection_then_return(session):
        # Poison the tx just before the _global_candidates_by_ticker_norm lookup
        # so it hits the aborted-transaction path mid-run.
        _poison_session(session)
        return {}

    mock_internal.side_effect = _drop_connection_then_return

    # Must report failure, NOT raise PendingRollbackError out of the job.
    result = run_daily_prescreen_job(db)
    assert result.get("ok") is False
    assert "error" in result

    # The decisive check: the session recovered, so the NEXT scheduled run
    # (the fresh universe feeding the 2:30am market scan) succeeds.
    db.rollback()
    mock_internal.side_effect = None
    mock_internal.return_value = {}
    result2 = run_daily_prescreen_job(db)
    assert result2.get("ok") is True
    active = load_active_global_candidate_tickers(db)
    assert "AAPL" in active
    assert "MSFT" in active


@patch("app.services.trading.prescreen_job.collect_internal_prescreen_tickers")
@patch("app.services.trading.prescreen_job.collect_prescreen_with_provenance")
def test_snapshot_committed_before_collection_survives_midrun_rollback(
    mock_collect, mock_internal, db,
) -> None:
    """Regression for the trading_prescreen_candidates_snapshot_id_fkey storm.

    The real incident: the snapshot was only *flushed* before the multi-minute
    external collection, so the connection sat idle-in-transaction past the 120s
    ``idle_in_transaction_session_timeout`` and Postgres killed it. The drop
    rolled the session back (``rollback_if_poisoned``) — discarding the
    snapshot — yet ``snap.id`` still held the orphaned value, so the candidate
    upsert violated the FK. This reproduces that exact sequence by rolling the
    session back mid-collection and asserts the run still succeeds with no
    orphaned ``snapshot_id`` (i.e. the snapshot was committed up front and is a
    durable FK target). Before the fix this raised the FK IntegrityError and the
    run returned ``ok=False``.
    """
    mock_collect.return_value = (
        ["AAPL", "MSFT"],
        {"AAPL": ["massive_top_gainers"], "MSFT": ["core_default"]},
        {"massive_top_gainers": 1, "core_default": 1},
        0.05,
    )

    def _rollback_midrun_then_return(session):
        # Mirror rollback_if_poisoned firing after a mid-transaction disconnect:
        # the session is rolled back clean, which would discard any
        # flushed-but-uncommitted snapshot along with it.
        session.rollback()
        return {}

    mock_internal.side_effect = _rollback_midrun_then_return

    result = run_daily_prescreen_job(db)
    assert result.get("ok") is True

    active = load_active_global_candidate_tickers(db)
    assert "AAPL" in active
    assert "MSFT" in active

    orphans = db.execute(
        text(
            "SELECT count(*) FROM trading_prescreen_candidates c "
            "WHERE c.snapshot_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM trading_prescreen_snapshots s WHERE s.id = c.snapshot_id)"
        )
    ).scalar()
    assert orphans == 0
