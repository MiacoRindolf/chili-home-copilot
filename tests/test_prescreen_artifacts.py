"""Prescreen DB artifacts and scan read path."""
from __future__ import annotations

from unittest.mock import patch

from app.models.trading import BrainBatchJob, PrescreenCandidate, PrescreenSnapshot
from app.services.trading.prescreen_job import (
    load_active_global_candidate_tickers,
    run_daily_prescreen_job,
)
from app.services.trading import prescreener
from app.services.trading.prescreen_normalize import (
    iter_normalized_prescreen_tickers,
    normalize_prescreen_ticker,
)


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


def test_main_prescreen_includes_momentum_gappers_source() -> None:
    assert "massive_momentum_gappers" in prescreener._prescreen_source_callables()


def test_scheduler_prescreen_skips_when_disabled(monkeypatch) -> None:
    from app.config import settings
    from app.services.trading_scheduler import _run_daily_prescreen_job

    monkeypatch.setattr(settings, "brain_prescreen_scheduler_enabled", False)
    _run_daily_prescreen_job()
