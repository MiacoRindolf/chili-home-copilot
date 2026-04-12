"""Research hygiene: causality / bar alignment checks and backtest provenance (CHILI-native).

Inspired by public research docs (e.g. lookahead analysis); no third-party GPL code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def rules_json_fingerprint(conditions: list[dict[str, Any]] | None) -> str | None:
    if not conditions:
        return None
    try:
        raw = json.dumps(conditions, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_data_provenance(
    *,
    ticker: str,
    period: str,
    interval: str,
    df: pd.DataFrame | None,
    ohlc_start: str | None = None,
    ohlc_end: str | None = None,
    scan_pattern_id: int | None = None,
    rules_fingerprint: str | None = None,
    provider_hint: str = "market_data_fetch",
) -> dict[str, Any]:
    row_count = int(len(df)) if df is not None and not df.empty else 0
    prov: dict[str, Any] = {
        "ticker": str(ticker).upper(),
        "period": str(period),
        "interval": str(interval),
        "ohlc_bars": row_count,
        "ohlc_start": ohlc_start,
        "ohlc_end": ohlc_end,
        "scan_pattern_id": scan_pattern_id,
        "rules_fingerprint": rules_fingerprint,
        "provider_hint": provider_hint,
    }
    if df is not None and not df.empty:
        idx = df.index
        try:
            prov["chart_time_from"] = int(pd.Timestamp(idx[0]).timestamp())
            prov["chart_time_to"] = int(pd.Timestamp(idx[-1]).timestamp())
        except Exception:
            pass
    return prov


def _values_equal(a: Any, b: Any, *, float_tol: float = 1e-4) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) and isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, float) and isinstance(b, float):
            if math.isnan(a) and math.isnan(b):
                return True
        try:
            return abs(float(a) - float(b)) <= float_tol
        except (TypeError, ValueError):
            return False
    return a == b


def check_signal_bar_alignment(
    df: pd.DataFrame,
    conditions: list[dict[str, Any]],
    indicator_arrays: dict[str, list],
    *,
    max_check_bars: int = 48,
    float_tol: float = 1e-4,
) -> dict[str, Any]:
    """Assert precomputed indicator rows match truncation recomputation (causal bar alignment).

    For sampled bar indices *i*, recomputes indicators on ``df.iloc[:i+1]`` and compares
    the last row to ``indicator_arrays[key][i]``.
    """
    from ..backtest_service import _compute_series_for_conditions

    n = len(df)
    mismatches: list[dict[str, Any]] = []
    if n < 10 or not conditions or not indicator_arrays:
        return {
            "lookahead_ok": True,
            "causality_checked_bars": 0,
            "mismatches": [],
            "detail": "skipped_short_or_empty",
        }

    work = df.copy()
    work.index = pd.to_datetime(work.index)
    if work.index.tz is not None:
        work.index = work.index.tz_localize(None)

    lo = max(30, n // 4)
    hi = n - 1
    if hi <= lo:
        indices = [hi]
    else:
        step = max(1, (hi - lo) // max(1, max_check_bars))
        indices = list(range(lo, hi + 1, step))[:max_check_bars]
        if indices[-1] != hi:
            indices.append(hi)

    for i in indices:
        sub = work.iloc[: i + 1].copy()
        try:
            fresh = _compute_series_for_conditions(sub, conditions)
        except Exception as ex:
            mismatches.append({"bar": i, "key": "__compute__", "error": str(ex)[:200]})
            continue
        for key, arr in indicator_arrays.items():
            if key not in fresh:
                continue
            if i >= len(arr) or i >= len(fresh[key]):
                continue
            v_full = arr[i]
            v_fresh = fresh[key][-1]
            if not _values_equal(v_full, v_fresh, float_tol=float_tol):
                mismatches.append({
                    "bar": i,
                    "key": key,
                    "precomputed": v_full,
                    "truncated_recompute": v_fresh,
                })
                if len(mismatches) >= 12:
                    break
        if len(mismatches) >= 12:
            break

    ok = len(mismatches) == 0
    if not ok:
        logger.warning(
            "[research_integrity] Causality check failed: %d mismatch(es) (bars=%s)",
            len(mismatches),
            [m.get("bar") for m in mismatches[:5]],
        )
    return {
        "lookahead_ok": ok,
        "causality_checked_bars": len(indices),
        "mismatches": mismatches[:8],
    }


def check_recursive_indicator_sanity(
    df: pd.DataFrame,
    indicator_arrays: dict[str, list],
    *,
    sample_keys: tuple[str, ...] = ("rsi_14", "sma_20", "sma_50", "ema_12", "ema_26"),
    max_indices: int = 24,
) -> dict[str, Any]:
    """Cheap sanity: rolling-type series at *i* should not equal the same series shifted by -1
    everywhere (a weak signal of accidental forward shift). Best-effort; skipped if keys absent.
    """
    close = df["Close"].astype(float)
    n = len(close)
    warnings: list[str] = []
    if n < 50:
        return {"recursive_ok": True, "warnings": [], "detail": "skipped_short"}

    for key in sample_keys:
        arr = indicator_arrays.get(key)
        if not arr or len(arr) != n:
            continue
        hits = 0
        checked = 0
        start = max(25, n // 5)
        step = max(1, (n - 1 - start) // max_indices)
        for i in range(start, n, step):
            if i < 1 or i >= len(arr):
                continue
            a = arr[i]
            b = arr[i - 1]
            if a is None or b is None:
                continue
            try:
                fa, fb = float(a), float(b)
            except (TypeError, ValueError):
                continue
            checked += 1
            if abs(fa - fb) < 1e-12:
                hits += 1
        if checked >= 8 and hits >= checked - 2:
            warnings.append(
                f"{key}: nearly identical to lag-1 across samples (possible shift bug)"
            )

    ok = len(warnings) == 0
    return {"recursive_ok": ok, "warnings": warnings}


def build_research_integrity_report(
    df: pd.DataFrame,
    conditions: list[dict[str, Any]],
    indicator_arrays: dict[str, list],
    *,
    max_check_bars: int = 48,
) -> dict[str, Any]:
    caus = check_signal_bar_alignment(
        df, conditions, indicator_arrays, max_check_bars=max_check_bars,
    )
    rec = check_recursive_indicator_sanity(df, indicator_arrays)
    return {
        "lookahead_ok": bool(caus.get("lookahead_ok", True)),
        "causality_checked_bars": caus.get("causality_checked_bars", 0),
        "mismatches": caus.get("mismatches") or [],
        "recursive_ok": bool(rec.get("recursive_ok", True)),
        "recursive_warnings": rec.get("warnings") or [],
    }


def enrich_pattern_backtest_result(
    result: dict[str, Any],
    df: pd.DataFrame,
    conditions: list[dict[str, Any]],
    *,
    ticker: str,
    period: str,
    interval: str,
    ohlc_start: str | None = None,
    ohlc_end: str | None = None,
    scan_pattern_id: int | None = None,
    indicator_arrays: dict[str, list] | None = None,
) -> None:
    """Mutates *result* with ``data_provenance`` and ``research_integrity``."""
    from ...config import settings

    if not getattr(settings, "brain_research_integrity_enabled", True):
        result.setdefault(
            "data_provenance",
            build_data_provenance(
                ticker=ticker,
                period=period,
                interval=interval,
                df=df,
                ohlc_start=ohlc_start,
                ohlc_end=ohlc_end,
                scan_pattern_id=scan_pattern_id,
                rules_fingerprint=rules_json_fingerprint(conditions),
            ),
        )
        result.setdefault("research_integrity", {"skipped": True, "reason": "integrity_disabled"})
        return

    fp = rules_json_fingerprint(conditions)
    result["data_provenance"] = build_data_provenance(
        ticker=ticker,
        period=period,
        interval=interval,
        df=df,
        ohlc_start=ohlc_start,
        ohlc_end=ohlc_end,
        scan_pattern_id=scan_pattern_id,
        rules_fingerprint=fp,
    )

    arrays = indicator_arrays
    if arrays is None:
        from ..backtest_service import _compute_series_for_conditions

        work = df.copy()
        work.index = pd.to_datetime(work.index)
        if work.index.tz is not None:
            work.index = work.index.tz_localize(None)
        arrays = _compute_series_for_conditions(work, conditions)

    max_bars = int(getattr(settings, "brain_research_integrity_max_check_bars", 48) or 48)
    report = build_research_integrity_report(
        df, conditions, arrays, max_check_bars=max_bars,
    )
    result["research_integrity"] = report

    strict = bool(getattr(settings, "brain_research_integrity_strict", False))
    if strict and not report.get("lookahead_ok", True):
        logger.error(
            "[research_integrity] Strict mode: lookahead/causality failed for %s %s",
            ticker,
            fp,
        )


def enrich_generic_backtest_result(
    result: dict[str, Any],
    df: pd.DataFrame,
    *,
    ticker: str,
    period: str,
    interval: str,
    strategy_id: str,
) -> None:
    """Attach provenance for non-pattern (registry) backtests."""
    result["data_provenance"] = build_data_provenance(
        ticker=ticker,
        period=period,
        interval=interval,
        df=df,
        scan_pattern_id=None,
        rules_fingerprint=strategy_id,
        provider_hint="registry_strategy",
    )
    result["research_integrity"] = {
        "lookahead_ok": True,
        "causality_checked_bars": 0,
        "mismatches": [],
        "recursive_ok": True,
        "recursive_warnings": [],
        "detail": "generic_strategy_not_pattern_scanned",
    }


def aggregate_promotion_integrity(per_ticker_reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold multiple ticker-level ``research_integrity`` dicts into one summary for ScanPattern."""
    if not per_ticker_reports:
        return {
            "lookahead_ok_all": True,
            "any_warnings": False,
            "per_ticker": [],
            "detail": "no_reports",
        }
    ok_all = all(bool(r.get("lookahead_ok", True)) for r in per_ticker_reports)
    rec_ok = all(bool(r.get("recursive_ok", True)) for r in per_ticker_reports)
    warnings_any = any(
        (r.get("mismatches") or []) or (r.get("recursive_warnings") or [])
        for r in per_ticker_reports
    )
    return {
        "lookahead_ok_all": ok_all,
        "recursive_ok_all": rec_ok,
        "any_warnings": warnings_any,
        "per_ticker": per_ticker_reports[:20],
    }


def promotion_blocked_by_integrity(
    agg: dict[str, Any],
    *,
    target_status: str,
) -> bool:
    from ...config import settings

    if not bool(getattr(settings, "brain_research_integrity_strict", True)):
        logger.warning("[research-integrity] strict mode DISABLED — lookahead-biased patterns may reach live trading")
        return False
    if target_status != "promoted":
        return False
    if not agg.get("lookahead_ok_all", True):
        return True
    return False
