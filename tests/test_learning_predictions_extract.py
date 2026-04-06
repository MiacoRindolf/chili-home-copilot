"""learning_predictions extraction: facade parity and prediction row shape."""
from __future__ import annotations

import pytest


def test_learning_facade_wires_prediction_helpers() -> None:
    from app.services.trading import learning as L
    from app.services.trading import learning_predictions as LP

    assert L.predict_direction is LP.predict_direction
    assert L.predict_confidence is LP.predict_confidence
    assert L._indicator_data_to_flat_snapshot is LP._indicator_data_to_flat_snapshot
    assert hasattr(L, "get_current_predictions")
    assert hasattr(L, "refresh_promoted_prediction_cache")
    assert not hasattr(LP, "get_current_predictions")
    assert L._get_current_predictions_impl.__code__ is not LP._get_current_predictions_impl.__code__


def test_compute_prediction_stays_on_learning_module() -> None:
    from app.services.trading import learning as L
    from app.services.trading import learning_predictions as LP

    assert hasattr(L, "compute_prediction")
    assert not hasattr(LP, "compute_prediction")


def test_prediction_row_keys_stable() -> None:
    """Frozen keys for legacy list dict prediction rows (mirror and API consumers)."""
    keys = {
        "ticker", "price", "score", "meta_ml_probability", "direction", "confidence",
        "signals", "matched_patterns", "vix_regime", "suggested_stop", "suggested_target",
        "risk_reward", "position_size_pct",
    }
    row = {k: None for k in keys}
    assert set(row) == keys


def test_get_current_predictions_explicit_list_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.trading import learning as learning_mod

    calls: list[tuple] = []

    def fake_impl(db, tickers, **kwargs):
        calls.append((tuple(tickers or ()), kwargs.get("explicit_api_tickers")))
        return []

    monkeypatch.setattr(learning_mod, "_get_current_predictions_impl", fake_impl)
    learning_mod.get_current_predictions(None, ["AAA", "BBB"])  # type: ignore[arg-type]
    assert calls == [(("AAA", "BBB"), True)]
