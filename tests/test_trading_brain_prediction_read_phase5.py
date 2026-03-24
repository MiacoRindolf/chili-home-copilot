"""Phase 5: prediction mirror read compare + candidate-authoritative (Postgres)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.trading_brain.infrastructure.prediction_line_mapper import (
    legacy_prediction_rows_to_dtos,
    prediction_universe_fingerprint,
)
from app.trading_brain.infrastructure.prediction_read_parity import legacy_mirror_list_parity_ok
from app.trading_brain.infrastructure.prediction_ops_log import (
    READ_AUTH_MIRROR,
    READ_COMPARE_MISS,
    READ_COMPARE_MISMATCH,
    READ_COMPARE_OK,
    READ_FALLBACK_INELIGIBLE,
    READ_FALLBACK_STALE,
)
from app.trading_brain.infrastructure.prediction_read_phase5 import phase5_apply_prediction_read
from app.trading_brain.infrastructure.repositories.prediction_snapshot_sqlalchemy import (
    SqlAlchemyBrainPredictionSnapshotRepository,
)
from app.trading_brain.schemas.prediction_snapshot import PredictionSnapshotSealDTO


def _sample_legacy() -> list[dict]:
    return [
        {
            "ticker": "TST",
            "price": 100.0,
            "score": 2.5,
            "meta_ml_probability": 0.55,
            "direction": "long",
            "confidence": 60,
            "signals": ["Pattern: x"],
            "matched_patterns": [{"name": "p", "win_rate": 50}],
            "vix_regime": "normal",
            "suggested_stop": 99.0,
            "suggested_target": 102.0,
            "risk_reward": 1.5,
            "position_size_pct": 2.0,
        }
    ]


def test_parity_accepts_isclose_floats() -> None:
    a = _sample_legacy()
    b = [dict(a[0])]
    b[0]["score"] = 2.5 + 1e-10
    ok, _ = legacy_mirror_list_parity_ok(a, b)
    assert ok


def test_parity_rejects_signals_order() -> None:
    a = _sample_legacy()
    b = [dict(a[0])]
    b[0]["signals"] = ["b", "a"]
    ok, reason = legacy_mirror_list_parity_ok(a, b)
    assert not ok
    assert "signals" in reason


def test_parity_matched_patterns_order_invariant() -> None:
    a = _sample_legacy()
    b = [dict(a[0])]
    b[0]["matched_patterns"] = [{"win_rate": 50, "name": "p"}]
    ok, _ = legacy_mirror_list_parity_ok(a, b)
    assert ok


def test_compare_miss_emits_debug_only(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", False, raising=False)
    caplog.set_level(logging.DEBUG, logger="app.trading_brain.infrastructure.prediction_read_phase5")
    legacy = _sample_legacy()
    out, meta = phase5_apply_prediction_read(
        results=list(legacy),
        ticker_batch=["TST"],
        explicit_api_tickers=True,
    )
    assert out == legacy
    assert meta.read == READ_COMPARE_MISS
    assert any("mirror_miss" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_compare_mismatch_warning(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", False, raising=False)
    legacy = _sample_legacy()
    bad = [dict(legacy[0])]
    bad[0]["score"] = 99.0
    fp = prediction_universe_fingerprint(["TST"])
    repo = SqlAlchemyBrainPredictionSnapshotRepository()
    sid = repo.seal_snapshot(
        db,
        header=PredictionSnapshotSealDTO(universe_fingerprint=fp, ticker_count=1),
        lines=legacy_prediction_rows_to_dtos(bad),
    )
    db.commit()
    caplog.set_level(logging.WARNING, logger="app.trading_brain.infrastructure.prediction_read_phase5")
    out, meta = phase5_apply_prediction_read(
        results=list(legacy),
        ticker_batch=["TST"],
        explicit_api_tickers=True,
    )
    assert out == legacy
    assert any("parity_mismatch" in r.message for r in caplog.records)


def test_compare_full_parity_no_warning_storm(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", False, raising=False)
    legacy = _sample_legacy()
    fp = prediction_universe_fingerprint(["TST"])
    repo = SqlAlchemyBrainPredictionSnapshotRepository()
    repo.seal_snapshot(
        db,
        header=PredictionSnapshotSealDTO(universe_fingerprint=fp, ticker_count=1),
        lines=legacy_prediction_rows_to_dtos(legacy),
    )
    db.commit()
    caplog.set_level(logging.DEBUG, logger="app.trading_brain.infrastructure.prediction_read_phase5")
    _out, meta = phase5_apply_prediction_read(
        results=list(legacy),
        ticker_batch=["TST"],
        explicit_api_tickers=True,
    )
    assert not any("parity_mismatch" in r.message for r in caplog.records)
    assert meta.read == READ_COMPARE_OK


def test_authoritative_returns_mirror_when_fresh(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_max_age_seconds", 900, raising=False)
    legacy = _sample_legacy()
    fp = prediction_universe_fingerprint(["TST"])
    repo = SqlAlchemyBrainPredictionSnapshotRepository()
    repo.seal_snapshot(
        db,
        header=PredictionSnapshotSealDTO(universe_fingerprint=fp, ticker_count=1),
        lines=legacy_prediction_rows_to_dtos(legacy),
    )
    db.commit()
    out, meta = phase5_apply_prediction_read(
        results=list(legacy),
        ticker_batch=["TST"],
        explicit_api_tickers=True,
    )
    assert out == legacy
    assert out[0]["ticker"] == "TST"
    assert meta.read == READ_AUTH_MIRROR


def test_authoritative_disabled_when_not_explicit_api_tickers(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", True, raising=False)
    legacy = _sample_legacy()
    wrong = [dict(legacy[0])]
    wrong[0]["score"] = -99.0
    fp = prediction_universe_fingerprint(["TST"])
    repo = SqlAlchemyBrainPredictionSnapshotRepository()
    repo.seal_snapshot(
        db,
        header=PredictionSnapshotSealDTO(universe_fingerprint=fp, ticker_count=1),
        lines=legacy_prediction_rows_to_dtos(legacy),
    )
    db.commit()
    out, meta = phase5_apply_prediction_read(
        results=list(wrong),
        ticker_batch=["TST"],
        explicit_api_tickers=False,
    )
    assert out[0]["score"] == -99.0
    assert meta.read == READ_FALLBACK_INELIGIBLE


def test_authoritative_stale_fallback_legacy(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_max_age_seconds", 60, raising=False)
    legacy = _sample_legacy()
    fp = prediction_universe_fingerprint(["TST"])
    repo = SqlAlchemyBrainPredictionSnapshotRepository()
    sid = repo.seal_snapshot(
        db,
        header=PredictionSnapshotSealDTO(universe_fingerprint=fp, ticker_count=1),
        lines=legacy_prediction_rows_to_dtos(legacy),
    )
    db.commit()
    from sqlalchemy import text

    old = datetime.utcnow() - timedelta(hours=2)
    db.execute(
        text("UPDATE brain_prediction_snapshot SET as_of_ts = :ts WHERE id = :id"),
        {"ts": old, "id": sid},
    )
    db.commit()
    alt = [dict(legacy[0])]
    alt[0]["score"] = 7.0
    out, meta = phase5_apply_prediction_read(
        results=list(alt),
        ticker_batch=["TST"],
        explicit_api_tickers=True,
    )
    assert out[0]["score"] == 7.0
    assert meta.read == READ_FALLBACK_STALE
