"""Prescreen DB artifacts and scan read path."""
from __future__ import annotations

from unittest.mock import patch

from app.models.trading import PrescreenCandidate, PrescreenSnapshot
from app.services.trading.prescreen_job import (
    load_active_global_candidate_tickers,
    run_daily_prescreen_job,
)
from app.services.trading.prescreen_normalize import normalize_prescreen_ticker


def test_normalize_prescreen_ticker_crypto_bare() -> None:
    assert normalize_prescreen_ticker("btcusd") == "BTC-USD"
    assert normalize_prescreen_ticker("MSFT") == "MSFT"


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
    assert active == ["AAA", "MSFT", "ZZZ"]

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


def test_scheduler_prescreen_skips_when_disabled(monkeypatch) -> None:
    from app.config import settings
    from app.services.trading_scheduler import _run_daily_prescreen_job

    monkeypatch.setattr(settings, "brain_prescreen_scheduler_enabled", False)
    _run_daily_prescreen_job()
