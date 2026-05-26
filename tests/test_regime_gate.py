from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import regime_gate

PATTERN_ID = 1248
CRYPTO_TICKER = "TRIA-USD"
EQUITY_TICKER = "AAPL"
MIN_TRADES = 5
MAX_AGE_DAYS = 7
MIN_NEGATIVES = 2
NEGATIVE_MEAN_PNL = -0.25
POSITIVE_MEAN_PNL = 0.40
HIT_RATE = 0.55


def _ledger(mean_pnl_pct: float) -> SimpleNamespace:
    return SimpleNamespace(
        n_trades=MIN_TRADES,
        hit_rate=HIT_RATE,
        mean_pnl_pct=mean_pnl_pct,
        has_confidence=True,
    )


def _install_regime_gate_fakes(monkeypatch, means: dict[str, float]) -> None:
    labels = {
        regime_gate.REGIME_DIM_TICKER: "ticker_label",
        regime_gate.REGIME_DIM_BREADTH: "breadth_label",
        regime_gate.REGIME_DIM_CROSS_ASSET: "cross_label",
        regime_gate.REGIME_DIM_VOL: "vol_label",
    }

    def _settings(name: str, default):
        overrides = {
            "chili_regime_gate_enabled": True,
            "chili_regime_gate_mode": "live",
            "chili_regime_gate_min_trades": MIN_TRADES,
            "chili_regime_gate_max_age_days": MAX_AGE_DAYS,
            "chili_regime_gate_min_negatives": MIN_NEGATIVES,
            "chili_regime_gate_require_crypto_anchor_negative": True,
            "chili_regime_gate_crypto_anchor_dimensions": (
                "ticker_regime,cross_asset_regime"
            ),
        }
        return overrides.get(name, default)

    def _ledger_row(_sess, _pattern_id, dimension, _label, *, max_age_days):
        assert max_age_days == MAX_AGE_DAYS
        return _ledger(means[dimension])

    monkeypatch.setattr(regime_gate, "_settings_get", _settings)
    monkeypatch.setattr(regime_gate, "_current_regime_labels", lambda _sess, _ticker: labels)
    monkeypatch.setattr(regime_gate, "_ledger_row", _ledger_row)


def test_crypto_breadth_and_vol_negatives_need_anchor(monkeypatch):
    _install_regime_gate_fakes(
        monkeypatch,
        {
            regime_gate.REGIME_DIM_TICKER: POSITIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_BREADTH: NEGATIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_CROSS_ASSET: POSITIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_VOL: NEGATIVE_MEAN_PNL,
        },
    )

    result = regime_gate.evaluate_regime_gate(
        SimpleNamespace(),
        pattern_id=PATTERN_ID,
        ticker=CRYPTO_TICKER,
    )

    assert result.blocked is False
    assert result.n_negative == MIN_NEGATIVES
    assert result.reason.startswith("insufficient_crypto_anchor_negative_consensus")


def test_crypto_blocks_when_anchor_and_global_dimension_are_negative(monkeypatch):
    _install_regime_gate_fakes(
        monkeypatch,
        {
            regime_gate.REGIME_DIM_TICKER: NEGATIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_BREADTH: POSITIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_CROSS_ASSET: POSITIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_VOL: NEGATIVE_MEAN_PNL,
        },
    )

    result = regime_gate.evaluate_regime_gate(
        SimpleNamespace(),
        pattern_id=PATTERN_ID,
        ticker=CRYPTO_TICKER,
    )

    assert result.blocked is True
    assert result.reason.startswith("negative_ev_consensus")


def test_equity_still_blocks_breadth_and_vol_consensus(monkeypatch):
    _install_regime_gate_fakes(
        monkeypatch,
        {
            regime_gate.REGIME_DIM_TICKER: POSITIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_BREADTH: NEGATIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_CROSS_ASSET: POSITIVE_MEAN_PNL,
            regime_gate.REGIME_DIM_VOL: NEGATIVE_MEAN_PNL,
        },
    )

    result = regime_gate.evaluate_regime_gate(
        SimpleNamespace(),
        pattern_id=PATTERN_ID,
        ticker=EQUITY_TICKER,
    )

    assert result.blocked is True
    assert result.reason.startswith("negative_ev_consensus")
