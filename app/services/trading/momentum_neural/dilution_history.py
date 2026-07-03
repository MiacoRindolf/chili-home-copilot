"""A10 (Ross CLRO-lesson 2026-07-02) — OWN-HEADLINE DILUTION-HISTORY MEMORY.

Ross has "written off" serial diluters ("many secondary offerings, many reverse splits …" —
WHLR-class). No corp-actions vendor exists, but ``catalyst.weak_catalyst_symbols`` already
flags dilution/compliance/legal symbols DAILY. This DB-aware helper:

  * PERSISTS each day's flagged symbols into ``momentum_dilution_history`` (one row per
    (symbol, observed_day); idempotent via ON CONFLICT DO NOTHING);
  * reads the trailing-window distinct-flag-day count per symbol and returns a DECAYING
    selection DERATE for a serial diluter (flagged on >= an ADAPTIVE K distinct days) — never a
    hard ban (the fresh reverse-split-squeeze carve-out still wins; the caller applies the derate
    AFTER the squeeze boost so a live squeeze always outranks the stale-diluter memory).

Adaptive basis (no magic numbers): the trailing WINDOW (calendar days) is the ONE documented
base (``chili_momentum_dilution_history_window_days``, default 90). K is ADAPTIVE within the
window — the median distinct-flag-day count across all symbols seen in the window (a symbol must
be flagged MORE than the typical name to count as a serial diluter), floored at 2 (a single
day is never "serial"). The derate DECAYS with recency: the more recent the last flag, the
stronger the derate; a flag near the window edge decays toward zero. Fail direction: NO history
(or any read error) => NO derate (0.0). Pure-of-side-effects on the read path; the persist path
is the only writer. docs/DESIGN/MOMENTUM_LANE.md; see [[project_momentum_lane]].
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from ....config import settings

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── A10 tilt bases (the only irreducible documented constants) ──────────────────────────────
# The MAX derate a serial diluter can receive (a symbol flagged every recent day). It DECAYS
# toward 0 with the recency of the last flag; it is a SOFT selection derate on the [0,1] score,
# never a veto. Mirrors the A8 REIT down-weight magnitude (viability.py:324, base -= 0.12).
_DILUTION_MAX_DERATE = 0.12
# The adaptive-K floor: a symbol flagged on fewer than this many distinct days in the window is
# never "serial" (a one-off offering is not a written-off diluter). K = max(this, median count).
_DILUTION_MIN_DISTINCT_DAYS = 2


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def persist_dilution_flags(
    db: "Session",
    symbols: Iterable[str],
    *,
    now_utc: datetime | None = None,
    flag_reason: str = "weak_catalyst",
    correlation_id: str | None = None,
    source_node_id: str | None = None,
) -> int:
    """Persist today's dilution/weak-flagged symbols into ``momentum_dilution_history`` (one row
    per (symbol, observed_day)). Idempotent: a repeated same-day write is a no-op (ON CONFLICT
    DO NOTHING on the UNIQUE(symbol, observed_day) constraint). Returns the number of NEW rows
    inserted. Fail-open: any error is swallowed (logged) and returns 0 — persistence is best-
    effort and must never break the viability tick."""
    if not bool(getattr(settings, "chili_momentum_dilution_history_derate_enabled", True)):
        return 0
    syms = sorted({str(s).strip().upper() for s in (symbols or []) if s and str(s).strip()})
    if not syms:
        return 0
    now = now_utc or _now_utc()
    observed_day = now.date()
    inserted = 0
    try:
        from sqlalchemy import text

        stmt = text(
            "INSERT INTO momentum_dilution_history "
            "(symbol, observed_day, observed_at, flag_reason, correlation_id, source_node_id) "
            "VALUES (:symbol, :observed_day, :observed_at, :flag_reason, :correlation_id, :source_node_id) "
            "ON CONFLICT (symbol, observed_day) DO NOTHING"
        )
        for sym in syms:
            res = db.execute(
                stmt,
                {
                    "symbol": sym,
                    "observed_day": observed_day,
                    "observed_at": now,
                    "flag_reason": flag_reason,
                    "correlation_id": correlation_id,
                    "source_node_id": source_node_id,
                },
            )
            # rowcount is 1 on a real insert, 0 on a conflict-skip.
            try:
                inserted += int(res.rowcount or 0)
            except Exception:
                pass
        db.commit()
    except Exception:
        logger.debug("[dilution_history] persist failed", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    if inserted:
        logger.info("[dilution_history] persisted %d new dilution-flag rows for %s", inserted, observed_day)
    return inserted


def dilution_history_derate(
    db: "Session | None",
    symbol: str,
    *,
    now_utc: datetime | None = None,
) -> float:
    """Return a DECAYING selection derate in [0, _DILUTION_MAX_DERATE] for a serial diluter.

    A symbol flagged on >= K distinct days in the trailing window (K adaptive = the max of the
    documented floor and the median distinct-flag-day count across all symbols in the window)
    earns a derate that scales with (a) HOW serial it is (distinct-day count vs K) and (b) the
    RECENCY of its last flag (a flag today derates full; a flag near the window edge decays to
    ~0). NEVER a hard ban. Fail direction: no history / read error / flag OFF => 0.0 (no derate).
    The caller subtracts this from the [0,1] score AFTER any fresh-squeeze boost, so a live
    reverse-split squeeze always outranks the stale-diluter memory."""
    if db is None:
        return 0.0
    if not bool(getattr(settings, "chili_momentum_dilution_history_derate_enabled", True)):
        return 0.0
    sym = str(symbol or "").strip().upper()
    if not sym or "-USD" in sym:  # crypto has no dilution class
        return 0.0
    window_days = int(getattr(settings, "chili_momentum_dilution_history_window_days", 90) or 90)
    if window_days < 1:
        return 0.0
    now = now_utc or _now_utc()
    cutoff_day = (now - timedelta(days=window_days)).date()
    try:
        from sqlalchemy import text

        # (1) this symbol's distinct flag-day count + most-recent flag day in the window.
        row = db.execute(
            text(
                "SELECT COUNT(DISTINCT observed_day) AS n_days, MAX(observed_day) AS last_day "
                "FROM momentum_dilution_history "
                "WHERE symbol = :symbol AND observed_day >= :cutoff"
            ),
            {"symbol": sym, "cutoff": cutoff_day},
        ).first()
        n_days = int(row[0] or 0) if row is not None else 0
        last_day = row[1] if row is not None else None
        if n_days <= 0 or last_day is None:
            return 0.0

        # (2) adaptive K = max(floor, median distinct-flag-day count across all symbols in the
        #     window). A serial diluter must be flagged MORE than the typical flagged name.
        med_row = db.execute(
            text(
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d) FROM ("
                " SELECT symbol, COUNT(DISTINCT observed_day) AS d"
                " FROM momentum_dilution_history WHERE observed_day >= :cutoff"
                " GROUP BY symbol"
                ") s"
            ),
            {"cutoff": cutoff_day},
        ).first()
        median_days = float(med_row[0]) if (med_row is not None and med_row[0] is not None) else 0.0
        k = max(float(_DILUTION_MIN_DISTINCT_DAYS), median_days)
        if n_days < k:
            return 0.0  # not serial enough vs the population — no derate

        # (3) severity: how far past K (bounded so a hyper-diluter never blows past the cap).
        severity = min(1.0, (n_days - k + 1.0) / max(1.0, k))

        # (4) recency decay: a flag today decays full; a flag near the window edge decays to ~0.
        try:
            days_since_last = max(0, (now.date() - last_day).days)
        except Exception:
            days_since_last = 0
        recency = max(0.0, 1.0 - (days_since_last / float(window_days)))

        derate = _DILUTION_MAX_DERATE * severity * recency
        return float(max(0.0, min(_DILUTION_MAX_DERATE, derate)))
    except Exception:
        logger.debug("[dilution_history] derate read failed for %s", sym, exc_info=True)
        return 0.0
