"""Test the _score_ticker expanded-vocabulary helper.

The alert path (scanner._score_ticker_impl / _intraday / crypto / breakout) now
spreads _expanded_vocab_indicators into its score['indicators'] dict, so the new
volume-flow / alt-momentum / squeeze indicators flow to flat_indicators -> the
miner. This asserts the helper produces those keys and that its values match
indicator_core.compute_all_from_df (the entry surface) by construction.
"""
import numpy as np
import pandas as pd

from app.services.trading.indicator_core import compute_all_from_df
from app.services.trading.scanner import _expanded_vocab_indicators

NUMERIC = ["obv", "mfi", "vwap", "cci", "roc", "keltner_upper", "keltner_lower", "keltner_mid"]


def _make_df(n: int = 140) -> pd.DataFrame:
    wave = 100.0 + np.cumsum(np.sin(np.arange(n) / 7.0)) + np.cos(np.arange(n) / 3.0)
    close = pd.Series(wave)
    return pd.DataFrame({
        "Open": close.shift(1).fillna(close.iloc[0]),
        "High": close * 1.012, "Low": close * 0.988, "Close": close,
        "Volume": pd.Series(1000.0 + (np.arange(n) % 11) * 150.0),
    })


def _last_non_none(values):
    for v in reversed(values):
        if v is not None:
            return v
    return None


def test_vocab_helper_produces_all_keys():
    df = _make_df()
    out = _expanded_vocab_indicators(df["High"], df["Low"], df["Close"], df["Volume"])
    for k in NUMERIC + ["vwap_dist_pct", "ttm_squeeze"]:
        assert k in out, f"helper missing {k}"
    assert isinstance(out["ttm_squeeze"], bool)


def test_vocab_helper_matches_canonical():
    df = _make_df()
    out = _expanded_vocab_indicators(df["High"], df["Low"], df["Close"], df["Volume"])
    canon = compute_all_from_df(df)
    for k in NUMERIC:
        canon_last = _last_non_none(canon[k])
        if out[k] is None or canon_last is None:
            continue
        assert abs(out[k] - canon_last) <= 1e-6 * max(1.0, abs(canon_last)), \
            f"{k}: helper={out[k]} canon={canon_last}"
    assert bool(out["ttm_squeeze"]) == bool(_last_non_none(canon["ttm_squeeze"]))


def test_vocab_helper_safe_on_garbage():
    # Bad input -> empty dict, never raises (must not break scoring).
    out = _expanded_vocab_indicators(None, None, None, None)
    assert out == {}
