"""CPCV + DSR + PBO promotion gate (Q1.T1, extended Q1.T1.6).

**HR1 (kill switch):** When ``CHILI_CPCV_PROMOTION_GATE_ENABLED`` is True, no caller may
treat a pattern as promotion-ready without running :func:`finalize_promotion_with_cpcv`
from :func:`check_promotion_ready` / :func:`check_promotion_ready_v2` in
``mining_validation`` (the only intended call sites). Do not add a parallel promotion
path that bypasses that funnel.

**Q1.T1.6 — Evaluator routing:** ``scan_patterns.pattern_evidence_kind`` chooses
**realized_pnl** (default: CPCV on realized trade returns, no classifier) vs **ml_signal**
(legacy triple-barrier + LightGBM below).

**Model / label contract (``ml_signal`` only; locked for Q2.T6 meta-classifier reuse):**
  * **Classifier:** ``lightgbm.LGBMClassifier`` with
    ``n_estimators=200``, ``num_leaves=31``, ``min_data_in_leaf=100``,
    ``learning_rate=0.02``, ``objective="multiclass"``, ``num_class=3``,
    ``random_state=42``, ``verbose=-1``.
  * **X:** One row per mined / trade-analytics sample. Fixed feature order
    :data:`CPCV_FEATURE_NAMES` — floats from the mining row (or PTR row + ``features_json``
    aliases). Booleans cast to 0.0 / 1.0.
  * **y:** Triple-barrier label from :func:`app.services.trading.triple_barrier.compute_label_atr`
    with ``atr_mult_tp=2``, ``atr_mult_sl=2``, ``side="long"``, ``max_bars`` =
    :func:`cpcv_vertical_max_bars` (60 trading days expressed in bar steps for the row's
    ``bar_interval``). Mapping to LightGBM class indices: ``{-1 -> 0, 0 -> 1, +1 -> 2}``.
    Rows with ``barrier_hit == "missing_data"`` are dropped.

Shadow mode (flag OFF): CPCV is evaluated only at promotion-attempt time (after prior
gates pass inside ``check_promotion_ready*``); metrics are logged and persisted but do
not block promotion.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Mapping

import numpy as np
import pandas as pd
from skfolio.model_selection import CombinatorialPurgedCV, optimal_folds_number

from ...config import settings
from .triple_barrier import compute_label_atr

logger = logging.getLogger(__name__)

# ── Locked feature vector (order-stable for Q2.T6) ─────────────────────────
CPCV_FEATURE_NAMES: tuple[str, ...] = (
    "rsi",
    "macd",
    "macd_sig",
    "macd_hist",
    "adx",
    "bb_pct",
    "stoch_k",
    "atr",
    "price",
    "bb_squeeze",
    "stoch_bull_div",
    "stoch_bear_div",
    "above_sma20",
)

LGBM_CPCV_PARAMS: dict[str, Any] = {
    "n_estimators": 200,
    "num_leaves": 31,
    "min_data_in_leaf": 100,
    "learning_rate": 0.02,
    "objective": "multiclass",
    "num_class": 3,
    "random_state": 42,
    "verbose": -1,
}


def bars_per_year(bar_interval: str) -> float:
    """US equity session approximation: 252 days × 6.5 hours / bar duration."""
    iv = (bar_interval or "1d").strip().lower()
    if iv in ("1d", "d", "day", "1day"):
        return 252.0
    if iv in ("4h", "240m"):
        return 252.0 * 6.5 / 4.0
    if iv in ("1h", "60m"):
        return 252.0 * 6.5
    if iv in ("30m",):
        return 252.0 * 6.5 * 2.0
    if iv in ("15m",):
        return 252.0 * 6.5 * 4.0
    if iv in ("5m",):
        return 252.0 * 6.5 * 12.0
    if iv in ("1m",):
        return 252.0 * 6.5 * 60.0
    if iv in ("90m",):
        return 252.0 * 6.5 * (60.0 / 90.0)
    return 252.0


def cpcv_vertical_max_bars(bar_interval: str) -> int:
    """60 trading days in units of *bar_interval* bars (min 5)."""
    bpy = bars_per_year(bar_interval)
    return max(5, int(round(60.0 * (bpy / 252.0))))


SCANNER_BUCKETS: tuple[str, ...] = ("swing", "day", "breakout", "momentum", "patterns")


def infer_scanner_bucket(pat: Any) -> str:
    """Coarse scanner lane for CPCV dry-run / shadow funnel (not a persisted FK).

    Order: momentum → breakout → day (intraday timeframe) → patterns (mined/builtin) → swing (daily default).
    """
    name = (getattr(pat, "name", None) or "").lower()
    tf = (getattr(pat, "timeframe", None) or "1d").strip().lower()
    hypo = (getattr(pat, "hypothesis_family", None) or "").lower()
    origin = (getattr(pat, "origin", None) or "").lower()

    if "momentum" in name or "momentum" in hypo:
        return "momentum"
    if any(
        x in name
        for x in (
            "breakout",
            "squeeze",
            "nr7",
            "narrow range",
            "vcp",
            "compression",
            "expansion",
        )
    ):
        return "breakout"
    if tf not in ("1d", "d", "day", "1day", ""):
        return "day"
    if origin in ("mined", "builtin"):
        return "patterns"
    return "swing"


def _flatten_test_indices(te: Any) -> np.ndarray:
    if isinstance(te, (list, tuple)):
        return np.concatenate([np.asarray(x, dtype=int) for x in te])
    return np.asarray(te, dtype=int)


def _row_to_float_features(row: Mapping[str, Any]) -> np.ndarray | None:
    vec: list[float] = []
    for name in CPCV_FEATURE_NAMES:
        v = row.get(name)
        if v is None:
            if name in ("bb_squeeze", "stoch_bull_div", "stoch_bear_div", "above_sma20"):
                v = False
            else:
                return None
        if isinstance(v, bool):
            vec.append(1.0 if v else 0.0)
        else:
            try:
                vec.append(float(v))
            except (TypeError, ValueError):
                return None
    return np.array(vec, dtype=float)


def normalize_mining_row_features(row: Mapping[str, Any]) -> np.ndarray | None:
    """Map a mining ``_mine_from_history`` row (or flat dict) to **X**."""
    return _row_to_float_features(row)


def normalize_ptr_row_features(
    *,
    outcome_return_pct: float | None,
    as_of_ts: datetime,
    ticker: str,
    timeframe: str,
    features_json: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a mining-shaped dict from :class:`~app.models.trading.PatternTradeRow` data."""
    fj = features_json or {}
    out: dict[str, Any] = {
        "ret_5d": float(outcome_return_pct or 0.0),
        "bar_start_utc": as_of_ts,
        "ticker": ticker,
        "bar_interval": (timeframe or "1d").strip() or "1d",
        "price": float(fj.get("entry_price") or 0.0),
        "rsi": float(fj.get("rsi_14") or fj.get("rsi") or 50.0),
        "macd": 0.0,
        "macd_sig": 0.0,
        "macd_hist": float(fj.get("macd_hist") or fj.get("macd_histogram") or 0.0),
        "adx": 0.0,
        "bb_pct": 0.5,
        "stoch_k": 50.0,
        "atr": 0.0,
        "bb_squeeze": False,
        "stoch_bull_div": False,
        "stoch_bear_div": False,
        "above_sma20": False,
    }
    for k in ("macd", "macd_sig", "adx", "bb_pct", "stoch_k", "atr"):
        if k in fj and fj[k] is not None:
            try:
                out[k] = float(fj[k])
            except (TypeError, ValueError):
                pass
    if "bb_squeeze" in fj:
        out["bb_squeeze"] = bool(fj["bb_squeeze"])
    if "above_sma20" in fj:
        out["above_sma20"] = bool(fj["above_sma20"])
    return out


def _entry_atr_from_df(df: pd.DataFrame, idx: int, window: int = 14) -> float:
    if idx < 1 or idx >= len(df):
        return 0.0
    hi = df["High"].astype(float)
    lo = df["Low"].astype(float)
    cl = df["Close"].astype(float)
    i0 = max(1, idx - window + 1)
    trs: list[float] = []
    for j in range(i0, idx + 1):
        h, l = float(hi.iloc[j]), float(lo.iloc[j])
        c_prev = float(cl.iloc[j - 1])
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return float(np.mean(trs)) if trs else 0.0


def _find_bar_index(df: pd.DataFrame, bar_start_utc: datetime) -> int | None:
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return None
    ts = pd.Timestamp(bar_start_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    # nearest past bar
    try:
        idx = int(df.index.searchsorted(ts, side="right") - 1)
    except Exception:
        return None
    if idx < 0:
        return None
    return idx


def _label_rows_barrier(
    rows: list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]] | None:
    """Return (X float matrix, y_lgb 0..2, barrier realized return fraction, regimes)."""
    from .market_data import fetch_ohlcv_df

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        tk = str(r.get("ticker") or "").strip().upper()
        iv = (r.get("bar_interval") or r.get("timeframe") or "1d").strip() or "1d"
        if not tk:
            continue
        groups.setdefault((tk, iv), []).append(r)

    xs: list[np.ndarray] = []
    ys: list[int] = []
    rets: list[float] = []
    regimes: list[str] = []

    for (tk, iv), g_rows in groups.items():
        g_rows = sorted(g_rows, key=lambda x: x.get("bar_start_utc") or datetime.min)
        if not g_rows:
            continue
        starts = [r.get("bar_start_utc") for r in g_rows if r.get("bar_start_utc")]
        if not starts:
            continue
        mn, mx = min(starts), max(starts)
        start_d = (mn.date() if hasattr(mn, "date") else mn) - timedelta(days=30)
        end_d = (mx.date() if hasattr(mx, "date") else mx) + timedelta(days=400)
        try:
            df = fetch_ohlcv_df(
                tk,
                interval=iv,
                start=start_d.isoformat(),
                end=end_d.isoformat(),
            )
        except Exception:
            df = pd.DataFrame()
        if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
            continue
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        max_v = cpcv_vertical_max_bars(iv)
        for r in g_rows:
            feats = normalize_mining_row_features(r)
            if feats is None:
                continue
            bst = r.get("bar_start_utc")
            if not isinstance(bst, datetime):
                continue
            idx = _find_bar_index(df, bst)
            if idx is None or idx >= len(df) - 2:
                continue
            future = df.iloc[idx + 1 : idx + 1 + max_v]
            if future.empty:
                continue
            entry_close = float(df["Close"].iloc[idx])
            if entry_close <= 0:
                continue
            atr_v = float(r.get("atr") or 0.0)
            if atr_v <= 0:
                atr_v = _entry_atr_from_df(df, idx)
            if atr_v <= 0:
                continue
            bars = [future.iloc[i] for i in range(len(future))]
            label = compute_label_atr(
                entry_close,
                atr_v,
                bars,
                atr_mult_tp=2.0,
                atr_mult_sl=2.0,
                max_bars=max_v,
                side="long",
            )
            if label.barrier_hit == "missing_data":
                continue
            y_tb = int(label.label)
            if y_tb not in (-1, 0, 1):
                continue
            xs.append(feats)
            ys.append(y_tb + 1)
            rets.append(float(label.realized_return_pct))
            regimes.append(str(r.get("regime") or "unknown"))

    min_lb = int(getattr(settings, "chili_cpcv_min_trades", 15))
    if len(xs) < min_lb:
        return None
    return np.vstack(xs), np.array(ys, dtype=int), np.array(rets, dtype=float), regimes


def _lgbm_classifier():
    import lightgbm as lgb

    return lgb.LGBMClassifier(**LGBM_CPCV_PARAMS)


def _sharpe_annualized(returns: np.ndarray, periods_per_year: float) -> float:
    if returns.size < 2:
        return 0.0
    m = float(np.mean(returns))
    s = float(np.std(returns, ddof=1))
    if s < 1e-12:
        return 0.0
    return m / s * math.sqrt(periods_per_year)


def _regime_median_sharpes_from_path(
    path_returns: np.ndarray,
    regime_labels: list[str],
    periods_per_year: float,
) -> dict[str, float]:
    """One path: median of per-regime OOS Sharpe (each regime needs >=2 bars)."""
    out: dict[str, float] = {}
    for reg in sorted(set(regime_labels)):
        mask = np.array([r == reg for r in regime_labels], dtype=bool)
        if mask.sum() < 2:
            continue
        out[reg] = _sharpe_annualized(path_returns[mask], periods_per_year)
    return out


def _cpcv_autoscaled_purge_embargo(n: int, purge_frac: float, embargo_frac: float) -> tuple[int, int]:
    pf = float(max(0.0, min(0.5, purge_frac)))
    ef = float(max(0.0, min(0.5, embargo_frac)))
    return max(2, int(pf * n)), max(1, int(ef * n))


def _cpcv_build_feasible_cv_and_splits(
    X: np.ndarray,
    n_folds: int,
    n_test_folds: int,
    purged_size: int,
    embargo_size: int,
) -> tuple[CombinatorialPurgedCV | None, int, int, list | None]:
    """Shrink purge/embargo until each train fold can absorb purge+embargo (López de Prado)."""
    ps, es = purged_size, embargo_size
    cap = max(200, X.shape[0] + 50)
    for _ in range(cap):
        try:
            cv = CombinatorialPurgedCV(
                n_folds=n_folds,
                n_test_folds=n_test_folds,
                purged_size=ps,
                embargo_size=es,
            )
            splits = list(cv.split(X))
        except Exception:
            splits = []
        if splits:
            min_tr = min(np.asarray(tr, dtype=int).size for tr, _te in splits)
            if min_tr > ps + es:
                return cv, ps, es, splits
        if ps > 2:
            ps -= 1
        elif es > 1:
            es -= 1
        else:
            break
    return None, purged_size, embargo_size, None


def _bar_start_ts(row: Mapping[str, Any]) -> float:
    b = row.get("bar_start_utc")
    if isinstance(b, datetime):
        return b.timestamp()
    if hasattr(b, "timestamp"):
        try:
            return float(b.timestamp())  # type: ignore[no-any-return]
        except Exception:
            pass
    return 0.0


def filtered_rows_to_realized_series(
    filtered: list[dict[str, Any]],
) -> tuple[list[float], list[datetime] | None]:
    """Time-order rows and extract realized returns + timestamps for T1.6 CPCV."""
    ordered = sorted(filtered, key=_bar_start_ts)
    rets: list[float] = []
    ts: list[datetime] = []
    for r in ordered:
        b = r.get("bar_start_utc")
        if not isinstance(b, datetime):
            continue
        rets.append(float(r.get("ret_5d") or 0))
        ts.append(b)
    if len(rets) < len(ordered):
        return [float(r.get("ret_5d") or 0) for r in ordered], None
    return rets, ts if len(ts) == len(rets) else None


def trade_sequence_annualization(
    n: int,
    trade_timestamps: list[datetime] | None,
    bar_interval_fallback: str,
) -> float:
    """Effective periods-per-year for trade-level returns (span from first to last trade)."""
    if n >= 2 and trade_timestamps and len(trade_timestamps) == n:
        ts_sorted = sorted(trade_timestamps)
        t0, t1 = ts_sorted[0], ts_sorted[-1]
        span_sec = float(t1.timestamp()) - float(t0.timestamp())
        years = max(span_sec / (365.25 * 24 * 3600), 1.0 / 252.0)
        eff = float(n) / years
        return float(min(252.0, max(1.0, eff)))
    return bars_per_year((bar_interval_fallback or "1d").strip() or "1d")


def evaluate_pattern_cpcv_realized_pnl(
    pattern_id: int | None,
    trade_returns: list[float] | np.ndarray,
    trade_timestamps: list[datetime] | None,
    *,
    n_hypotheses_tested: int,
    n_target_paths: int | None = None,
    purged_size: int | None = None,
    embargo_size: int | None = None,
    bar_interval_hint: str | None = None,
    max_labeled_rows: int | None = None,
) -> dict[str, Any]:
    """CPCV on the pattern's realized trade PnL sequence (no classifier, no triple-barrier).

    Each test fold's OOS metric is the annualized Sharpe of realized ``outcome_return_pct``
    values in that fold. DSR and PBO use the same return stream with
    :func:`app.services.trading.mining_validation.compute_deflated_sharpe_ratio` /
    :func:`app.services.trading.mining_validation.compute_pbo` (two-column CSCV: realized vs
    time-shifted copy).
    """
    pid_for_seed = int(pattern_id) if pattern_id is not None else 0
    rets = np.asarray(trade_returns, dtype=float).ravel()
    n = int(rets.size)
    if n < 1:
        return {
            "skipped": True,
            "reason": "empty_returns",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
            "evaluator": "realized_pnl",
        }

    ts_list = trade_timestamps
    if ts_list is not None and len(ts_list) != n:
        ts_list = None

    cap = int(max_labeled_rows) if max_labeled_rows is not None else int(
        getattr(settings, "chili_cpcv_max_labeled_rows", 0) or 0
    )
    if cap > 0 and n > cap:
        rng = np.random.default_rng(42 + pid_for_seed)
        pick = rng.choice(n, size=cap, replace=False)
        pick.sort()
        rets = rets[pick]
        if ts_list is not None:
            ts_list = [ts_list[int(i)] for i in pick.tolist()]
        n = int(cap)

    min_tr = int(getattr(settings, "chili_cpcv_min_trades", 15))
    if n < min_tr:
        return {
            "skipped": True,
            "reason": "insufficient_trades",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
            "evaluator": "realized_pnl",
        }

    periods_py = trade_sequence_annualization(n, ts_list, bar_interval_hint or "1d")

    purge_frac = float(getattr(settings, "chili_cpcv_purge_frac", 0.05))
    embargo_frac = float(getattr(settings, "chili_cpcv_embargo_frac", 0.02))
    target_cap = int(getattr(settings, "chili_cpcv_target_paths_max", 100))
    if purged_size is not None and embargo_size is not None:
        ps, es = max(2, int(purged_size)), max(1, int(embargo_size))
    else:
        ps, es = _cpcv_autoscaled_purge_embargo(n, purge_frac, embargo_frac)

    if n_target_paths is None:
        n_paths_budget = min(target_cap, max(10, n // 5))
    else:
        n_paths_budget = int(n_target_paths)

    X = np.arange(n, dtype=float).reshape(-1, 1)
    try:
        t_train = min(252, max(min_tr, n // 2))
        n_folds, n_test_folds = optimal_folds_number(
            n_observations=n,
            target_train_size=t_train,
            target_n_test_paths=min(n_paths_budget, max(2, n // 15)),
        )
        n_folds = max(5, min(n_folds, max(5, n // 10)))
        n_test_folds = max(2, min(n_test_folds, n_folds - 2))
        cv, ps, es, splits = _cpcv_build_feasible_cv_and_splits(
            X, n_folds, n_test_folds, ps, es
        )
        if cv is None or not splits:
            return {
                "skipped": True,
                "reason": "cv_infeasible_for_sample_size",
                "cpcv_n_paths": 0,
                "cpcv_median_sharpe": None,
                "cpcv_median_sharpe_by_regime": None,
                "deflated_sharpe": None,
                "pbo": None,
                "n_effective_trials": int(max(1, n_hypotheses_tested)),
                "evaluator": "realized_pnl",
            }
    except Exception as exc:
        return {
            "skipped": True,
            "reason": f"cv_config:{exc}",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
            "evaluator": "realized_pnl",
        }

    sharpes: list[float] = []
    min_train_floor = max(10, min_tr)
    min_test_floor = max(3, min(5, n // 25))

    try:
        for train_idx, test_idx in splits:
            te = _flatten_test_indices(test_idx)
            tr = np.asarray(train_idx, dtype=int)
            if tr.size < min_train_floor or te.size < min_test_floor:
                continue
            oos = rets[te]
            sharpes.append(_sharpe_annualized(oos, periods_py))
    except Exception as exc:
        return {
            "skipped": True,
            "reason": f"cv_loop:{exc}",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
            "evaluator": "realized_pnl",
        }

    if len(sharpes) < 1:
        return {
            "skipped": True,
            "reason": "no_cv_paths",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
            "evaluator": "realized_pnl",
        }

    from .mining_validation import compute_deflated_sharpe_ratio, compute_pbo

    n_eff = int(max(1, n_hypotheses_tested))
    dsr_pack = compute_deflated_sharpe_ratio(
        rets.tolist(),
        n_trials=n_eff,
        annualization=periods_py,
    )
    dsr_val = dsr_pack.get("dsr")

    pbo_val = None
    r_alt = np.roll(rets, max(1, n // 4))
    mat = np.column_stack([rets, r_alt])
    for npart in (8, 6, 4):
        if n < 2 * npart:
            continue
        try:
            pbo_pack = compute_pbo(
                mat, n_partitions=npart, n_combos=100, rng_seed=42 + pid_for_seed
            )
            pbo_val = pbo_pack.get("pbo")
            if pbo_val is not None:
                break
        except Exception:
            continue

    return {
        "skipped": False,
        "cpcv_n_paths": len(sharpes),
        "cpcv_median_sharpe": float(np.median(sharpes)),
        "cpcv_median_sharpe_by_regime": None,
        "deflated_sharpe": dsr_val,
        "pbo": pbo_val,
        "n_effective_trials": n_eff,
        "deflated_sharpe_detail": dsr_pack,
        "n_labeled_samples": int(n),
        "n_trades": int(n),
        "evaluator": "realized_pnl",
    }


def evaluate_pattern_cpcv(
    pattern_id: int | None,
    filtered: list[dict[str, Any]],
    *,
    n_hypotheses_tested: int,
    n_target_paths: int | None = None,
    purged_size: int | None = None,
    embargo_size: int | None = None,
    bar_interval_hint: str | None = None,
    max_labeled_rows: int | None = None,
) -> dict[str, Any]:
    """Run Combinatorial Purged CV with LightGBM; compute DSR, PBO, path Sharpes."""
    pid_for_seed = int(pattern_id) if pattern_id is not None else 0

    base_iv = (
        bar_interval_hint
        or (filtered[0].get("bar_interval") if filtered else None)
        or (filtered[0].get("timeframe") if filtered else None)
        or "1d"
    )
    periods_py = bars_per_year(str(base_iv))

    prep = _label_rows_barrier(filtered)
    if prep is None:
        return {
            "skipped": True,
            "reason": "insufficient_labeled_rows",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
        }

    X, y_lgb, barrier_rets, regimes = prep
    n = X.shape[0]

    cap = int(max_labeled_rows) if max_labeled_rows is not None else int(
        getattr(settings, "chili_cpcv_max_labeled_rows", 0) or 0
    )
    if cap > 0 and n > cap:
        logger.info(
            "[cpcv_promotion_gate] subsampling labeled rows %s -> %s for CPCV (cap)",
            n,
            cap,
        )
        rng = np.random.default_rng(42 + pid_for_seed)
        pick = rng.choice(n, size=cap, replace=False)
        pick.sort()
        X = X[pick]
        y_lgb = y_lgb[pick]
        barrier_rets = barrier_rets[pick]
        regimes = [regimes[int(i)] for i in pick.tolist()]
        n = int(cap)

    min_tr = int(getattr(settings, "chili_cpcv_min_trades", 15))
    if n < min_tr:
        return {
            "skipped": True,
            "reason": "insufficient_rows_after_barrier",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
        }

    purge_frac = float(getattr(settings, "chili_cpcv_purge_frac", 0.05))
    embargo_frac = float(getattr(settings, "chili_cpcv_embargo_frac", 0.02))
    target_cap = int(getattr(settings, "chili_cpcv_target_paths_max", 100))
    if purged_size is not None and embargo_size is not None:
        ps, es = max(2, int(purged_size)), max(1, int(embargo_size))
    else:
        ps, es = _cpcv_autoscaled_purge_embargo(n, purge_frac, embargo_frac)

    if n_target_paths is None:
        n_paths_budget = min(target_cap, max(10, n // 5))
    else:
        n_paths_budget = int(n_target_paths)

    try:
        t_train = min(252, max(min_tr, n // 2))
        n_folds, n_test_folds = optimal_folds_number(
            n_observations=n,
            target_train_size=t_train,
            target_n_test_paths=min(n_paths_budget, max(2, n // 15)),
        )
        n_folds = max(5, min(n_folds, max(5, n // 10)))
        n_test_folds = max(2, min(n_test_folds, n_folds - 2))
        cv, ps, es, splits = _cpcv_build_feasible_cv_and_splits(
            X, n_folds, n_test_folds, ps, es
        )
        if cv is None or not splits:
            logger.info(
                "[cpcv_promotion_gate] CV infeasible: n=%s folds=%s/%s purge=%s embargo=%s",
                n,
                n_folds,
                n_test_folds,
                ps,
                es,
            )
            return {
                "skipped": True,
                "reason": "cv_infeasible_for_sample_size",
                "cpcv_n_paths": 0,
                "cpcv_median_sharpe": None,
                "cpcv_median_sharpe_by_regime": None,
                "deflated_sharpe": None,
                "pbo": None,
                "n_effective_trials": int(max(1, n_hypotheses_tested)),
            }
    except Exception as exc:
        logger.info("[cpcv_promotion_gate] CV config failed: %s", exc)
        return {
            "skipped": True,
            "reason": f"cv_config:{exc}",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
        }

    sharpes: list[float] = []
    path_regime_medians: dict[str, list[float]] = {}
    clf_template = _lgbm_classifier()

    min_train_floor = max(10, min_tr)
    min_test_floor = max(3, min(5, n // 25))

    try:
        for train_idx, test_idx in splits:
            te = _flatten_test_indices(test_idx)
            tr = np.asarray(train_idx, dtype=int)
            if tr.size < min_train_floor or te.size < min_test_floor:
                continue
            clf = _lgbm_classifier()
            clf.fit(X[tr], y_lgb[tr])
            pred = clf.predict(X[te])
            pos = pred.astype(int) - 1  # back to {-1,0,1}
            strat = barrier_rets[te] * pos
            sharpes.append(_sharpe_annualized(strat, periods_py))
            reg_slice = [regimes[i] for i in te.tolist()]
            per_reg = _regime_median_sharpes_from_path(strat, reg_slice, periods_py)
            for rk, rv in per_reg.items():
                path_regime_medians.setdefault(rk, []).append(rv)
    except Exception as exc:
        logger.info("[cpcv_promotion_gate] CV loop failed: %s", exc)
        return {
            "skipped": True,
            "reason": f"cv_loop:{exc}",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
        }

    if len(sharpes) < 1:
        return {
            "skipped": True,
            "reason": "no_cv_paths",
            "cpcv_n_paths": 0,
            "cpcv_median_sharpe": None,
            "cpcv_median_sharpe_by_regime": None,
            "deflated_sharpe": None,
            "pbo": None,
            "n_effective_trials": int(max(1, n_hypotheses_tested)),
        }

    cpcv_n_paths = len(sharpes)
    cpcv_median_sharpe = float(np.median(sharpes))

    by_reg_med: dict[str, float] = {}
    try:
        by_reg_med = {
            k: float(np.median(v)) for k, v in path_regime_medians.items() if v
        }
    except Exception:
        by_reg_med = {}

    from .mining_validation import compute_deflated_sharpe_ratio, compute_pbo

    n_eff = int(max(1, n_hypotheses_tested))
    dsr_pack = compute_deflated_sharpe_ratio(
        barrier_rets.tolist(),
        n_trials=n_eff,
        annualization=periods_py,
    )
    dsr_val = dsr_pack.get("dsr")

    pbo_val = None
    try:
        pos_full = clf_template.fit(X, y_lgb).predict(X).astype(int) - 1
        strat_a = barrier_rets * pos_full
        strat_b = barrier_rets * 1.0
        mat = np.column_stack([strat_a, strat_b])
        pbo_pack = compute_pbo(mat, n_partitions=min(8, max(4, n // 20)), n_combos=100, rng_seed=42)
        pbo_val = pbo_pack.get("pbo")
    except Exception:
        pbo_val = None

    return {
        "skipped": False,
        "cpcv_n_paths": cpcv_n_paths,
        "cpcv_median_sharpe": cpcv_median_sharpe,
        "cpcv_median_sharpe_by_regime": by_reg_med or None,
        "deflated_sharpe": dsr_val,
        "pbo": pbo_val,
        "n_effective_trials": n_eff,
        "deflated_sharpe_detail": dsr_pack,
        "n_labeled_samples": int(n),
        "n_trades": int(n),
        "evaluator": "ml_signal",
    }


def promotion_gate_passes(metrics: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return (ok, reasons) against Q1.T1 thresholds.

    When ``ok`` is True and ``n_trades`` is below full-confidence minimum (30 by default),
    reasons include ``provisional_sample_size`` (wider CIs; see runbook).
    """
    failures: list[str] = []
    if metrics.get("skipped"):
        failures.append(str(metrics.get("reason") or "skipped"))
        return False, failures

    n_tr = int(metrics.get("n_trades") or metrics.get("n_labeled_samples") or 0)
    min_tr = int(getattr(settings, "chili_cpcv_min_trades", 15))
    full_conf = int(getattr(settings, "chili_cpcv_full_confidence_min_trades", 30))
    if n_tr < min_tr:
        failures.append("n_trades_below_min")

    dsr = metrics.get("deflated_sharpe")
    if dsr is None:
        failures.append("dsr_missing")
    elif float(dsr) < 0.95:
        failures.append("dsr_below_0_95")

    pbo = metrics.get("pbo")
    if pbo is None:
        failures.append("pbo_missing")
    elif float(pbo) > 0.2:
        failures.append("pbo_above_0_2")

    n_paths = int(metrics.get("cpcv_n_paths") or 0)
    if n_paths < 50:
        failures.append("cpcv_n_paths_lt_50")

    med_sh = metrics.get("cpcv_median_sharpe")
    if med_sh is None:
        failures.append("median_sharpe_missing")
    elif float(med_sh) < 0.5:
        failures.append("median_sharpe_below_0_5")

    ok = len(failures) == 0
    reasons = list(failures)
    if ok and n_tr < full_conf:
        reasons.append("provisional_sample_size")
    return ok, reasons


def finalize_promotion_with_cpcv(
    detail: dict[str, Any],
    filtered: list[dict[str, Any]],
    *,
    n_hypotheses_tested: int,
    scan_pattern: Any | None = None,
) -> dict[str, Any]:
    """Run CPCV evaluation and merge into *detail*; may set ``detail["blocked"]``.

    Call only from ``check_promotion_ready`` / ``check_promotion_ready_v2`` after
    prior gates pass (immediately before setting ready=True).

    ``scan_pattern.pattern_evidence_kind`` selects **realized_pnl** (trade-return CPCV,
    default) vs **ml_signal** (triple-barrier + LightGBM). When *scan_pattern* is
    omitted, **realized_pnl** is used (mining rows without a persisted pattern row).
    """
    enforce = bool(getattr(settings, "chili_cpcv_promotion_gate_enabled", False))
    kind = "realized_pnl"
    if scan_pattern is not None:
        raw_k = getattr(scan_pattern, "pattern_evidence_kind", None) or "realized_pnl"
        kind = str(raw_k).strip().lower()
    if kind not in ("realized_pnl", "ml_signal"):
        kind = "realized_pnl"

    cap = int(getattr(settings, "chili_cpcv_max_labeled_rows", 0) or 0)
    cap_kw: dict[str, Any] = {}
    if cap > 0:
        cap_kw["max_labeled_rows"] = cap

    if kind == "ml_signal":
        eval_payload = evaluate_pattern_cpcv(
            getattr(scan_pattern, "id", None),
            filtered,
            n_hypotheses_tested=n_hypotheses_tested,
            **cap_kw,
        )
    else:
        rets, ts = filtered_rows_to_realized_series(filtered)
        eval_payload = evaluate_pattern_cpcv_realized_pnl(
            getattr(scan_pattern, "id", None) if scan_pattern else None,
            rets,
            ts,
            n_hypotheses_tested=n_hypotheses_tested,
            bar_interval_hint=(
                getattr(scan_pattern, "timeframe", None) if scan_pattern else None
            ),
            **cap_kw,
        )
    ok_metrics, reasons = promotion_gate_passes(eval_payload)
    eval_payload["promotion_gate_passed"] = ok_metrics
    eval_payload["promotion_gate_reasons"] = reasons
    detail["cpcv_promotion_gate"] = eval_payload

    if eval_payload.get("skipped"):
        logger.info(
            "[cpcv_promotion_gate] enforced=%s skipped=%s reason=%s",
            enforce,
            True,
            eval_payload.get("reason"),
        )
        if enforce:
            detail["blocked"] = "cpcv_promotion_gate_failed"
            detail["cpcv_gate_reasons"] = [str(eval_payload.get("reason") or "skipped")]
        return detail

    logger.info(
        "[cpcv_promotion_gate] enforced=%s pass=%s dsr=%s pbo=%s paths=%s med_sh=%s reasons=%s",
        enforce,
        ok_metrics,
        eval_payload.get("deflated_sharpe"),
        eval_payload.get("pbo"),
        eval_payload.get("cpcv_n_paths"),
        eval_payload.get("cpcv_median_sharpe"),
        reasons,
    )

    if enforce and not ok_metrics:
        detail["blocked"] = "cpcv_promotion_gate_failed"
        detail["cpcv_gate_reasons"] = reasons

    return detail


def cpcv_eval_to_scan_pattern_fields(eval_payload: Mapping[str, Any]) -> dict[str, Any]:
    """ORM patch fragment for :class:`~app.models.trading.ScanPattern` CPCV columns."""
    out: dict[str, Any] = {}
    if not eval_payload or eval_payload.get("skipped"):
        return out
    out["cpcv_n_paths"] = eval_payload.get("cpcv_n_paths")
    out["cpcv_median_sharpe"] = eval_payload.get("cpcv_median_sharpe")
    out["cpcv_median_sharpe_by_regime"] = eval_payload.get("cpcv_median_sharpe_by_regime")
    out["deflated_sharpe"] = eval_payload.get("deflated_sharpe")
    out["pbo"] = eval_payload.get("pbo")
    out["n_effective_trials"] = eval_payload.get("n_effective_trials")
    out["promotion_gate_passed"] = bool(eval_payload.get("promotion_gate_passed"))
    out["promotion_gate_reasons"] = eval_payload.get("promotion_gate_reasons") or []
    # Invariant (pairs with migration 166): no gate outcome without CPCV path evidence.
    if "promotion_gate_passed" in out and out.get("cpcv_n_paths") is None:
        raise AssertionError(
            "promotion_gate_passed without cpcv_n_paths — CPCV evidence required for a gate outcome"
        )
    return out


def persist_cpcv_shadow_eval(db: Any, scan_pattern: Any, eval_payload: Mapping[str, Any]) -> None:
    """Append one CPCV evaluation row for :obj:`cpcv_shadow_funnel_v` (7d brain panel).

    No-op on empty payload or missing ``scan_pattern.id``. Swallows DB errors (e.g. migration
    not applied) so promotion never fails on telemetry.
    """
    if not eval_payload:
        return
    sid = getattr(scan_pattern, "id", None)
    if sid is None:
        return
    try:
        from sqlalchemy import text

        skipped = bool(eval_payload.get("skipped"))
        would_pass = bool(eval_payload.get("promotion_gate_passed"))
        scanner = infer_scanner_bucket(scan_pattern)
        pname = (getattr(scan_pattern, "name", None) or "")[:500]
        db.execute(
            text(
                """
                INSERT INTO cpcv_shadow_eval_log (
                    scan_pattern_id, scanner, would_pass_cpcv, passed_prior_gates,
                    deflated_sharpe, pbo, cpcv_n_paths, pattern_name, skipped
                ) VALUES (
                    :sid, :scanner, :wp, TRUE, :dsr, :pbo, :paths, :pname, :skipped
                )
                """
            ),
            {
                "sid": int(sid),
                "scanner": scanner,
                "wp": would_pass,
                "dsr": eval_payload.get("deflated_sharpe"),
                "pbo": eval_payload.get("pbo"),
                "paths": eval_payload.get("cpcv_n_paths"),
                "pname": pname or None,
                "skipped": skipped,
            },
        )
    except Exception as exc:
        logger.debug("[cpcv_shadow] persist skipped: %s", exc)


__all__ = [
    "CPCV_FEATURE_NAMES",
    "LGBM_CPCV_PARAMS",
    "SCANNER_BUCKETS",
    "bars_per_year",
    "cpcv_vertical_max_bars",
    "infer_scanner_bucket",
    "evaluate_pattern_cpcv",
    "evaluate_pattern_cpcv_realized_pnl",
    "filtered_rows_to_realized_series",
    "trade_sequence_annualization",
    "finalize_promotion_with_cpcv",
    "promotion_gate_passes",
    "normalize_ptr_row_features",
    "cpcv_eval_to_scan_pattern_fields",
    "persist_cpcv_shadow_eval",
]
