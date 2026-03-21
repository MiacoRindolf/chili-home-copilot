"""Evidence hypothesis cards from pattern trade analytics."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import LearningEvent, PatternEvidenceHypothesis, PatternTradeRow
from .pattern_trade_analysis import analyze_pattern_trades

logger = logging.getLogger(__name__)


def propose_from_analysis(
    db: Session,
    scan_pattern_id: int,
    *,
    window_days: int = 180,
    user_id: int | None = None,
) -> list[PatternEvidenceHypothesis]:
    """Create proposed PatternEvidenceHypothesis rows from bucket analysis (idempotent per run)."""
    report = analyze_pattern_trades(db, scan_pattern_id, window_days=window_days)
    created: list[PatternEvidenceHypothesis] = []
    for b in report.buckets[:5]:
        feat = b.get("feature")
        low = b.get("low") or {}
        high = b.get("high") or {}
        if not feat:
            continue
        try:
            exp_low = low.get("mean_return") or 0
            exp_high = high.get("mean_return") or 0
            if exp_low == exp_high:
                continue
            title = f"{feat}: above_median mean {exp_high:.2f}% vs below {exp_low:.2f}%"
            pred = {
                "type": "median_split",
                "feature_key": feat,
                "median": b.get("median"),
                "favor": "above_median" if exp_high > exp_low else "below_median",
            }
            metrics = {
                "n_low": low.get("n"),
                "n_high": high.get("n"),
                "mean_low": exp_low,
                "mean_high": exp_high,
                "window_days": window_days,
                "total_rows": report.total_rows,
            }
            hyp = PatternEvidenceHypothesis(
                scan_pattern_id=scan_pattern_id,
                title=title[:200],
                predicate_json=pred,
                status="proposed",
                metrics_json=metrics,
            )
            db.add(hyp)
            created.append(hyp)
        except Exception as e:
            logger.debug("[evidence] skip bucket: %s", e)
    if created:
        db.commit()
        for h in created:
            db.refresh(h)
        try:
            log_pattern_evidence_event(
                db, user_id, "pattern_evidence",
                f"Proposed {len(created)} evidence hypotheses for pattern {scan_pattern_id}",
            )
        except Exception:
            pass
    return created


def log_pattern_evidence_event(
    db: Session,
    user_id: int | None,
    event_type: str,
    description: str,
) -> None:
    db.add(LearningEvent(user_id=user_id, event_type=event_type[:30], description=description[:2000]))
    db.commit()


def walk_forward_validate(
    db: Session,
    hypothesis_id: int,
    *,
    is_days: int = 90,
    oos_days: int = 90,
) -> dict[str, Any]:
    """Split window: first is_days vs last oos_days; compare mean outcome in predicate buckets."""
    hyp = db.query(PatternEvidenceHypothesis).filter(PatternEvidenceHypothesis.id == hypothesis_id).first()
    if not hyp:
        return {"ok": False, "error": "hypothesis not found"}
    pred = hyp.predicate_json or {}
    if pred.get("type") != "median_split":
        return {"ok": False, "error": "unsupported predicate type"}
    feat_key = pred.get("feature_key")
    med = pred.get("median")
    if feat_key is None or med is None:
        return {"ok": False, "error": "incomplete predicate"}

    now = datetime.utcnow()
    is_start = now - timedelta(days=is_days + oos_days)
    is_end = now - timedelta(days=oos_days)
    oos_start = is_end

    def _mean_for_window(start: datetime, end: datetime, above: bool) -> tuple[float, int]:
        q = (
            db.query(PatternTradeRow)
            .filter(PatternTradeRow.scan_pattern_id == hyp.scan_pattern_id)
            .filter(PatternTradeRow.as_of_ts >= start, PatternTradeRow.as_of_ts < end)
        )
        rows = q.all()
        vals: list[float] = []
        for r in rows:
            fj = r.features_json or {}
            if not isinstance(fj, dict):
                continue
            v = fj.get(feat_key)
            if v is None or r.outcome_return_pct is None:
                continue
            try:
                fv = float(v)
                if above and fv > float(med):
                    vals.append(float(r.outcome_return_pct))
                if not above and fv <= float(med):
                    vals.append(float(r.outcome_return_pct))
            except (TypeError, ValueError):
                continue
        if not vals:
            return 0.0, 0
        return sum(vals) / len(vals), len(vals)

    favor_above = pred.get("favor") == "above_median"
    is_mean, is_n = _mean_for_window(is_start, is_end, favor_above)
    oos_mean, oos_n = _mean_for_window(oos_start, now, favor_above)
    bench_is, _ = _mean_for_window(is_start, is_end, not favor_above)
    bench_oos, _ = _mean_for_window(oos_start, now, not favor_above)

    if favor_above:
        validated = is_n >= 15 and oos_n >= 15 and oos_mean > bench_oos
    else:
        validated = is_n >= 15 and oos_n >= 15 and oos_mean > bench_oos

    out = {
        "ok": True,
        "is_mean": round(is_mean, 4),
        "is_n": is_n,
        "oos_mean": round(oos_mean, 4),
        "oos_n": oos_n,
        "bench_is_mean": round(bench_is, 4),
        "bench_oos_mean": round(bench_oos, 4),
        "validated": validated,
    }
    hyp.metrics_json = {**(hyp.metrics_json or {}), "walk_forward": out}
    hyp.status = "validated" if validated else hyp.status
    hyp.updated_at = datetime.utcnow()
    db.commit()
    return out
