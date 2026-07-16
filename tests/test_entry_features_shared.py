"""Shared entry-feature capture (entry_features.capture_entry_features) — the parity-
identical replay/live vector for the winner/loser discriminator dataset (2026-06-23).

Proves: correct geometry/structure fields, and DUAL-PATH PARITY — identical inputs through
the live arg-shape vs the replay arg-shape yield byte-identical SHARED fields (the
load-bearing dual-path guarantee; minute_vol is replay-only/lookahead and excluded).
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_features as entry_features_module
from app.services.trading.momentum_neural.entry_features import (
    capture_entry_features,
    macro_feature_cache,
    macro_regime_features,
)
from app.services.trading.momentum_neural.replay_errors import (
    ReplayOhlcvInputUnavailableError,
)


def _df(n: int = 20) -> pd.DataFrame:
    idx = pd.date_range("2026-06-22 13:30", periods=n, freq="1min", tz="UTC")
    base = [1.40 + 0.01 * i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": base,
            "High": [b + 0.01 for b in base],
            "Low": [b - 0.01 for b in base],
            "Close": base,
            "Volume": [1000 + 50 * i for i in range(n)],
        },
        index=idx,
    )


_DBG = {"vol_ratio": 3.0, "sustained_rvol": 2.0, "vwap": 1.50, "pullback_ordinal": 1, "back_side": False}
_COMMON = dict(
    fill_px=1.60, stop=1.55, target=1.70, qty=100.0, want_qty=120.0,
    spread_bps=20.0, atr_pct=0.02, stop_atr_pct_eff=0.031, mid=1.595,
    liq_mult=1.0, fire_ts="2026-06-22 14:05:00", entry_fidelity="live",
)


def test_basic_output():
    f = capture_entry_features("AAA", **_COMMON, dollar_vol=1e6, trigger_debug=_DBG,
                               session_df=_df(), l2_db=None, l2_as_of=None)
    assert f is not None
    assert f["spread_bps"] == 20.0 and f["atr_pct"] == 0.02 and f["stop_pct_eff"] == 0.031
    assert abs(f["rr"] - ((1.70 - 1.60) / (1.60 - 1.55))) < 1e-9          # = 2.0
    assert abs(f["partial"] - (100.0 / 120.0)) < 1e-9
    assert f["premarket"] == 0.0                                          # 14:05 UTC >= 13:30 open
    assert f["vol_ratio"] == 3.0 and f["sustained_rvol"] == 2.0
    assert "front_side_score" in f                                       # df has bars+range+vol


def test_dual_shape_parity():
    df = _df()
    live = capture_entry_features("AAA", **_COMMON, dollar_vol=1e6, trigger_debug=_DBG,
                                  session_df=df, df_cols=("High", "Low", "Close", "Volume"),
                                  minute_vol=None, l2_db=None, l2_as_of=None)
    replay = capture_entry_features("AAA", **_COMMON, dollar_vol=1e6, trigger_debug=_DBG,
                                    session_df=df, df_cols=("High", "Low", "Close", "Volume"),
                                    minute_vol=500.0, l2_db=None, l2_as_of=None)
    shared = (set(live) & set(replay)) - {"minute_vol"}
    for k in shared:
        assert live[k] == replay[k], (k, live[k], replay[k])
    assert "minute_vol" in replay and "minute_vol" not in live           # the only path-different field


def test_fail_open_no_df():
    f = capture_entry_features("AAA", **_COMMON, dollar_vol=None, trigger_debug=None,
                               session_df=None, l2_db=None, l2_as_of=None)
    assert f is not None                                                 # geometry still returned
    assert "front_side_score" not in f                                   # no df -> no structure, no crash
    assert "dollar_vol" not in f                                         # None -> omitted
    assert "minute_vol" not in f


def test_macro_merged_into_features():
    # macro-regime features (computed by the caller, passed in) merge into the vector
    f = capture_entry_features("AAA", **_COMMON, dollar_vol=1e6, trigger_debug=_DBG,
                               session_df=_df(), l2_db=None, l2_as_of=None,
                               macro={"spy_trend": 1.0, "bear_x_vol": 0.3, "mkt_vol": 0.25})
    assert f["spy_trend"] == 1.0 and f["bear_x_vol"] == 0.3 and f["mkt_vol"] == 0.25


def test_noncanonical_cols_renamed():
    # replay-style lowercase columns must rename to canonical for front_side_state
    df = _df().rename(columns={"High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    f = capture_entry_features("AAA", **_COMMON, dollar_vol=1e6, trigger_debug=_DBG,
                               session_df=df, df_cols=("high", "low", "close", "volume"),
                               l2_db=None, l2_as_of=None)
    assert f is not None and "front_side_score" in f


def test_macro_runtime_cache_is_local_deterministic_and_reuses_exact_frames():
    calls: list[tuple[str, str, str]] = []

    def fetcher(symbol: str, *, interval: str, period: str) -> pd.DataFrame:
        calls.append((symbol, interval, period))
        count = 21 if symbol in {"SPY", "IWM"} else 1
        close = 20.0 if symbol == "^VIX" else (22.0 if symbol == "^VIX3M" else 100.0)
        values = [close + index for index in range(count)]
        return pd.DataFrame(
            {
                "Open": values,
                "High": [value + 1.0 for value in values],
                "Low": [value - 1.0 for value in values],
                "Close": values,
                "Volume": [1_000.0] * count,
            },
            index=pd.date_range("2026-06-01", periods=count, freq="1d", tz="UTC"),
        )

    entry_features_module._MACRO_CACHE["SPY"] = (9_999_999_999.0, [1.0] * 21)
    local_cache: dict = {}
    try:
        with macro_feature_cache(local_cache):
            first = macro_regime_features(now_ts=1_782_000_000.0, fetcher=fetcher)
            second = macro_regime_features(now_ts=1_782_000_100.0, fetcher=fetcher)
        assert first == second
        assert [call[0] for call in calls] == ["SPY", "IWM", "^VIX", "^VIX3M"]
        assert set(local_cache) == {"SPY", "IWM", "VIXSLOPE"}
        assert first["vix_slope"] == pytest.approx(22.0 / 20.0)
    finally:
        entry_features_module._MACRO_CACHE.pop("SPY", None)


def test_macro_input_contract_failure_is_never_median_imputed_or_swallowed():
    def rejected_fetcher(*_args, **_kwargs):
        raise ReplayOhlcvInputUnavailableError("fixture_missing_exact_receipt")

    with macro_feature_cache({}), pytest.raises(
        ReplayOhlcvInputUnavailableError,
        match="fixture_missing_exact_receipt",
    ):
        macro_regime_features(now_ts=1_782_000_000.0, fetcher=rejected_fetcher)
