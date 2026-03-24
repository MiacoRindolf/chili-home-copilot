"""Phase 7: explicit vs implicit caller contract for mirror read authority (hardening only)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.services.trading import learning as learning_mod


def test_get_current_predictions_nonempty_list_passes_explicit_true(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_impl(
        db_arg: Session,
        tickers: list[str] | None,
        *,
        explicit_api_tickers: bool = False,
    ) -> list:
        captured["explicit"] = explicit_api_tickers
        captured["tickers"] = tickers
        return []

    monkeypatch.setattr(learning_mod, "_get_current_predictions_impl", fake_impl)
    learning_mod.get_current_predictions(db, ["aapl", "msft"])
    assert captured["explicit"] is True
    assert captured["tickers"] == ["aapl", "msft"]


def test_get_current_predictions_empty_list_passes_explicit_false(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_impl(
        db_arg: Session,
        tickers: list[str] | None,
        *,
        explicit_api_tickers: bool = False,
    ) -> list:
        captured["explicit"] = explicit_api_tickers
        captured["tickers"] = tickers
        return []

    monkeypatch.setattr(learning_mod, "_get_current_predictions_impl", fake_impl)
    learning_mod.get_current_predictions(db, [])
    assert captured["explicit"] is False
    assert captured["tickers"] == []


def test_impl_coerces_explicit_false_when_tickers_none_or_empty(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[bool] = []

    def capture_phase5(*, results, ticker_batch, explicit_api_tickers):
        recorded.append(explicit_api_tickers)
        from app.trading_brain.infrastructure.prediction_ops_log import READ_NA
        from app.trading_brain.infrastructure.prediction_read_phase5 import PredictionReadOpsMeta

        return results, PredictionReadOpsMeta(read=READ_NA, fp16="none")

    monkeypatch.setattr(
        "app.trading_brain.infrastructure.prediction_read_phase5.phase5_apply_prediction_read",
        capture_phase5,
    )
    monkeypatch.setattr("app.services.trading.pattern_engine.get_active_patterns", lambda _db: [MagicMock(ticker_scope="universal")])
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quotes_batch",
        lambda _batch: {"TST": {"price": 100.0}},
    )
    monkeypatch.setattr(
        "app.services.trading.learning._predict_single_ticker",
        lambda *a, **k: {
            "ticker": "TST",
            "price": 100.0,
            "score": 1.0,
            "meta_ml_probability": None,
            "direction": "long",
            "confidence": 50,
            "signals": [],
            "matched_patterns": [],
            "vix_regime": "normal",
            "suggested_stop": None,
            "suggested_target": None,
            "risk_reward": None,
            "position_size_pct": None,
        },
    )
    monkeypatch.setattr("app.services.trading.market_data.get_vix", lambda: 15.0)
    monkeypatch.setattr(
        "app.services.trading.market_data.get_volatility_regime",
        lambda _v: {"regime": "normal"},
    )
    ml = MagicMock()
    ml.is_ready.return_value = False
    monkeypatch.setattr("app.services.trading.pattern_ml.get_meta_learner", lambda: ml)

    learning_mod._get_current_predictions_impl(db, None, explicit_api_tickers=True)
    assert recorded[-1] is False

    recorded.clear()
    learning_mod._get_current_predictions_impl(db, [], explicit_api_tickers=True)
    assert recorded[-1] is False
