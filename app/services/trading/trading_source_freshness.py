"""Ground-truth timestamps for Trading Brain opportunity board freshness (read-only queries)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models.trading import BrainBatchJob, PrescreenCandidate, PrescreenSnapshot, ScanResult
from .batch_job_constants import JOB_PATTERN_IMMINENT_SCANNER

logger = logging.getLogger(__name__)

BOARD_FRESHNESS_KEYS: tuple[str, ...] = (
    "scan_results_latest_utc",
    "prescreen_snapshot_finished_latest_utc",
    "prescreen_candidate_last_seen_latest_utc",
    "imminent_job_ok_latest_utc",
    "predictions_cache_last_updated_utc",
)


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    a = _aware_utc(dt)
    return a.isoformat() if a else None


def collect_source_freshness(db: Session) -> dict[str, Any]:
    """Latest known timestamps for feeds that feed the board (UTC ISO or null).

    Semantics:
    - Each value is the newest row time we can attribute to that feed.
    - ``data_as_of`` for the board is computed separately as the **minimum** of these
      non-null times (conservative): the composite view cannot be fresher than the
      stalest contributing source.
    """
    out: dict[str, Any] = {
        "scan_results_latest_utc": None,
        "prescreen_snapshot_finished_latest_utc": None,
        "prescreen_candidate_last_seen_latest_utc": None,
        "imminent_job_ok_latest_utc": None,
        "predictions_cache_last_updated_utc": None,
    }
    try:
        mx = db.query(func.max(ScanResult.scanned_at)).scalar()
        out["scan_results_latest_utc"] = _iso(mx)
    except Exception as e:
        logger.debug("[source_freshness] scan_results: %s", e)
    try:
        mx = db.query(func.max(PrescreenSnapshot.run_finished_at)).scalar()
        out["prescreen_snapshot_finished_latest_utc"] = _iso(mx)
    except Exception as e:
        logger.debug("[source_freshness] prescreen_snapshot: %s", e)
    try:
        mx = db.query(func.max(PrescreenCandidate.last_seen_at)).scalar()
        out["prescreen_candidate_last_seen_latest_utc"] = _iso(mx)
    except Exception as e:
        logger.debug("[source_freshness] prescreen_candidate: %s", e)
    try:
        row = (
            db.query(BrainBatchJob)
            .filter(
                BrainBatchJob.job_type == JOB_PATTERN_IMMINENT_SCANNER,
                BrainBatchJob.status == "ok",
                BrainBatchJob.ended_at.isnot(None),
            )
            .order_by(BrainBatchJob.ended_at.desc())
            .first()
        )
        out["imminent_job_ok_latest_utc"] = _iso(row.ended_at) if row else None
    except Exception as e:
        logger.debug("[source_freshness] imminent_job: %s", e)

    try:
        from .learning import get_prediction_swr_cache_meta

        meta = get_prediction_swr_cache_meta()
        ts = float(meta.get("cache_last_updated_unix") or 0.0)
        if ts > 0:
            out["predictions_cache_last_updated_utc"] = datetime.fromtimestamp(
                ts, tz=timezone.utc
            ).isoformat()
    except Exception as e:
        logger.debug("[source_freshness] predictions_cache: %s", e)

    return out


def compute_board_data_as_of(
    source_freshness: dict[str, Any],
    *,
    keys: list[str] | tuple[str, ...] | None = None,
) -> tuple[str | None, list[str]]:
    """Return (data_as_of_iso_utc, keys_used_in_min).

    ``data_as_of`` = min(non-null source timestamps), all parsed as UTC-aware.
    """
    keys_to_consider = tuple(keys or BOARD_FRESHNESS_KEYS)
    parsed: list[tuple[datetime, str]] = []
    for k in keys_to_consider:
        raw = source_freshness.get(k)
        if not raw or not isinstance(raw, str):
            continue
        try:
            s = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            parsed.append((_aware_utc(dt) or dt, k))
        except (ValueError, TypeError):
            continue
    if not parsed:
        return None, []
    # data_as_of is the minimum (stalest) among sources — conservative bound.
    min_dt = min(t for t, _ in parsed)
    return min_dt.isoformat(), [k for t, k in parsed if t == min_dt]
