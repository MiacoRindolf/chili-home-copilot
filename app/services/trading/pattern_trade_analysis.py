"""Bucket and stability analysis for PatternTradeRow."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import PatternTradeRow

logger = logging.getLogger(__name__)

DEFAULT_MIN_N = 50
WARN_MIN_N = 30


@dataclass
class BucketStats:
    bucket: str
    n: int
    mean_return: float | None
    median_return: float | None
    win_rate_pct: float | None
    profit_factor: float | None


@dataclass
class AnalysisReport:
    scan_pattern_id: int
    window_days: int
    total_rows: int
    as_of_min: str | None
    as_of_max: str | None
    warnings: list[str] = field(default_factory=list)
    numeric_keys: list[str] = field(default_factory=list)
    buckets: list[dict[str, Any]] = field(default_factory=list)
    ticker_rollup: list[dict[str, Any]] = field(default_factory=list)
    stability: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _numeric_from_features(row: PatternTradeRow) -> dict[str, float]:
    out: dict[str, float] = {}
    fj = row.features_json or {}
    if not isinstance(fj, dict):
        return out
    for k, v in fj.items():
        if isinstance(v, (int, float)) and k not in ("schema",):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    if row.outcome_return_pct is not None:
        out["_outcome_return_pct"] = float(row.outcome_return_pct)
    return out


def _profit_factor(returns: list[float]) -> float | None:
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    if losses <= 0:
        return None if gains <= 0 else gains
    return gains / losses


def analyze_pattern_trades(
    db: Session,
    scan_pattern_id: int,
    *,
    window_days: int = 180,
    min_n: int = DEFAULT_MIN_N,
    top_k_drop: int = 5,
) -> AnalysisReport:
    """Trade-level median split on numeric feature keys + ticker rollup + concentration."""
    since = datetime.utcnow() - timedelta(days=window_days)
    q = (
        db.query(PatternTradeRow)
        .filter(PatternTradeRow.scan_pattern_id == scan_pattern_id)
        .filter(PatternTradeRow.as_of_ts >= since)
    )
    rows = q.all()
    report = AnalysisReport(
        scan_pattern_id=scan_pattern_id,
        window_days=window_days,
        total_rows=len(rows),
        as_of_min=min((r.as_of_ts.isoformat() for r in rows), default=None),
        as_of_max=max((r.as_of_ts.isoformat() for r in rows), default=None),
    )
    if len(rows) < WARN_MIN_N:
        report.warnings.append(f"Low sample size n={len(rows)} (warn below {WARN_MIN_N})")
    if len(rows) < min_n:
        report.warnings.append(f"Below analysis min_n={min_n}; buckets may be unreliable")

    # Collect outcomes per ticker
    by_ticker: dict[str, list[float]] = {}
    numeric_keys: set[str] = set()
    parsed: list[tuple[PatternTradeRow, dict[str, float]]] = []
    for r in rows:
        d = _numeric_from_features(r)
        parsed.append((r, d))
        numeric_keys.update(k for k in d if not k.startswith("_"))
        oc = r.outcome_return_pct
        if oc is not None:
            by_ticker.setdefault(r.ticker, []).append(float(oc))

    report.numeric_keys = sorted(numeric_keys)

    # Median split for first few keys (cap compute)
    for feat in report.numeric_keys[:8]:
        vals = [(r, d.get(feat)) for r, d in parsed if d.get(feat) is not None]
        if len(vals) < 10:
            continue
        xs = [v for _, v in vals if v is not None]
        med = median(xs)
        low_rets = [
            float(r.outcome_return_pct)
            for r, v in vals
            if v is not None and r.outcome_return_pct is not None and v <= med
        ]
        high_rets = [
            float(r.outcome_return_pct)
            for r, v in vals
            if v is not None and r.outcome_return_pct is not None and v > med
        ]
        if len(low_rets) < 3 or len(high_rets) < 3:
            continue

        def _bs(name: str, rets: list[float]) -> BucketStats:
            wr = 100.0 * sum(1 for x in rets if x > 0) / len(rets) if rets else None
            return BucketStats(
                bucket=name,
                n=len(rets),
                mean_return=sum(rets) / len(rets) if rets else None,
                median_return=median(rets) if rets else None,
                win_rate_pct=wr,
                profit_factor=_profit_factor(rets),
            )

        report.buckets.append({
            "feature": feat,
            "median": med,
            "low": asdict(_bs("below_median", low_rets)),
            "high": asdict(_bs("above_median", high_rets)),
        })

    # Ticker rollup
    for tkr, rets in sorted(by_ticker.items(), key=lambda x: -len(x[1]))[:200]:
        if len(rets) < 1:
            continue
        report.ticker_rollup.append({
            "ticker": tkr,
            "n": len(rets),
            "mean_return": sum(rets) / len(rets),
            "median_return": median(rets),
            "win_rate_pct": 100.0 * sum(1 for x in rets if x > 0) / len(rets),
            "profit_factor": _profit_factor(rets),
        })

    # Stability: concentration of PnL in top tickers by |mean|*n proxy
    if by_ticker:
        scores = []
        for tk, v in by_ticker.items():
            mag = abs(sum(v) / len(v)) * len(v) if v else 0.0
            scores.append((tk, mag))
        scores.sort(key=lambda x: -x[1])
        total_score = sum(s for _, s in scores) or 1.0
        top_share = sum(s for _, s in scores[:top_k_drop]) / total_score
        report.stability["top_k_tickers"] = [x[0] for x in scores[:top_k_drop]]
        report.stability["top_k_pnl_proxy_share"] = round(top_share, 3)
        if top_share > 0.8:
            report.warnings.append(
                f"Top {top_k_drop} tickers drive ~{top_share:.0%} of score proxy — concentrated edge"
            )

    # Calendar split rough: first half vs second half of sample by as_of_ts
    if len(rows) >= 20:
        sorted_rows = sorted(rows, key=lambda r: r.as_of_ts)
        mid = len(sorted_rows) // 2
        a = [float(r.outcome_return_pct) for r in sorted_rows[:mid] if r.outcome_return_pct is not None]
        b = [float(r.outcome_return_pct) for r in sorted_rows[mid:] if r.outcome_return_pct is not None]
        if len(a) >= 5 and len(b) >= 5:
            report.stability["first_half_mean"] = sum(a) / len(a)
            report.stability["second_half_mean"] = sum(b) / len(b)

    return report
