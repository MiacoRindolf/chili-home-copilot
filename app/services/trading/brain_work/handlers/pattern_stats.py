"""Phase 2 handler: pattern-evidence recompute on trade close.

Subscribes to ``paper_trade_closed`` / ``live_trade_closed`` /
``broker_fill_closed``. For each event, calls
``learning.update_pattern_stats_from_closed_trades`` for the closed
trade's user. The function is the canonical-aware writer (mig 228) --
it re-derives ``ScanPattern.{win_rate, avg_return_pct, trade_count}``
using counterfactual exit prices for trades that held past their
intended ``max_bars`` and writes one ``pattern_evidence_corrections``
audit row per pattern processed.

Author: 2026-05-05 (f-handler-pattern-stats). Sixth Phase 2 handler.

Design rules:
  * **Per-event, NOT per-trade-affected.** The function buckets all of
    a user's recent (180-day) closed trades by pattern internally, so
    calling it once per close-event handles all patterns the close
    touched.
  * **Idempotent.** The function writes ``correction_reason='no_change'``
    audit rows when recompute matches existing stats. Repeated
    invocations don't drift.
  * **Coverage gate enforced inside the function** (not by the
    handler) -- when >50% of overheld trades have no counterfactual
    OHLCV, the function records ``coverage_too_thin`` and skips the
    field update.
  * **Failures swallowed at the handler boundary** so a broken
    pattern's recompute can't poison subsequent events. The
    dispatcher will mark this event as done either way; a transient
    failure is logged and we move on (next close event re-runs the
    aggregation).
  * **Fresh SessionLocal** -- mirror of ``demote.py``'s pattern.
    Isolates the recompute's internal commits from the dispatcher's
    transaction, matching the f-evidence-canonical-writer commit's
    intent (each pattern's audit row commits atomically).
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:pattern_stats]"


def handle_paper_trade_closed(db: "Session", ev: Any, user_id: int | None) -> None:
    """Handler entry for ``paper_trade_closed`` events."""
    _run_pattern_stats_recompute(ev, user_id, source="paper")


def handle_live_trade_closed(db: "Session", ev: Any, user_id: int | None) -> None:
    """Handler entry for ``live_trade_closed`` events."""
    _run_pattern_stats_recompute(ev, user_id, source="live")


def handle_broker_fill_closed(db: "Session", ev: Any, user_id: int | None) -> None:
    """Handler entry for ``broker_fill_closed`` events."""
    _run_pattern_stats_recompute(ev, user_id, source="broker")


def _run_pattern_stats_recompute(
    ev: Any,
    user_id: int | None,
    *,
    source: str,
) -> None:
    """Open a fresh SessionLocal and run the canonical recompute.

    The recompute function commits per-pattern internally; using a
    fresh session here keeps the dispatcher's transaction free of the
    recompute's writes (which can be many rows on a first-run backfill).
    """
    # Absolute imports avoid the dot-depth bug class -- the relative
    # ``....db`` would resolve to ``app.services.db`` (doesn't exist),
    # not ``app.db``. ``....learning`` would also miss
    # ``app.services.trading.learning``. Absolute is unambiguous.
    from app.db import SessionLocal
    from app.services.trading.learning import (
        update_pattern_stats_from_closed_trades,
    )

    # Some events carry a user_id in the payload; prefer that over the
    # dispatcher's user_id arg (which is usually None for system-emitted
    # events). Falls through to the arg when payload is absent.
    payload = getattr(ev, "payload", None)
    if isinstance(payload, dict):
        payload_uid = payload.get("user_id")
        if payload_uid is not None:
            try:
                user_id = int(payload_uid)
            except (TypeError, ValueError):
                pass

    sess = SessionLocal()
    try:
        result = update_pattern_stats_from_closed_trades(sess, user_id)
        logger.info(
            "%s source=%s event_id=%s user_id=%s "
            "patterns_updated=%d cycle_run_id=%s",
            LOG_PREFIX,
            source,
            getattr(ev, "id", None),
            user_id,
            int(result.get("patterns_updated", 0) or 0),
            result.get("cycle_run_id"),
        )
    except Exception as e:
        logger.exception(
            "%s source=%s event_id=%s failed: %s",
            LOG_PREFIX,
            source,
            getattr(ev, "id", None),
            e,
        )
    finally:
        try:
            sess.close()
        except Exception:
            pass
