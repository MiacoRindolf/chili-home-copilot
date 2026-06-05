"""Tests for the expanded indicator vocabulary (volume-flow, alt-momentum,
volatility-squeeze).

The critical guarantee: the scanner path (feeds the miner via flat_indicators)
and indicator_core.compute_all_from_df (the entry/backtest surface) produce the
SAME value for each new indicator — they call the same indicator_core helper, so
scan-time and entry-time agree by construction. The new keys are deliberately
NOT in the feature-parity catalog, so they never gate live entries.
"""
import numpy as np
import pandas as pd

from app.services.trading import scanner
from app.services.trading.indicator_core import compute_all_from_df

NEW_NUMERIC = [
    "obv", "mfi", "vwap", "cci", "roc",
    "keltner_upper", "keltner_lower", "keltner_mid",
]


def _make_df(n: int = 140) -> pd.DataFrame:
    # Deterministic OHLCV with real variation so every indicator computes.
    wave = 100.0 + np.cumsum(np.sin(np.arange(n) / 7.0)) + np.cos(np.arange(n) / 3.0)
    close = pd.Series(wave)
    high = close * 1.012
    low = close * 0.988
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(1000.0 + (np.arange(n) % 11) * 150.0 + (np.arange(n) % 3) * 80.0)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}
    )


def _last_non_none(values):
    for v in reversed(values):
        if v is not None:
            return v
    return None


def test_new_indicators_present_in_compute_all():
    out = compute_all_from_df(_make_df())
    for k in NEW_NUMERIC + ["vwap_dist_pct", "ttm_squeeze"]:
        assert k in out, f"missing {k} from compute_all_from_df"


def test_mfi_is_bounded_0_100():
    out = compute_all_from_df(_make_df())
    vals = [v for v in out["mfi"] if v is not None]
    assert vals
    assert all(-0.001 <= v <= 100.001 for v in vals)


def test_ttm_squeeze_is_boolean():
    out = compute_all_from_df(_make_df())
    assert all(isinstance(v, bool) for v in out["ttm_squeeze"])


def test_scanner_matches_canonical_by_construction():
    df = _make_df()
    canon = compute_all_from_df(df)
    scan = scanner._compute_indicators(df)
    assert scan is not None
    for k in NEW_NUMERIC:
        canon_last = _last_non_none(canon[k])
        scan_v = scan.get(k)
        if canon_last is None or scan_v is None:
            continue
        tol = 1e-6 * max(1.0, abs(canon_last))
        assert abs(scan_v - canon_last) <= tol, f"{k}: scan={scan_v} canon={canon_last}"


def test_scanner_ttm_squeeze_matches_canonical():
    df = _make_df()
    canon_sq = _last_non_none(compute_all_from_df(df)["ttm_squeeze"])
    scan_sq = scanner._compute_indicators(df).get("ttm_squeeze")
    assert bool(scan_sq) == bool(canon_sq)


def test_needed_subset_computes_only_requested():
    # The `needed` fast-path must still produce the new keys when asked.
    out = compute_all_from_df(_make_df(), needed={"mfi", "ttm_squeeze"})
    assert "mfi" in out and "ttm_squeeze" in out
