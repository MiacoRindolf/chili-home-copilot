"""Daily realized-EV demote pass — CLEAN-WINDOW edition.

Operator audit 2026-04-29 (third-pass) Finding B-1 found promoted patterns
that were never validated against realized PnL. This pass re-checks every
``lifecycle_stage='promoted'`` pattern so promotion is not a one-way ratchet.

2026-06-05 rebuild (clean-window). The earlier version re-applied
:func:`realized_ev_gate.check_realized_ev_blocking` directly. Two problems made
its demote verdict NOT apples-to-apples:

1. **Pre-floor churn.** The live execution system churned heavily through early
   2026 (constantly-changing algo-trader, execution discrepancies, gate/quality
   drift). Realized PnL before the ``chili_realized_ev_clean_window_since``
   instrumentation floor is not comparable to current behaviour, so demoting on
   a window that includes it punishes patterns for a regime that no longer
   exists.
2. **Conflated + paper-inflated signals.** The old ``n = trade_count`` evidence
   check read the CONFLATED legacy column (overwritten by mining/backtests too),
   and the gate's raw-realized fallback folds in idealized paper/shadow fills —
   neither is the right basis for demoting a *live* promoted pattern.

This pass now judges each promoted pattern on its **representative post-floor
clean LIVE realized EV only**:

* CLEAN: dirty reconcile / sync-gone / position-gone placeholder exits excluded
  (``clean_live_pattern_ev_exit_filter_sql``).
* LIVE: ``trading_trades`` only — paper/shadow excluded (idealized fills must
  not keep a live-losing pattern promoted, nor demote one on idealized data).
* POST-FLOOR: ``exit_date >= chili_realized_ev_clean_window_since``.
* REPRESENTATIVE: at least ``chili_realized_ev_clean_window_min_trades`` such
  trades spanning at least ``chili_realized_ev_clean_window_min_days`` days.

Demote ONLY when representative post-floor live evidence exists AND is
net-negative (mean return <= floor OR win-rate <= floor). Patterns that are
data-starved or thin in the trustworthy window are KEPT — never demoted on
pre-floor noise. The post-floor crypto/equity coverage asymmetry (equity is
data-starved) makes this guard load-bearing; without it the pass would wrongly
cull equity supply.

Settle window: anchored on ``lifecycle_changed_at`` (set at promotion time),
NOT ``updated_at`` (bumped by any write — recomputes, migrations, manual
repairs — which silently reset the settle clock). Never-traded backtest-promoted
patterns are handled by the separate ``stale_promoted_sweep`` cron, not here.

All thresholds come from settings; none are magic fallbacks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _clean_window_live_ev(
    db: Session, pattern_ids: list[int], *, since: str
) -> dict[int, dict[str, Any]]:
    """Post-floor (>= ``since``) CLEAN LIVE realized EV per pattern.

    Paper/shadow rows are deliberately excluded — demoting a live promoted
    pattern must rest on live realized evidence in the trustworthy window, not
    idealized-fill paper. Uses the same partial-aware, sign-correct return
    fraction (``trade_return_fraction_sql``) and dirty-exit filter
    (``clean_live_pattern_ev_exit_filter_sql``) as the raw-realized writer, so
    the EV definition matches the rest of the realized-EV stack.

    Returns ``{pattern_id: {n, avg_ret_pct, win_rate, span_days}}`` for patterns
    with >= 1 qualifying trade.
    """
    from .realized_pnl_sql import (
        clean_live_pattern_ev_exit_filter_sql,
        trade_return_fraction_sql,
    )

    if not pattern_ids:
        return {}

    sql = text(
        f"""
        SELECT scan_pattern_id AS pid,
               count(frac) AS n,
               avg(frac * 100.0) AS avg_ret_pct,
               sum(CASE WHEN frac > 0 THEN 1 ELSE 0 END) AS wins,
               min(exit_date) AS mind,
               max(exit_date) AS maxd
          FROM (
            SELECT scan_pattern_id,
                   exit_date,
                   {trade_return_fraction_sql()} AS frac
              FROM trading_trades
             WHERE status = 'closed'
               AND scan_pattern_id = ANY(:pids)
               AND pnl IS NOT NULL
               AND entry_price > 0
               AND quantity > 0
               AND exit_date IS NOT NULL
               AND exit_date >= :since
               AND {clean_live_pattern_ev_exit_filter_sql()}
          ) s
         GROUP BY scan_pattern_id
        """
    )
    out: dict[int, dict[str, Any]] = {}
    for row in db.execute(sql, {"pids": list(pattern_ids), "since": since}).fetchall():
        n = int(row.n or 0)
        wins = int(row.wins or 0)
        span_days = 0
        if row.mind is not None and row.maxd is not None:
            try:
                span_days = int((row.maxd - row.mind).days)
            except Exception:
                span_days = 0
        out[int(row.pid)] = {
            "n": n,
            "avg_ret_pct": float(row.avg_ret_pct) if row.avg_ret_pct is not None else None,
            "win_rate": (wins / n) if n > 0 else None,
            "span_days": span_days,
        }
    return out


def run_realized_ev_demote_pass(db: Session) -> dict[str, Any]:
    """Demote promoted patterns whose representative post-floor live EV is net-negative.

    Returns a summary dict (back-compatible keys retained; ``demoted_failing_gate``
    now means 'demoted on representative post-floor clean-window net-negative
    live EV', and ``demoted_no_evidence_after_settle`` is always 0 — that case is
    owned by ``stale_promoted_sweep``)::

        {
          "evaluated": int,
          "demoted_failing_gate": int,
          "demoted_no_evidence_after_settle": int,   # always 0 here
          "kept_within_settle_window": int,
          "kept_unrepresentative_clean_window": int,
          "kept_passing_gate": int,
          "skipped_disabled": bool,
          "clean_window_since": str,
          "demoted_pattern_ids": [int, ...],
        }
    """
    from ...models.trading import ScanPattern

    enabled = bool(_settings_get("chili_realized_ev_demote_pass_enabled", True))
    since = str(_settings_get("chili_realized_ev_clean_window_since", "2026-05-22"))
    base = {
        "evaluated": 0,
        "demoted_failing_gate": 0,
        "demoted_no_evidence_after_settle": 0,
        "kept_within_settle_window": 0,
        "kept_unrepresentative_clean_window": 0,
        "kept_passing_gate": 0,
        "skipped_disabled": not enabled,
        "clean_window_since": since,
        "demoted_pattern_ids": [],
    }
    if not enabled:
        return base

    settle_days = int(_settings_get("chili_realized_ev_demote_settle_days", 14))
    min_trades = int(_settings_get("chili_realized_ev_clean_window_min_trades", 5))
    min_days = int(_settings_get("chili_realized_ev_clean_window_min_days", 5))
    min_ret = float(_settings_get("chili_realized_ev_min_avg_return_pct", 0.0))
    min_wr = float(_settings_get("chili_realized_ev_min_win_rate", 0.0))
    settle_cutoff = datetime.utcnow() - timedelta(days=settle_days)

    promoted = (
        db.query(ScanPattern)
        .filter(ScanPattern.lifecycle_stage == "promoted")
        .all()
    )
    cw = _clean_window_live_ev(db, [int(p.id) for p in promoted], since=since)

    demoted = 0
    kept_within_settle = 0
    kept_unrepresentative = 0
    kept_passing = 0
    demoted_ids: list[int] = []
    reason_updates: list[tuple[int, str]] = []

    for p in promoted:
        base["evaluated"] += 1

        # Settle-in window — anchored on the promotion-time lifecycle change,
        # NOT updated_at (which any write resets). Give a freshly-promoted
        # pattern the configured days to accumulate post-floor evidence.
        # Anchor on lifecycle_changed_at (set at promotion), then created_at (a
        # stable, NON-NULL lower bound) -- NEVER updated_at, which any write
        # (recompute / migration / stats refresh) bumps, silently restarting the
        # settle clock. (promoted_at is not a ScanPattern column.)
        anchor = (
            getattr(p, "lifecycle_changed_at", None)
            or getattr(p, "created_at", None)
            or datetime.utcnow()
        )
        if anchor >= settle_cutoff:
            kept_within_settle += 1
            continue

        s = cw.get(int(p.id)) or {"n": 0, "avg_ret_pct": None, "win_rate": None, "span_days": 0}

        # Representativeness guard: too few / too short a span of post-floor live
        # trades to judge -> KEEP (data-starved or still proving; never demote on
        # pre-floor churn). This is what protects data-starved supply (equity).
        if s["n"] < min_trades or s["span_days"] < min_days:
            kept_unrepresentative += 1
            continue

        net_negative = (
            (s["avg_ret_pct"] is not None and float(s["avg_ret_pct"]) <= min_ret)
            or (s["win_rate"] is not None and float(s["win_rate"]) <= min_wr)
        )
        if not net_negative:
            kept_passing += 1
            continue

        # Representative post-floor clean LIVE evidence, net-negative -> demote.
        p.lifecycle_stage = "challenged"
        p.promotion_status = "demote_clean_window_realized_ev"[:30]
        _avg = s["avg_ret_pct"] if s["avg_ret_pct"] is not None else 0.0
        reason = (
            f"realized_ev_demote_pass(clean-window>={since}) "
            f"{datetime.utcnow().isoformat(timespec='seconds')}: "
            f"post_floor_live n={s['n']} avg_ret_pct={_avg:.3f} "
            f"win_rate={(s['win_rate'] or 0.0):.3f} span_days={s['span_days']} "
            f"(representative & net-negative; min_n={min_trades} min_days={min_days})"
        )
        now = datetime.utcnow()
        p.lifecycle_changed_at = now
        p.updated_at = now
        demoted += 1
        demoted_ids.append(int(p.id))
        reason_updates.append((int(p.id), reason))
        logger.warning(
            "[realized_ev_demote_pass] DEMOTE id=%s name=%s post_floor_live_n=%s "
            "avg_ret_pct=%.3f win_rate=%.3f span_days=%s",
            p.id, getattr(p, "name", "?"), s["n"],
            (s["avg_ret_pct"] or 0.0), (s["win_rate"] or 0.0), s["span_days"],
        )

    db.commit()

    # promotion_demote_reason is NOT a mapped ScanPattern column (it exists in the
    # DB only, via migration) -- ORM attribute assignment is silently discarded at
    # flush, exactly as run_thin_evidence_demote documents. Persist the audit
    # reason via raw SQL after the lifecycle commit (append to any prior reason).
    for _pid, _reason in reason_updates:
        try:
            _row = db.execute(
                text("SELECT promotion_demote_reason FROM scan_patterns WHERE id = :pid"),
                {"pid": _pid},
            ).first()
            _existing = (_row[0] if _row and _row[0] else "") or ""
            db.execute(
                text("UPDATE scan_patterns SET promotion_demote_reason = :r WHERE id = :pid"),
                {"r": (_existing + "\n" + _reason).strip()[:2000], "pid": _pid},
            )
        except Exception:
            logger.warning(
                "[realized_ev_demote_pass] failed to persist demote reason for pattern %s", _pid
            )
    if reason_updates:
        db.commit()

    base["demoted_failing_gate"] = demoted
    base["kept_within_settle_window"] = kept_within_settle
    base["kept_unrepresentative_clean_window"] = kept_unrepresentative
    base["kept_passing_gate"] = kept_passing
    base["demoted_pattern_ids"] = demoted_ids
    logger.info("[realized_ev_demote_pass] %s", base)
    return base
