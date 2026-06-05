"""Test the new vocabulary is wired through to the miner.

Chain: snapshot (trading_snapshots.indicator_data, the crypto-miner source) must
compute the new indicators, and the miner's _COMPLEMENTARY_POOL (its condition
vocabulary) must include them. mfi/cci/ttm_squeeze are present consistently
across the snapshot, the alert path, and compute_all_from_df (entry), so patterns
the miner builds on them evaluate correctly at entry.
"""
import numpy as np
import pandas as pd

from app.services.trading.market_data import _compute_single_indicator
from app.services.trading.learning import _COMPLEMENTARY_POOL


def _make_df(n: int = 140) -> pd.DataFrame:
    wave = 100.0 + np.cumsum(np.sin(np.arange(n) / 7.0)) + np.cos(np.arange(n) / 3.0)
    close = pd.Series(wave)
    return pd.DataFrame({
        "Open": close.shift(1).fillna(close.iloc[0]),
        "High": close * 1.012, "Low": close * 0.988, "Close": close,
        "Volume": pd.Series(1000.0 + (np.arange(n) % 11) * 150.0),
    })


def test_snapshot_computes_ttm_squeeze():
    df = _make_df()
    recs = _compute_single_indicator(df, list(range(len(df))), "ttm_squeeze")
    assert recs and isinstance(recs[-1]["value"], bool)


def test_snapshot_computes_mfi_and_cci():
    df = _make_df()
    ts = list(range(len(df)))
    for name in ("mfi", "cci"):
        recs = _compute_single_indicator(df, ts, name)
        assert recs and "value" in recs[-1], f"{name} not computed"


def test_complementary_pool_includes_new_vocab():
    inds = {c["indicator"] for c in _COMPLEMENTARY_POOL}
    assert {"mfi", "cci", "ttm_squeeze"} <= inds, f"pool missing new vocab: {inds}"


def test_pool_entries_well_formed():
    # New entries must have the same shape the miner expects (indicator/op/value).
    new = [c for c in _COMPLEMENTARY_POOL if c["indicator"] in {"mfi", "cci", "ttm_squeeze"}]
    assert len(new) >= 5
    for c in new:
        assert "indicator" in c and "op" in c and "value" in c
