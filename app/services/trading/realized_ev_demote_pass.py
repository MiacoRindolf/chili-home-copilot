"""Daily realized-EV demote pass.

Operator audit 2026-04-29 (third-pass) Finding B-1 found that 10 of 12
``lifecycle_stage='promoted'`` patterns had ``trade_count=0`` (promoted
on backtest evidence by mig 197/199, never validated against realized
PnL). Pattern 860 was worse: WR=0.0 / avg_return_pct=0.0 / n=2 — failing
the EV gate but still ``promoted`` because the gate is only consulted
at *promotion time*, not periodically.

This module re-applies :func:`realized_ev_gate.evaluate_realized_ev` to
every currently-promoted pattern and demotes any that now fail. It is
meant to run as a scheduled daily job (registered in
:mod:`trading_scheduler`) so that promoted patterns must keep proving
themselves on realized data.

**Per the operator's no-hardcoded-fallback principle**:

* The ``min_settled_age_days`` threshold (default 14) is NOT a magic
  number used as a missing-measurement fallback — it's a settle-in
  window after promotion during which we deliberately do not demote.
  Documented inline as such; pulled from settings so operator can
  tune.
* The ``min_realized_n`` threshold reuses
  ``chili_realized_ev_min_trades`` (5) which is the same setting the
  promotion-time gate uses; never a separate magic constant.
* When a pattern has zero realized trades AND has been promoted for
  longer than the settle-in window, the pass demotes it for "no
  evidence after settle window" — not on a fabricated default WR/return.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .realized_ev_gate import check_realized_ev_blocking

logger = logging.getLogger(__name__)


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def run_realized_ev_demote_pass(db: Session) -> dict[str, Any]:
    """Re-evaluate every promoted pattern against the realized-EV gate.

    Returns a summary dict::

        {
          "evaluated": int,
          "demoted_failing_gate": int,
          "demoted_no_evidence_after_settle": int,
          "kept_within_settle_window": int,
          "kept_passing_gate": int,
          "skipped_disabled": bool,
          "demoted_pattern_ids": [int, ...],
        }
    """
    from ...models.trading import ScanPattern

    enabled = bool(_settings_get("chili_realized_ev_demote_pass_enabled", True))
    if not enabled:
        return {
            "evaluated": 0,
            "demoted_failing_gate": 0,
            "demoted_no_evidence_after_settle": 0,
            "kept_within_settle_window": 0,
            "kept_passing_gate": 0,
            "skipped_disabled": True,
            "demoted_pattern_ids": [],
        }

    settle_days = int(_settings_get("chili_realized_ev_demote_settle_days", 14))
    settle_cutoff = datetime.utcnow() - timedelta(days=settle_days)

    promoted = (
        db.query(ScanPattern)
        .filter(ScanPattern.lifecycle_stage == "promoted")
        .all()
    )

    evaluated = 0
    demoted_failing_gate = 0
    demoted_no_evidence = 0
    kept_within_settle = 0
    kept_passing = 0
    demoted_ids: list[int] = []

    for p in promoted:
        evaluated += 1

        # Settle-in window: don't demote a pattern that was just promoted —
        # give it the configured number of days to accumulate evidence.
        try:
            updated_at = p.updated_at or datetime.utcnow()
        except AttributeError:
            updated_at = datetime.utcnow()
        if updated_at >= settle_cutoff:
            kept_within_settle += 1
            continue

        blocked, reasons, snapshot = check_realized_ev_blocking(p)
        if blocked:
            # Distinguish "no evidence at all after settle window" from
            # "evidence exists and fails the gate" so the demote reason
            # row tells operators which case to address.
            n = int(getattr(p, "trade_count", 0) or 0)
            if n == 0:
                kind = "demote_no_evidence_after_settle"
                demoted_no_evidence += 1
            else:
                kind = "demote_failing_realized_ev_gate"
                demoted_failing_gate += 1

            p.lifecycle_stage = "challenged"
            p.promotion_status = (kind[:30])  # promotion_status is varchar(32)
            existing_reason = getattr(p, "promotion_demote_reason", None) or ""
            new_reason = (
                f"realized_ev_demote_pass {datetime.utcnow().isoformat(timespec='seconds')}: "
                f"reasons={','.join(reasons)} snapshot={snapshot}"
            )
            p.promotion_demote_reason = (
                (existing_reason + "\n" + new_reason).strip()[:2000]
            )
            p.updated_at = datetime.utcnow()
            demoted_ids.append(int(p.id))

            logger.warning(
                "[realized_ev_demote_pass] DEMOTE id=%s name=%s kind=%s reasons=%s",
                p.id, getattr(p, "name", "?"), kind, reasons,
            )
        else:
            kept_passing += 1

    db.commit()

    summary = {
        "evaluated": evaluated,
        "demoted_failing_gate": demoted_failing_gate,
        "demoted_no_evidence_after_settle": demoted_no_evidence,
        "kept_within_settle_window": kept_within_settle,
        "kept_passing_gate": kept_passing,
        "skipped_disabled": False,
        "demoted_pattern_ids": demoted_ids,
    }
    logger.info("[realized_ev_demote_pass] %s", summary)
    return summary
