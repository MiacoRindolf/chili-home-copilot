"""Phase 6: bounded chili_prediction_ops INFO line (flag-gated) + phase5 read metadata."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.trading_brain.infrastructure.prediction_ops_log import (
    CHILI_PREDICTION_OPS_PREFIX,
    DUAL_WRITE_NA,
    DUAL_WRITE_OK,
    READ_COMPARE_MISS,
    READ_NA,
    format_chili_prediction_ops_line,
)


def _minimal_prediction_row() -> dict:
    return {
        "ticker": "TST",
        "price": 100.0,
        "score": 2.5,
        "meta_ml_probability": None,
        "direction": "long",
        "confidence": 60,
        "signals": [],
        "matched_patterns": [],
        "vix_regime": "normal",
        "suggested_stop": None,
        "suggested_target": None,
        "risk_reward": None,
        "position_size_pct": None,
    }


def test_chili_prediction_ops_line_contract() -> None:
    line = format_chili_prediction_ops_line(
        dual_write=DUAL_WRITE_OK,
        read=READ_COMPARE_MISS,
        explicit_api_tickers=False,
        fp16="deadbeef01234567",
        snapshot_id=None,
        line_count=3,
    )
    assert line.startswith(CHILI_PREDICTION_OPS_PREFIX)
    assert "dual_write=ok" in line
    assert "read=compare_miss" in line
    assert "explicit_api_tickers=false" in line
    assert "fp16=deadbeef01234567" in line
    assert "snapshot_id=none" in line
    assert "line_count=3" in line


def _run_get_current_predictions_impl_stub(
    db: Session, *, explicit_api_tickers: bool
) -> None:
    from app.services.trading.learning import _get_current_predictions_impl

    fake_pat = MagicMock()
    fake_pat.ticker_scope = "universal"
    ml = MagicMock()
    ml.is_ready.return_value = False
    with patch("app.services.trading.pattern_engine.get_active_patterns", return_value=[fake_pat]), patch(
        "app.services.trading.market_data.fetch_quotes_batch", return_value={"TST": {"price": 100.0}}
    ), patch(
        "app.services.trading.learning_predictions._predict_single_ticker", return_value=_minimal_prediction_row()
    ), patch("app.services.trading.market_data.get_vix", return_value=15.0), patch(
        "app.services.trading.market_data.get_volatility_regime", return_value={"regime": "normal"}
    ), patch("app.services.trading.pattern_ml.get_meta_learner", return_value=ml):
        _get_current_predictions_impl(db, ["TST"], explicit_api_tickers=explicit_api_tickers)


def test_ops_log_disabled_emits_no_chili_prediction_ops(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_ops_log_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_dual_write_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", False, raising=False)
    caplog.set_level(logging.INFO)
    _run_get_current_predictions_impl_stub(db, explicit_api_tickers=True)
    assert not any(CHILI_PREDICTION_OPS_PREFIX in r.getMessage() for r in caplog.records)


def test_ops_log_enabled_single_info_line_na_na(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_ops_log_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_dual_write_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", False, raising=False)
    # Scope the level to the parent package so messages from both ``learning``
    # (original integration location) and ``learning_predictions`` (post-
    # extract location; b3a6b6d + Phase-D follow-up restored the ops-log
    # emit there) are captured.
    caplog.set_level(logging.INFO, logger="app.services.trading")
    _run_get_current_predictions_impl_stub(db, explicit_api_tickers=True)
    hits = [r for r in caplog.records if CHILI_PREDICTION_OPS_PREFIX in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno == logging.INFO
    msg = hits[0].getMessage()
    assert f"dual_write={DUAL_WRITE_NA}" in msg
    assert f"read={READ_NA}" in msg
    assert "explicit_api_tickers=true" in msg


def test_ops_log_dual_write_ok_when_enabled(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "brain_prediction_ops_log_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_dual_write_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_compare_enabled", False, raising=False)
    monkeypatch.setattr(settings, "brain_prediction_read_authoritative_enabled", False, raising=False)
    # Scope the level to the parent package so messages from both ``learning``
    # (original integration location) and ``learning_predictions`` (post-
    # extract location; b3a6b6d + Phase-D follow-up restored the ops-log
    # emit there) are captured.
    caplog.set_level(logging.INFO, logger="app.services.trading")
    with patch(
        "app.trading_brain.infrastructure.prediction_mirror_session.brain_prediction_mirror_write_dedicated"
    ) as w:
        _run_get_current_predictions_impl_stub(db, explicit_api_tickers=False)
        w.assert_called_once()
    hits = [r for r in caplog.records if CHILI_PREDICTION_OPS_PREFIX in r.getMessage()]
    assert len(hits) == 1
    assert f"dual_write={DUAL_WRITE_OK}" in hits[0].getMessage()
