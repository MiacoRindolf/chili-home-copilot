"""Phase 3 handler: event-driven quality_composite_score recompute.

Subscribes to ``backtest_completed`` (refreshes after the CPCV gate
writes ``cpcv_*`` fields) and the three trade-close events
(``live_trade_closed`` / ``paper_trade_closed`` / ``broker_fill_closed``,
which fan in via ``pattern_stats`` / ``demote`` / ``regime_ledger`` and
refresh ``win_rate`` / ``avg_return_pct`` / directional-WR inputs to the
composite).

The handler runs LAST in the dispatcher's per-event chain so the
upstream handlers have already committed their writes. It computes
``pattern_quality_score.compute_quality_composite_score`` for the
single affected pattern, writes only when the score differs from the
persisted value (idempotency contract from Phase 1b), and emits
``pattern_quality_recomputed`` as an outcome event so downstream
consumers (e.g. Phase 2's adaptive gate via shadow log) can observe
the recompute.

Failure containment matches ``pattern_stats.py``: inner exceptions are
swallowed at the handler boundary (logged + rollback) so the upstream
cpcv_gate / pattern_stats / regime_ledger commits aren't poisoned by a
broken composite.

Author: 2026-05-11 (f-composite-quality-event-driven, Phase 3).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

from app.services.trading.realized_pnl_sql import trade_return_fraction_sql

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:quality_score]"


def handle_backtest_completed_quality(
    db: "Session", ev: Any, user_id: int | None,
) -> None:
    """Refresh quality_composite_score after CPCV gate writes cpcv_*."""
    _recompute_for_event(ev, source="backtest_completed")


def handle_trade_closed_quality(
    db: "Session", ev: Any, user_id: int | None,
) -> None:
    """Refresh quality_composite_score after pattern_stats / regime_ledger
    writes win_rate / avg_return / directional-WR inputs."""
    _recompute_for_event(ev, source="trade_closed")


def _resolve_pattern_id(ev: Any) -> int | None:
    payload = getattr(ev, "payload", None)
    if not isinstance(payload, dict):
        return None
    raw = payload.get("scan_pattern_id")
    if raw is None:
        return None
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _recompute_for_event(ev: Any, *, source: str) -> None:
    """Open a fresh SessionLocal and recompute composite for the
    single pattern referenced in the event payload.

    trade-close outcome events sometimes omit ``scan_pattern_id`` (digest-style
    events). When absent, this handler short-circuits: composite
    recomputes happen per-pattern on the backtest_completed leg, and
    the per-user batch (run nightly) covers any patterns the per-event
    path misses.
    """
    pid = _resolve_pattern_id(ev)
    if pid is None:
        logger.debug(
            "%s ev_id=%s source=%s no scan_pattern_id in payload — skip",
            LOG_PREFIX, getattr(ev, "id", None), source,
        )
        return

    from app.db import SessionLocal

    sess = SessionLocal()
    try:
        result = _recompute_for_pattern(sess, pid, source=source, ev=ev)
        if result is None:
            return
        old_score, new_score, changed = result
        if changed:
            sess.commit()
            logger.info(
                "%s ev_id=%s source=%s pattern_id=%d old=%s new=%s — wrote",
                LOG_PREFIX, getattr(ev, "id", None), source, pid,
                _fmt_score(old_score), _fmt_score(new_score),
            )
        else:
            logger.debug(
                "%s ev_id=%s source=%s pattern_id=%d score=%s — no change",
                LOG_PREFIX, getattr(ev, "id", None), source, pid,
                _fmt_score(new_score),
            )
    except Exception as e:
        try:
            sess.rollback()
        except Exception:
            pass
        logger.warning(
            "%s ev_id=%s source=%s pattern_id=%s failed: %s",
            LOG_PREFIX, getattr(ev, "id", None), source, pid, e,
            exc_info=True,
        )
        # Swallow: composite scoring is informational. Upstream commits
        # must survive a broken recompute.
        return
    finally:
        try:
            sess.close()
        except Exception:
            pass


def _recompute_for_pattern(
    sess: "Session",
    pid: int,
    *,
    source: str,
    ev: Any,
) -> Optional[tuple[Optional[float], Optional[float], bool]]:
    """Compute, conditionally write, and emit. Returns
    ``(old_score, new_score, changed)`` or ``None`` if the pattern is
    missing / retired."""
    from app.models.trading import ScanPattern
    from app.services.trading.pattern_quality_score import (
        _resolve_weights,
        compute_quality_composite_score,
        realized_pnl_score as _realized_pnl_score,
    )

    pattern = sess.get(ScanPattern, pid)
    if pattern is None:
        logger.warning(
            "%s ev_id=%s source=%s pattern_id=%d not found",
            LOG_PREFIX, getattr(ev, "id", None), source, pid,
        )
        return None

    lifecycle = (getattr(pattern, "lifecycle_stage", "") or "").strip().lower()
    if lifecycle == "retired":
        logger.debug(
            "%s ev_id=%s source=%s pattern_id=%d lifecycle=retired — skip",
            LOG_PREFIX, getattr(ev, "id", None), source, pid,
        )
        return None

    from app.config import settings as _settings

    weights = _resolve_weights(_settings)

    directional_wr, sample_n = _load_directional_wr_for_pattern(sess, pid)
    decay = _load_decay_for_pattern(sess, pid)
    rp_n, rp_avg = _load_realized_pnl_for_pattern(
        sess, pid, int(weights.get("realized_window_days", 90)),
    )
    if rp_n >= 5 and rp_avg is not None:
        rp_score = _realized_pnl_score(
            rp_avg,
            float(weights.get("realized_pnl_normalizer_pct", 0.01)),
        )
    else:
        rp_score = None

    if sample_n < 30 or decay is None:
        new_score: Optional[float] = None
    else:
        new_score = compute_quality_composite_score(
            pattern, directional_wr, decay, weights,
            realized_pnl_score=rp_score,
            realized_n_trades=rp_n,
        )

    old_score = getattr(pattern, "quality_composite_score", None)
    if _scores_equal(old_score, new_score):
        return (old_score, new_score, False)

    pattern.quality_composite_score = new_score
    sess.flush()
    _emit_recomputed_outcome(
        sess,
        pid=pid,
        old_score=old_score,
        new_score=new_score,
        source=source,
        parent_event_id=getattr(ev, "id", None),
    )
    return (old_score, new_score, True)


def _scores_equal(a: Optional[float], b: Optional[float]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def _fmt_score(v: Optional[float]) -> str:
    if v is None:
        return "NULL"
    return f"{float(v):.4f}"


def _load_directional_wr_for_pattern(
    sess: "Session", pid: int,
) -> tuple[Optional[float], int]:
    """Per-pattern rolling-30 directional WR from
    ``pattern_directional_quality_v``. Returns ``(wr, sample_n)``;
    ``(None, 0)`` when the view has no row for the pattern."""
    from sqlalchemy import text as _text

    try:
        row = sess.execute(
            _text(
                "SELECT rolling_directional_wr, rolling_sample_n "
                "FROM pattern_directional_quality_v "
                "WHERE scan_pattern_id = :pid"
            ),
            {"pid": int(pid)},
        ).fetchone()
    except Exception as exc:
        logger.debug(
            "%s pattern_directional_quality_v read failed for pid=%d: %s",
            LOG_PREFIX, pid, exc,
        )
        return (None, 0)
    if row is None:
        return (None, 0)
    wr = float(row[0]) if row[0] is not None else None
    n = int(row[1]) if row[1] is not None else 0
    return (wr, n)


def _load_realized_pnl_for_pattern(
    sess: "Session", pid: int, window_days: int,
) -> tuple[int, Optional[float]]:
    """Per-pattern realized PnL stats over the trailing window.

    Returns ``(n_closed_trades, avg_pnl_pct)``. ``avg_pnl_pct`` is
    equal-weighted as ``avg(pnl / notional)`` across all
    ``status='closed'`` trades with non-NULL pnl in the window. Options
    include the 100x contract multiplier in notional. Returns
    ``(0, None)`` when the pattern has no closed trades or on read
    failure (NULL propagation per advisor brief §2.6 — no magic
    fallback).
    """
    from sqlalchemy import text as _text

    try:
        row = sess.execute(
            _text(f"""
                SELECT COUNT(*) AS n,
                       AVG({trade_return_fraction_sql()}) AS avg_pnl_pct
                FROM trading_management_envelopes
                WHERE scan_pattern_id = :pid
                  AND scan_pattern_id != -1
                  AND status = 'closed'
                  AND pnl IS NOT NULL
                  AND entry_price > 0
                  AND quantity > 0
                  AND exit_date > NOW() - make_interval(days => :window_days)
                """
            ),
            {"pid": int(pid), "window_days": int(window_days)},
        ).fetchone()
    except Exception as exc:
        logger.debug(
            "%s realized read failed for pid=%d: %s", LOG_PREFIX, pid, exc,
        )
        return (0, None)
    if row is None:
        return (0, None)
    n = int(row[0] or 0)
    avg = float(row[1]) if row[1] is not None else None
    return (n, avg)


def _load_decay_for_pattern(
    sess: "Session", pid: int,
) -> Optional[float]:
    """Per-pattern decay from the rolling-30 outcome split. Mirrors
    ``pattern_quality_score._load_decay_map`` but scoped to a single
    pattern. Returns ``None`` when fewer than 30 outcomes exist."""
    from sqlalchemy import text as _text

    try:
        row = sess.execute(
            _text(
                """
                WITH ranked AS (
                    SELECT directional_correct,
                           ROW_NUMBER() OVER (ORDER BY alert_at DESC) AS rn
                    FROM pattern_alert_directional_outcome
                    WHERE scan_pattern_id = :pid
                      AND directional_correct IS NOT NULL
                )
                SELECT
                    AVG(CASE WHEN rn <= 15 AND directional_correct THEN 1.0
                             WHEN rn <= 15 THEN 0.0 END) AS newer_wr,
                    AVG(CASE WHEN rn BETWEEN 16 AND 30 AND directional_correct THEN 1.0
                             WHEN rn BETWEEN 16 AND 30 THEN 0.0 END) AS older_wr,
                    COUNT(*) FILTER (WHERE rn <= 15) AS newer_n,
                    COUNT(*) FILTER (WHERE rn BETWEEN 16 AND 30) AS older_n
                FROM ranked
                WHERE rn <= 30
                """
            ),
            {"pid": int(pid)},
        ).fetchone()
    except Exception as exc:
        logger.debug(
            "%s decay query failed for pid=%d: %s", LOG_PREFIX, pid, exc,
        )
        return None
    if row is None:
        return None
    newer_wr = row[0]
    older_wr = row[1]
    newer_n = int(row[2] or 0)
    older_n = int(row[3] or 0)
    if newer_n != 15 or older_n != 15 or newer_wr is None or older_wr is None:
        return None
    return max(0.0, float(older_wr) - float(newer_wr))


def _emit_recomputed_outcome(
    sess: "Session",
    *,
    pid: int,
    old_score: Optional[float],
    new_score: Optional[float],
    source: str,
    parent_event_id: Any,
) -> None:
    """Enqueue a ``pattern_quality_recomputed`` outcome event so the
    score change is auditable and downstream consumers can subscribe.

    Phase 1b consult-gated decision: event_kind='outcome' (audit-of-fact,
    not task-to-do). Per-call dedupe key includes the rounded new score
    so distinct score values produce distinct rows; identical-score
    re-emissions (which only happen if the conditional-write path is
    bypassed) collapse to a single row."""
    from ..ledger import enqueue_outcome_event

    parent_id = None
    if parent_event_id is not None:
        try:
            parent_id = int(parent_event_id)
        except (TypeError, ValueError):
            parent_id = None

    new_s = _fmt_score(new_score)
    h = hashlib.sha256(
        f"{pid}|{new_s}|{source}|{parent_id or 0}".encode()
    ).hexdigest()[:16]
    dedupe_key = f"quality:p{pid}:{h}"
    payload: dict[str, Any] = {
        "scan_pattern_id": int(pid),
        "old_score": (float(old_score) if old_score is not None else None),
        "new_score": (float(new_score) if new_score is not None else None),
        "source": source,
        "recomputed_at": datetime.utcnow().isoformat(),
    }
    if parent_id is not None:
        payload["parent_work_event_id"] = parent_id

    try:
        enqueue_outcome_event(
            sess,
            event_type="pattern_quality_recomputed",
            dedupe_key=dedupe_key,
            payload=payload,
            parent_event_id=parent_id,
            claimable=False,
        )
    except Exception as exc:
        logger.debug(
            "%s emit pattern_quality_recomputed failed pid=%d: %s",
            LOG_PREFIX, pid, exc,
        )
