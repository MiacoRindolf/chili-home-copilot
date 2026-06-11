"""Nightly replay regression — the tripwire that turns the Replay Lab from a
microscope into a CI gate.

Every evening after the data session closes, re-run TODAY's session through the
replay engine on TONIGHT'S code (live-armed mode: same sessions the live lane
actually armed) and diff it against what the live lane actually did. Drift means
either (a) today's code changes altered entry/exit behavior (catch it tonight,
not at tomorrow's open — the 2026-06-11 morning "may sinira ka" incident), or
(b) live execution diverged from its own decision logic (fills, gates, infra).

Report = REPLAY_RESULTS_DIR/regression-<date>.json + one log line per flag.
Best-effort everywhere: a regression failure must never affect anything else.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def _live_actuals(db: Session, date_et: str) -> dict[str, Any]:
    """What the LIVE lane actually did today: fills, exits, realized PnL, and the
    block-reason mix (the decision surface the replay must reproduce)."""
    out: dict[str, Any] = {"fills": 0, "symbols": [], "realized_usd": None, "top_blocks": []}
    try:
        rows = db.execute(text(
            "SELECT s.symbol, count(*) FROM trading_automation_events e "
            "JOIN trading_automation_sessions s ON s.id = e.session_id "
            "WHERE e.event_type = 'live_entry_filled' AND s.mode = 'live' "
            "AND s.symbol NOT LIKE '%-USD' "
            "AND (e.ts AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date = :d "
            "GROUP BY s.symbol"
        ), {"d": date_et}).fetchall()
        out["fills"] = int(sum(r[1] for r in rows))
        out["symbols"] = sorted({str(r[0]) for r in rows})
    except Exception:
        logger.debug("[replay_regression] live fills read failed", exc_info=True)
    try:
        row = db.execute(text(
            "SELECT coalesce(sum((exit_price - entry_price) * quantity), 0) "
            "FROM trading_trades WHERE status = 'closed' AND ticker NOT LIKE '%-USD' "
            "AND (updated_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date = :d"
        ), {"d": date_et}).fetchone()
        out["realized_usd"] = round(float(row[0] or 0.0), 2) if row else None
    except Exception:
        logger.debug("[replay_regression] realized read failed", exc_info=True)
    try:
        rows = db.execute(text(
            "SELECT coalesce(payload_json->>'reason', '?'), count(*) "
            "FROM trading_automation_events "
            "WHERE event_type IN ('live_entry_trigger_wait', 'live_blocked_by_risk') "
            "AND (ts AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date = :d "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
        ), {"d": date_et}).fetchall()
        out["top_blocks"] = [{"reason": str(r[0]), "n": int(r[1])} for r in rows]
    except Exception:
        logger.debug("[replay_regression] block mix read failed", exc_info=True)
    return out


def run_nightly_replay_regression(db: Session) -> dict[str, Any]:
    """Run today's live-armed replay on current code, diff vs live actuals,
    persist the report, and WARN on every tripped flag. Returns the report."""
    date_et = datetime.now(_ET).strftime("%Y-%m-%d")
    report: dict[str, Any] = {"date": date_et, "ran_at_utc": datetime.utcnow().isoformat()}
    try:
        from .replay_v2 import REPLAY_RESULTS_DIR, run_replay

        res = run_replay(date_et, persist=True, armed_source="live") or {}
    except Exception as exc:
        report["error"] = f"replay_failed: {str(exc)[:200]}"
        logger.warning("[replay_regression] REPLAY FAILED for %s: %s", date_et, exc)
        return report

    trades = res.get("trades") or []
    live = _live_actuals(db, date_et)
    div = res.get("divergence") or []
    report.update({
        "replay": {
            "trades": len(trades),
            "total_usd": res.get("total_usd"),
            "symbols": sorted({str(t.get("sym")) for t in trades}),
            "error": res.get("error"),
            "live_sessions": res.get("live_sessions"),
            "tape_symbols": res.get("tape_symbols"),
        },
        "live": live,
        "divergence_rows": len(div) if isinstance(div, list) else None,
    })

    # ── Tripwires: each flag is a reason to look BEFORE the next open ──────
    flags: list[str] = []
    if res.get("error"):
        flags.append(f"replay_error:{res.get('error')}")
    live_fills = int(live.get("fills") or 0)
    if live_fills > 0 and not trades:
        flags.append("replay_blind: live filled but the replay took ZERO trades — entry logic drifted or tape is broken")
    if trades and (res.get("tape_symbols") or 0) < 5:
        flags.append("thin_tape: replay ran on <5 tape symbols — sampler coverage problem")
    try:
        r_tot = float(res.get("total_usd") or 0.0)
        l_tot = float(live.get("realized_usd") or 0.0)
        if live_fills > 0 and abs(r_tot - l_tot) > max(200.0, abs(l_tot) * 1.0):
            flags.append(
                f"pnl_drift: replay ${r_tot:+.0f} vs live ${l_tot:+.0f} — decision or fill model diverged"
            )
    except (TypeError, ValueError):
        pass
    report["flags"] = flags

    try:
        os.makedirs(REPLAY_RESULTS_DIR, exist_ok=True)
        path = os.path.join(REPLAY_RESULTS_DIR, f"regression-{date_et}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, default=str)
        report["path"] = path
    except Exception:
        logger.debug("[replay_regression] persist failed", exc_info=True)

    if flags:
        for fl in flags:
            logger.warning("[replay_regression] TRIPWIRE %s: %s", date_et, fl)
    else:
        logger.info(
            "[replay_regression] %s clean: replay %d trades $%s vs live %d fills $%s",
            date_et, len(trades), res.get("total_usd"), live_fills, live.get("realized_usd"),
        )
    return report
