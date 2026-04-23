"""CPCV + DSR + PBO promotion gate (Q1.T1).

**HR1 (kill switch):** When ``CHILI_CPCV_PROMOTION_GATE_ENABLED`` is True, no caller may
treat a pattern as promotion-ready without running :func:`finalize_promotion_with_cpcv`
from :func:`check_promotion_ready` / :func:`check_promotion_ready_v2` in
``mining_validation`` (the only intended call sites). Do not add a parallel promotion
path that bypasses that funnel.

**Model / label contract (locked for Q2.T6 meta-classifier reuse):**
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

    if len(xs) < 30:
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


def evaluate_pattern_cpcv(
    pattern_id: int | None,
    filtered: list[dict[str, Any]],
    *,
    n_hypotheses_tested: int,
    n_target_paths: int = 100,
    purged_size: int = 20,
    embargo_size: int = 5,
    bar_interval_hint: str | None = None,
) -> dict[str, Any]:
    """Run Combinatorial Purged CV with LightGBM; compute DSR, PBO, path Sharpes."""
    del pattern_id  # reserved for logging / future DB correlation

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
    if n < 30:
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

    try:
        t_train = min(252, max(30, n // 2))
        n_folds, n_test_folds = optimal_folds_number(
            n_observations=n,
            target_train_size=t_train,
            target_n_test_paths=min(n_target_paths, max(2, n // 15)),
        )
        n_folds = max(5, min(n_folds, max(5, n // 10)))
        n_test_folds = max(2, min(n_test_folds, n_folds - 2))
        cv = CombinatorialPurgedCV(
            n_folds=n_folds,
            n_test_folds=n_test_folds,
            purged_size=purged_size,
            embargo_size=embargo_size,
        )
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

    try:
        splits = list(cv.split(X))
        for train_idx, test_idx in splits:
            te = _flatten_test_indices(test_idx)
            tr = np.asarray(train_idx, dtype=int)
            if tr.size < 30 or te.size < 5:
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
    }


def promotion_gate_passes(metrics: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return (ok, reasons) against Q1.T1 thresholds."""
    reasons: list[str] = []
    if metrics.get("skipped"):
        reasons.append(str(metrics.get("reason") or "skipped"))
        return False, reasons

    n_tr = int(metrics.get("n_trades") or metrics.get("n_labeled_samples") or 0)
    if n_tr < 30:
        reasons.append("n_trades_lt_30")

    dsr = metrics.get("deflated_sharpe")
    if dsr is None:
        reasons.append("dsr_missing")
    elif float(dsr) < 0.95:
        reasons.append("dsr_below_0_95")

    pbo = metrics.get("pbo")
    if pbo is None:
        reasons.append("pbo_missing")
    elif float(pbo) > 0.2:
        reasons.append("pbo_above_0_2")

    n_paths = int(metrics.get("cpcv_n_paths") or 0)
    if n_paths < 50:
        reasons.append("cpcv_n_paths_lt_50")

    med_sh = metrics.get("cpcv_median_sharpe")
    if med_sh is None:
        reasons.append("median_sharpe_missing")
    elif float(med_sh) < 0.5:
        reasons.append("median_sharpe_below_0_5")

    return (len(reasons) == 0), reasons


def finalize_promotion_with_cpcv(
    detail: dict[str, Any],
    filtered: list[dict[str, Any]],
    *,
    n_hypotheses_tested: int,
) -> dict[str, Any]:
    """Run CPCV evaluation and merge into *detail*; may set ``detail["blocked"]``.

    Call only from ``check_promotion_ready`` / ``check_promotion_ready_v2`` after
    prior gates pass (immediately before setting ready=True).
    """
    enforce = bool(getattr(settings, "chili_cpcv_promotion_gate_enabled", False))
    eval_payload = evaluate_pattern_cpcv(
        None,
        filtered,
        n_hypotheses_tested=n_hypotheses_tested,
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
    "finalize_promotion_with_cpcv",
    "promotion_gate_passes",
    "normalize_ptr_row_features",
    "cpcv_eval_to_scan_pattern_fields",
    "persist_cpcv_shadow_eval",
]
