"""Phase 5: compare-only + optional candidate-authoritative mirror reads (request-local; no cache mutation).

## Legacy-fallback retirement (tracked — Phase D tech-debt 2026-Q2)

Every ``read=fallback_*`` outcome in `[chili_prediction_ops]` is a case
where the mirror path could not serve the request (miss, empty, stale,
parity mismatch, ineligible, unexpected error) and we delegated back to
the legacy list. The fallbacks exist because Phase 5 rolled out before
the mirror's steady-state completeness was proven in prod.

**Retirement plan:** once two full quarters of production soak show
``fallback_miss`` + ``fallback_empty`` + ``fallback_stale`` at < 0.5%
of explicit-ticker reads (measured via `[chili_prediction_ops]` logs),
the fallback branches here become dead code and can be pulled out in
favor of returning the mirror value OR a clean error. Soak window
target: **2026-Q4 review, 2027-Q1 retirement PR**.

**Retirement is a new phase (Phase 9 in the roadmap), not an edit.**
Per Hard Rule 5 + ADR-004, authority changes require a new phase with
design + tests + soak + rollout doc. Phase 9 is the ONLY place legacy
reads may be removed. Do not short-circuit by deleting fallback
branches in an unrelated PR — that rewrites the authority contract
without the gating discipline.

Key parity-fail outcomes to watch during soak (see
``docs/TRADING_SLO.md``):
  - ``read=fallback_parity`` — drives Phase-9 readiness
  - ``read=fallback_stale`` — hints at learning-cycle latency (SLO 2)
  - ``read=fallback_miss`` — dual-write integrity signal
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...config import settings
from .prediction_line_mapper import prediction_universe_fingerprint
from .prediction_ops_log import (
    READ_AUTH_MIRROR,
    READ_COMPARE_MISMATCH,
    READ_COMPARE_MISS,
    READ_COMPARE_OK,
    READ_ERROR,
    READ_FALLBACK_EMPTY,
    READ_FALLBACK_INELIGIBLE,
    READ_FALLBACK_MISS,
    READ_FALLBACK_PARITY,
    READ_FALLBACK_STALE,
    READ_NA,
    universe_fingerprint_fp16,
)
from .prediction_read_hydrate import mirror_lines_to_legacy_rows
from .prediction_read_parity import legacy_mirror_list_parity_ok
from .repositories.prediction_read_sqlalchemy import SqlAlchemyBrainPredictionReadRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PredictionReadOpsMeta:
    """Structured read-path outcome for Phase 6 observability (no API surface change)."""

    read: str
    snapshot_id: int | None = None
    line_count: int | None = None
    fp16: str = "none"


def phase5_apply_prediction_read(
    *,
    results: list[dict],
    ticker_batch: list[str],
    explicit_api_tickers: bool,
) -> tuple[list[dict], PredictionReadOpsMeta]:
    """After legacy `results` are final. Does not mutate prediction cache globals."""
    fp16 = universe_fingerprint_fp16(ticker_batch)
    cmp_on = getattr(settings, "brain_prediction_read_compare_enabled", False)
    auth_on = getattr(settings, "brain_prediction_read_authoritative_enabled", False)
    if not cmp_on and not auth_on:
        return results, PredictionReadOpsMeta(read=READ_NA, fp16=fp16)
    if not results:
        return results, PredictionReadOpsMeta(read=READ_NA, fp16=fp16)

    max_age = int(getattr(settings, "brain_prediction_read_max_age_seconds", 900))

    try:
        fp = prediction_universe_fingerprint(ticker_batch)
        from ...db import SessionLocal

        repo = SqlAlchemyBrainPredictionReadRepository()
        db = SessionLocal()
        try:
            sid = repo.fetch_latest_snapshot_id(db, universe_fingerprint=fp)
            if sid is None:
                if cmp_on:
                    logger.debug(
                        "[brain_prediction_read_compare] mirror_miss fingerprint_prefix=%s",
                        fp[:16],
                    )
                if auth_on and explicit_api_tickers and ticker_batch:
                    logger.debug("[brain_prediction_read_auth] mirror_miss fallback legacy")
                if cmp_on:
                    rd = READ_COMPARE_MISS
                elif auth_on and explicit_api_tickers and ticker_batch:
                    rd = READ_FALLBACK_MISS
                else:
                    rd = READ_NA
                return results, PredictionReadOpsMeta(read=rd, fp16=fp16)

            header = repo.fetch_snapshot_header(db, snapshot_id=sid)
            if not header:
                if cmp_on:
                    logger.debug("[brain_prediction_read_compare] mirror_miss no_header snapshot_id=%s", sid)
                if cmp_on:
                    rd = READ_COMPARE_MISS
                elif auth_on and explicit_api_tickers and ticker_batch:
                    rd = READ_FALLBACK_MISS
                else:
                    rd = READ_NA
                return results, PredictionReadOpsMeta(read=rd, snapshot_id=sid, fp16=fp16)

            lines = repo.fetch_lines_for_snapshot(db, snapshot_id=sid)
            if not lines:
                if cmp_on:
                    logger.debug(
                        "[brain_prediction_read_compare] mirror_miss empty_lines snapshot_id=%s",
                        sid,
                    )
                if auth_on and explicit_api_tickers and ticker_batch:
                    logger.debug("[brain_prediction_read_auth] empty_lines fallback legacy")
                if cmp_on:
                    rd = READ_COMPARE_MISS
                elif auth_on and explicit_api_tickers and ticker_batch:
                    rd = READ_FALLBACK_EMPTY
                else:
                    rd = READ_NA
                return results, PredictionReadOpsMeta(read=rd, snapshot_id=sid, line_count=0, fp16=fp16)

            mirror_rows = mirror_lines_to_legacy_rows(lines)
            line_count = len(lines)

            parity_ok, parity_reason = legacy_mirror_list_parity_ok(results, mirror_rows)
            if cmp_on and not parity_ok:
                logger.warning(
                    "[brain_prediction_read_compare] parity_mismatch reason=%s snapshot_id=%s",
                    parity_reason,
                    sid,
                )

            auth_allowed = auth_on and explicit_api_tickers and bool(ticker_batch)
            if not auth_allowed:
                if cmp_on:
                    rd = READ_COMPARE_OK if parity_ok else READ_COMPARE_MISMATCH
                elif auth_on:
                    rd = READ_FALLBACK_INELIGIBLE
                else:
                    rd = READ_NA
                return results, PredictionReadOpsMeta(
                    read=rd, snapshot_id=sid, line_count=line_count, fp16=fp16
                )

            now = datetime.utcnow()
            if header.as_of_ts is None or (now - header.as_of_ts) > timedelta(seconds=max_age):
                logger.debug(
                    "[brain_prediction_read_auth] stale_or_missing_asof fallback legacy snapshot_id=%s",
                    sid,
                )
                return results, PredictionReadOpsMeta(
                    read=READ_FALLBACK_STALE, snapshot_id=sid, line_count=line_count, fp16=fp16
                )

            if not parity_ok:
                logger.debug(
                    "[brain_prediction_read_auth] parity_fail fallback legacy snapshot_id=%s reason=%s",
                    sid,
                    parity_reason,
                )
                return results, PredictionReadOpsMeta(
                    read=READ_FALLBACK_PARITY, snapshot_id=sid, line_count=line_count, fp16=fp16
                )

            return mirror_rows, PredictionReadOpsMeta(
                read=READ_AUTH_MIRROR, snapshot_id=sid, line_count=line_count, fp16=fp16
            )
        finally:
            db.close()
    except Exception:
        logger.warning("[brain_prediction_read] unexpected_error fallback legacy", exc_info=True)
        return results, PredictionReadOpsMeta(read=READ_ERROR, fp16=fp16)
