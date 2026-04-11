"""Selection-bias / validation-slice burn accounting (repeatable-edge lane).

v1: append-only ledger keyed by ``research_run_key`` so identical evaluation fingerprints
do not inflate usage on accidental retries. ``slice_key`` is derived from **actual** per-ticker
validation context (chart windows, bar counts, holdout), not nominal request settings alone.

This is research hygiene, not a multiple-testing correction.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ...models.trading import BrainValidationSliceLedger

logger = logging.getLogger(__name__)

SELECTION_BIAS_VERSION = 1

APPROXIMATION_NOTE = (
    "CHILI v1 slice accounting: counts distinct research_run_key inserts per validation slice; "
    "does not correct for full multiple testing. Burn tier is a hygiene signal."
)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha64(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:64]


def build_validation_slice_key(
    *,
    origin: str,
    asset_class: str,
    timeframe: str,
    hypothesis_family: str | None,
    eval_rows: list[dict[str, Any]],
) -> str:
    """Hash from **observed** evaluation context (per successful ticker backtest)."""
    rows = sorted(
        [
            {
                "ticker": (r.get("ticker") or "").strip().upper(),
                "chart_time_from": r.get("chart_time_from"),
                "chart_time_to": r.get("chart_time_to"),
                "ohlc_bars": r.get("ohlc_bars"),
                "in_sample_bars": r.get("in_sample_bars"),
                "out_of_sample_bars": r.get("out_of_sample_bars"),
                "oos_holdout_fraction": r.get("oos_holdout_fraction"),
                "period": r.get("period"),
                "interval": r.get("interval"),
                "spread_used": r.get("spread_used"),
                "commission_used": r.get("commission_used"),
            }
            for r in eval_rows
            if isinstance(r, dict) and r.get("ticker")
        ],
        key=lambda x: x["ticker"],
    )
    payload = {
        "origin": (origin or "").strip().lower(),
        "asset_class": (asset_class or "").strip().lower(),
        "timeframe": (timeframe or "").strip().lower(),
        "hypothesis_family": (hypothesis_family or "").strip().lower() or None,
        "evaluated_tickers": rows,
    }
    return _sha64(_canonical_json(payload))


def build_outcome_fingerprint(eval_rows: list[dict[str, Any]]) -> str:
    """Stable fingerprint of headline outcomes per ticker (for research_run_key)."""
    parts = []
    for r in sorted(eval_rows, key=lambda x: (x.get("ticker") or "").upper()):
        if not isinstance(r, dict):
            continue
        t = (r.get("ticker") or "").strip().upper()
        if not t:
            continue
        oos = r.get("oos_win_rate")
        isw = r.get("is_win_rate")
        parts.append(
            (
                t,
                r.get("chart_time_to"),
                r.get("in_sample_bars"),
                r.get("out_of_sample_bars"),
                round(float(r.get("oos_holdout_fraction") or 0), 6),
                round(float(isw or 0), 2),
                round(float(oos), 2) if oos is not None else None,
                int(r.get("trade_count") or 0),
            )
        )
    return _sha64(_canonical_json(parts))


def build_research_run_key(
    *,
    slice_key: str,
    scan_pattern_id: int,
    rules_fingerprint: str | None,
    outcome_fingerprint: str,
) -> str:
    """One row per distinct hypothesis evaluation; retries with identical fingerprint dedupe."""
    raw = _canonical_json(
        {
            "slice_key": slice_key,
            "scan_pattern_id": int(scan_pattern_id),
            "rules_fingerprint": rules_fingerprint,
            "outcome_fingerprint": outcome_fingerprint,
        }
    )
    return _sha64(raw)


def record_validation_slice_use(
    db: Session,
    *,
    research_run_key: str,
    slice_key: str,
    scan_pattern_id: int,
    rules_fingerprint: str | None,
    param_hash: str | None = None,
) -> bool:
    """Insert ledger row in the current session; return True if a new row was inserted."""
    stmt = pg_insert(BrainValidationSliceLedger).values(
        research_run_key=research_run_key[:64],
        slice_key=slice_key[:64],
        scan_pattern_id=int(scan_pattern_id),
        rules_fingerprint=(rules_fingerprint[:32] if rules_fingerprint else None),
        param_hash=(param_hash[:64] if param_hash else None),
        recorded_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["research_run_key"]).returning(
        BrainValidationSliceLedger.id
    )
    try:
        res = db.execute(stmt)
        row = res.fetchone()
        return row is not None and row[0] is not None
    except Exception as e:
        logger.debug("[selection_bias] ledger insert skipped: %s", e)
        return False


def summarize_slice_usage(db: Session, *, slice_key: str) -> dict[str, Any]:
    """Aggregate stats for a slice (queryable without scanning JSON blobs)."""
    sk = slice_key[:64]
    q = db.query(
        func.count(BrainValidationSliceLedger.id),
        func.count(func.distinct(BrainValidationSliceLedger.scan_pattern_id)),
        func.count(func.distinct(BrainValidationSliceLedger.rules_fingerprint)),
        func.count(func.distinct(BrainValidationSliceLedger.param_hash)),
        func.min(BrainValidationSliceLedger.recorded_at),
        func.max(BrainValidationSliceLedger.recorded_at),
    ).filter(BrainValidationSliceLedger.slice_key == sk)
    row = q.one()
    usage = int(row[0] or 0)
    d_pat = int(row[1] or 0)
    d_rf = int(row[2] or 0)
    d_ph = int(row[3] or 0)
    first_at = row[4]
    last_at = row[5]
    burn_score, burn_tier, burn_flags = _derive_burn(usage, d_pat, d_rf)
    return {
        "selection_bias_version": SELECTION_BIAS_VERSION,
        "validation_slice_key": sk,
        "usage_count": usage,
        "distinct_pattern_count": d_pat,
        "distinct_rules_fingerprint_count": d_rf,
        "distinct_param_hash_count": d_ph,
        "first_used_at": first_at.isoformat().replace("+00:00", "Z") if first_at else None,
        "last_used_at": last_at.isoformat().replace("+00:00", "Z") if last_at else None,
        "burn_score": burn_score,
        "burn_tier": burn_tier,
        "burn_flags": burn_flags,
        "approximation_note": APPROXIMATION_NOTE,
    }


def _derive_burn(usage: int, d_pat: int, d_rf: int) -> tuple[float, str, list[str]]:
    flags: list[str] = []
    if usage >= 12:
        flags.append("elevated_slice_touch_count")
    if d_pat >= 6:
        flags.append("many_patterns_on_slice")
    if d_rf >= 5:
        flags.append("many_rule_variants_on_slice")
    # score 0..1 monotonic in usage (simple v1)
    burn_score = min(1.0, usage / 20.0 + (d_pat - 1) * 0.03 + (d_rf - 1) * 0.02)
    if burn_score < 0.25:
        tier = "low"
    elif burn_score < 0.55:
        tier = "medium"
    else:
        tier = "high"
    return round(burn_score, 4), tier, flags


def build_selection_bias_contract(
    db: Session,
    *,
    slice_key: str,
    ledger_inserted: bool,
) -> dict[str, Any]:
    summary = summarize_slice_usage(db, slice_key=slice_key)
    summary["ledger_inserted_this_run"] = bool(ledger_inserted)
    return summary


def selection_bias_skip_contract(reason: str) -> dict[str, Any]:
    return {
        "selection_bias_version": SELECTION_BIAS_VERSION,
        "validation_slice_key": None,
        "usage_count": 0,
        "distinct_pattern_count": 0,
        "distinct_rules_fingerprint_count": 0,
        "distinct_param_hash_count": 0,
        "first_used_at": None,
        "last_used_at": None,
        "burn_score": 0.0,
        "burn_tier": "low",
        "burn_flags": [],
        "skip_reason": reason,
        "ledger_inserted_this_run": False,
        "approximation_note": APPROXIMATION_NOTE,
    }
