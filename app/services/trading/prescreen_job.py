"""Daily prescreen: write snapshot + upsert global candidates for run_full_market_scan."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import PrescreenCandidate, PrescreenSnapshot
from .prescreen_internal_signals import collect_internal_prescreen_tickers
from .prescreen_normalize import normalize_prescreen_ticker
from .prescreener import collect_prescreen_with_provenance

logger = logging.getLogger(__name__)


def _settings_subset() -> dict[str, Any]:
    return {
        "brain_crypto_universe_max": getattr(settings, "brain_crypto_universe_max", None),
        "brain_scan_include_full_crypto_universe": getattr(
            settings, "brain_scan_include_full_crypto_universe", True
        ),
    }


def run_daily_prescreen_job(db: Session) -> dict[str, Any]:
    """Collect external + internal tickers, write snapshot row, upsert global candidates."""
    run_id = str(uuid.uuid4())
    tz_label = "America/Los_Angeles"
    started = datetime.utcnow()
    snap = PrescreenSnapshot(
        run_id=run_id,
        run_started_at=started,
        timezone_label=tz_label,
        settings_json=_settings_subset(),
        status_json={"phase": "running"},
        source_map_json={},
        inclusion_summary_json={},
        candidate_count=0,
    )
    db.add(snap)
    db.flush()

    try:
        ext_list, ticker_sources, source_counts, elapsed = collect_prescreen_with_provenance(
            include_crypto=True,
            max_total=int(getattr(settings, "brain_prescreen_max_total", 3000)),
        )
        internal = collect_internal_prescreen_tickers(db)
        inclusion_summary: dict[str, int] = {
            "external_screen": len(ext_list),
            "internal_signal": len(internal),
        }

        # Merge: external first, then internal-only tickers
        all_tickers: list[str] = list(ext_list)
        seen = set(ext_list)
        for t in internal:
            if t not in seen:
                seen.add(t)
                all_tickers.append(t)

        ticker_reasons: dict[str, list[dict[str, Any]]] = {}
        for t in ext_list:
            srcs = ticker_sources.get(t) or []
            ticker_reasons[t] = [{"kind": "external_screen", "sources": srcs}]
        for t, rs in internal.items():
            ticker_reasons.setdefault(t, []).extend(rs)

        snap.source_map_json = dict(source_counts)
        snap.inclusion_summary_json = inclusion_summary
        snap.candidate_count = len(all_tickers)
        snap.status_json = {
            "phase": "ok",
            "elapsed_s": round(elapsed, 2),
            "external_count": len(ext_list),
            "internal_extra_count": len(all_tickers) - len(ext_list),
        }
        snap.run_finished_at = datetime.utcnow()

        now = datetime.utcnow()
        todays_norms: set[str] = set()

        for t in all_tickers:
            tn = normalize_prescreen_ticker(t)
            if not tn:
                continue
            todays_norms.add(tn)
            reasons = ticker_reasons.get(tn) or ticker_reasons.get(t) or [{"kind": "unknown"}]
            sources_payload = {"tags": ticker_sources.get(tn, ticker_sources.get(t, []))}

            row = (
                db.query(PrescreenCandidate)
                .filter(PrescreenCandidate.user_id.is_(None))
                .filter(PrescreenCandidate.ticker_norm == tn)
                .first()
            )
            if row is None:
                db.add(
                    PrescreenCandidate(
                        snapshot_id=snap.id,
                        user_id=None,
                        ticker=tn,
                        ticker_norm=tn,
                        active=True,
                        first_seen_at=now,
                        last_seen_at=now,
                        modified_at=now,
                        entry_reasons=reasons,
                        sources_json=sources_payload,
                    )
                )
            else:
                row.snapshot_id = snap.id
                row.ticker = tn
                row.active = True
                row.last_seen_at = now
                row.modified_at = now
                row.entry_reasons = reasons
                row.sources_json = sources_payload

        # Deactivate global rows not in today's set (skip if run produced no tickers)
        if todays_norms:
            db.query(PrescreenCandidate).filter(
                PrescreenCandidate.user_id.is_(None),
                PrescreenCandidate.ticker_norm.notin_(list(todays_norms)),
            ).update(
                {PrescreenCandidate.active: False, PrescreenCandidate.modified_at: now},
                synchronize_session=False,
            )

        db.commit()
        return {
            "ok": True,
            "run_id": run_id,
            "snapshot_id": snap.id,
            "candidate_count": len(all_tickers),
        }
    except Exception as e:
        logger.exception("[prescreen_job] failed: %s", e)
        snap.run_finished_at = datetime.utcnow()
        snap.status_json = {"phase": "error", "error": str(e)[:500]}
        db.commit()
        return {"ok": False, "run_id": run_id, "error": str(e)}


def load_active_global_candidate_tickers(db: Session) -> list[str]:
    """Tickers for full scan: active global prescreen rows, ordered by ticker_norm."""
    rows = (
        db.query(PrescreenCandidate.ticker_norm)
        .filter(PrescreenCandidate.user_id.is_(None))
        .filter(PrescreenCandidate.active.is_(True))
        .order_by(PrescreenCandidate.ticker_norm.asc())
        .all()
    )
    return [r[0] for r in rows if r[0]]


def prescreen_candidates_for_universe(
    db: Session | None,
    *,
    max_total: int = 3000,
    include_crypto: bool = True,
) -> list[str]:
    """Authoritative prescreen list: active DB rows when present, else live screeners."""
    if db is not None:
        rows = load_active_global_candidate_tickers(db)
        if rows:
            return list(rows[:max_total]) if len(rows) > max_total else list(rows)
    from .prescreener import _fetch_prescreen_universe_from_providers

    return _fetch_prescreen_universe_from_providers(
        include_crypto=include_crypto, max_total=max_total,
    )


def count_active_global_candidates(db: Session) -> int:
    return int(
        db.query(func.count(PrescreenCandidate.id))
        .filter(PrescreenCandidate.user_id.is_(None))
        .filter(PrescreenCandidate.active.is_(True))
        .scalar()
        or 0
    )


def get_latest_prescreen_summary(db: Session) -> dict[str, Any]:
    """Latest snapshot metadata for learning cycle reporting."""
    snap = (
        db.query(PrescreenSnapshot)
        .order_by(PrescreenSnapshot.run_started_at.desc())
        .first()
    )
    if not snap:
        return {}
    return {
        "snapshot_id": snap.id,
        "run_id": snap.run_id,
        "run_started_at": snap.run_started_at.isoformat() if snap.run_started_at else None,
        "source_map": snap.source_map_json or {},
        "inclusion_summary": snap.inclusion_summary_json or {},
        "status": snap.status_json or {},
        "candidate_count": snap.candidate_count,
    }
