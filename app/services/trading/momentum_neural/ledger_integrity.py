"""Three-way ledger integrity check for the live momentum lane.

WHY THIS EXISTS
---------------
On 2026-09-02 an analysis of ``momentum_automation_outcomes`` found four booked
rows for a twenty-one day window, said out loud that the books were incomplete,
and then reasoned from those four rows anyway — concluding that no trade had ever
reached real profit and given it back. The operator had personally watched SSM do
exactly that. The truth was in ``trading_automation_events`` all along: twelve
``momentum_mfe_realized`` records, including SSM 19315 at peak +3.20R / realized
−1.60R and AUUD 19337 at 0.00 / −6.729R. Every downstream study built on the
outcomes table inherited that hole.

The measured hole was four times larger than that premise. Over
2026-08-12..2026-09-02 the Alpaca account filled **26** live sessions. Three were
booked correctly, one was booked with a P&L that disagreed with the broker, and
**22 were never booked at all** — not null-P&L rows, zero rows. Broker truth was
−$1,211.14; the outcomes table totalled −$186.03. **84.6% of realised P&L was
invisible to the books**, including the single largest loss of the window (MOVE
19244, −$364.14, which left no DB trace whatsoever) and both of the only two
profitable exits, whose absence is what made a losing-only tape look total.

THE STRUCTURAL POINT: THE SOURCES ARE STRICTLY NESTED
----------------------------------------------------
The three in-DB sources do not disagree pairwise and randomly. Each is a strict
subset of the next::

    outcomes-with-P&L (4) ⊂ momentum_mfe_realized (12)
                          ⊂ momentum_fill_outcomes (18)
                          ⊂ broker fills (26)

No source ever over-reports. That is the signature of drop-out at successive
stages, and it means the ledger's error is one-directional: it always understates
loss. It also means **no amount of cross-checking DB tables against each other
can find the outermost ring** — nine sessions were filled by Alpaca with no fill
event, no fill leg and no outcome row, invisible to all three DB sources
simultaneously. Only the broker can see them. That is why ``include_broker`` is
the default and why a DB-only run is explicitly reported as partial coverage.

WHY THIS IS NOT ANOTHER SWEEPER
-------------------------------
``outcome_reconcile.reconcile_momentum_outcomes_to_broker_truth`` anchors on
``db.query(MomentumAutomationOutcome, TradingAutomationSession).join(...)`` — an
INNER join whose driving table is the outcomes table. A filled session with no
outcome row is out of its scope permanently, by construction. It can correct rows
that exist; it can never create the 22 that are missing. This check is anchored on
the **broker**, so a session CHILI has no row for, no leg for and no event for is
still visible to it.

It also does not depend on any session ever transitioning. Terminal states in this
lane are demonstrably written by processes outside the application (all 22 unbooked
sessions have ``ended_at IS NULL``; there is no ``live_arm_expired`` event anywhere
in the window; ``live_error`` booked 2,072/2,072 in June and 0/39 in August), so a
guard that only fires on an in-process transition cannot be the whole answer. The
structural guards in ``feedback_emit`` / ``outcome_extract`` close the in-process
paths; this check is what catches everything that never went through them.

READ-ONLY. This module never writes. It reports, and the caller decides.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import (
    MomentumAutomationOutcome,
    MomentumFillOutcome,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from .outcome_reconcile import _SESSION_CID_RE

_log = logging.getLogger(__name__)

# Every filled broker order in the audited window carried fees_usd 0.00 (Alpaca
# paper). A cent of tolerance absorbs float noise in the price/qty product without
# ever hiding a real divergence — the smallest genuine gap measured was $1.53.
PNL_TOLERANCE_USD = 0.01

STATUS_CLEAN = "booked_clean"
STATUS_NEVER_FILLED = "never_filled"
STATUS_FILLED_NEVER_BOOKED = "filled_never_booked"
STATUS_PNL_DISAGREES = "booked_pnl_disagrees_with_broker"
STATUS_BOOKED_NO_BROKER_FILL = "booked_without_broker_fill"
STATUS_UNRECONCILED_ENTRY = "booked_entry_evidence_unreconciled"

# Classes that mean the books and reality have parted company. A non-empty
# intersection with these is what makes the check fail.
VIOLATION_STATUSES = frozenset(
    {
        STATUS_FILLED_NEVER_BOOKED,
        STATUS_PNL_DISAGREES,
        STATUS_BOOKED_NO_BROKER_FILL,
        STATUS_UNRECONCILED_ENTRY,
    }
)


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _session_id_from_client_order_id(coid: Any) -> Optional[int]:
    """Session id carried by a CHILI client_order_id, or None.

    Attribution is carried by the order THE BROKER holds, not by anything CHILI
    stores — which is the whole point: a session whose local envelope lost its
    order id is exactly the shape this module exists to find.
    """
    text = str(coid or "").strip()
    if not text:
        return None
    match = _SESSION_CID_RE.match(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _order_time(order: Any) -> str:
    return str(getattr(order, "created_time", "") or "")


def _build_broker_episodes(orders: list[Any]) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Walk broker fills per symbol into closed round trips, attributed by entry.

    Returns ``(by_session, unattributed)``. An episode opens on a BUY and closes
    when net quantity returns to zero; its session is taken from the BUY's
    ``client_order_id``. Exits are deliberately NOT required to carry a session id:
    14 of the 26 filled sessions in the audited window were closed by something
    outside the FSM (``chili_operator_flatten_*``, ``chili_orphan_flatten_*``,
    ``chili_eod_flatten_*``, ``chili_unmanaged_flatten_*``, ``chili_supervisor_eod_*``,
    or a bare Alpaca-assigned UUID), and six filled exits carry no CHILI identity at
    all. Keying the reconciliation on the exit would lose exactly the sessions that
    most need finding.
    """
    by_symbol: dict[str, list[Any]] = {}
    for order in orders:
        size = _f(getattr(order, "filled_size", None)) or 0.0
        if size <= 0:
            continue
        sym = str(getattr(order, "product_id", "") or "").strip().upper()
        if not sym:
            continue
        by_symbol.setdefault(sym, []).append(order)

    by_session: dict[int, dict[str, Any]] = {}
    unattributed: list[dict[str, Any]] = []

    for sym, sym_orders in by_symbol.items():
        sym_orders.sort(key=_order_time)
        open_qty = 0.0
        buy_notional = 0.0
        sell_notional = 0.0
        episode_session: Optional[int] = None
        legs: list[dict[str, Any]] = []
        for order in sym_orders:
            qty = _f(getattr(order, "filled_size", None)) or 0.0
            price = _f(getattr(order, "average_filled_price", None))
            if qty <= 0 or price is None:
                continue
            side = str(getattr(order, "side", "") or "").strip().lower()
            coid = getattr(order, "client_order_id", None)
            legs.append({
                "ts": _order_time(order),
                "side": side,
                "qty": qty,
                "price": price,
                "status": str(getattr(order, "status", "") or ""),
                "client_order_id": coid,
                "broker_order_id": str(getattr(order, "order_id", "") or ""),
            })
            if side == "buy":
                if open_qty <= 1e-9:
                    episode_session = _session_id_from_client_order_id(coid)
                open_qty += qty
                buy_notional += qty * price
            else:
                open_qty -= qty
                sell_notional += qty * price
            if abs(open_qty) <= 1e-9 and legs:
                episode = {
                    "symbol": sym,
                    "session_id": episode_session,
                    "realized_pnl_usd": round(sell_notional - buy_notional, 6),
                    "buy_notional_usd": round(buy_notional, 6),
                    "sell_notional_usd": round(sell_notional, 6),
                    "legs": legs,
                    "closed": True,
                }
                if episode_session is None:
                    unattributed.append(episode)
                else:
                    slot = by_session.setdefault(
                        episode_session,
                        {"symbol": sym, "realized_pnl_usd": 0.0, "episodes": 0, "legs": [], "open": False},
                    )
                    slot["realized_pnl_usd"] = round(
                        slot["realized_pnl_usd"] + episode["realized_pnl_usd"], 6
                    )
                    slot["episodes"] += 1
                    slot["legs"].extend(legs)
                open_qty = 0.0
                buy_notional = 0.0
                sell_notional = 0.0
                episode_session = None
                legs = []
        if legs:
            # A lot still open at the window edge: its P&L is not yet measurable.
            # Reported, never guessed at.
            episode = {
                "symbol": sym,
                "session_id": episode_session,
                "realized_pnl_usd": None,
                "open_qty": round(open_qty, 6),
                "legs": legs,
                "closed": False,
            }
            if episode_session is None:
                unattributed.append(episode)
            else:
                slot = by_session.setdefault(
                    episode_session,
                    {"symbol": sym, "realized_pnl_usd": 0.0, "episodes": 0, "legs": [], "open": False},
                )
                slot["open"] = True
                slot["legs"].extend(legs)
    return by_session, unattributed


def _read_broker_orders(
    *,
    execution_family: str,
    after: datetime,
    until: datetime,
) -> dict[str, Any]:
    """One GET-only account-wide order listing. Never places or cancels anything."""
    try:
        from ..venue.factory import get_adapter

        adapter = get_adapter(execution_family)
        if adapter is None:
            return {"readable": False, "orders": [], "error": "adapter_unavailable"}
        reader = getattr(adapter, "list_account_orders_truth", None)
        if reader is None:
            return {"readable": False, "orders": [], "error": "reader_unavailable"}
        return reader(after=after, until=until, limit=500)
    except Exception as exc:
        _log.debug("[ledger_integrity] broker read failed: %s", exc, exc_info=True)
        return {"readable": False, "orders": [], "error": str(exc)[:200]}


def check_live_ledger_integrity(
    db: Session,
    *,
    days: float = 7.0,
    execution_family: str = "alpaca_spot",
    user_id: Optional[int] = None,
    include_broker: bool = True,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Reconcile outcome rows x mfe events x fill legs x broker truth. READ-ONLY.

    ``ok`` is False when any session lands in :data:`VIOLATION_STATUSES`. A broker
    read that comes back unreadable does NOT silently degrade to a clean DB-only
    verdict — ``coverage`` says ``db_only`` and ``ok`` is False, because "we cannot
    see the broker" and "the broker did nothing" are the two things this module
    exists to keep apart.
    """
    anchor = now or datetime.utcnow()
    window_days = max(0.5, min(float(days or 7.0), 120.0))
    since = anchor - timedelta(days=window_days)

    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.execution_family == execution_family,
        TradingAutomationSession.started_at >= since,
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    sessions = q.all()
    session_ids = [int(s.id) for s in sessions]

    outcomes: dict[int, MomentumAutomationOutcome] = {}
    mfe: dict[int, dict[str, Any]] = {}
    legs: dict[int, int] = {}
    if session_ids:
        for row in (
            db.query(MomentumAutomationOutcome)
            .filter(MomentumAutomationOutcome.session_id.in_(session_ids))
            .all()
        ):
            outcomes[int(row.session_id)] = row
        for ev in (
            db.query(TradingAutomationEvent)
            .filter(
                TradingAutomationEvent.session_id.in_(session_ids),
                TradingAutomationEvent.event_type == "momentum_mfe_realized",
            )
            .all()
        ):
            mfe[int(ev.session_id)] = ev.payload_json if isinstance(ev.payload_json, dict) else {}
        for leg in (
            db.query(MomentumFillOutcome)
            .filter(MomentumFillOutcome.session_id.in_(session_ids))
            .all()
        ):
            legs[int(leg.session_id)] = legs.get(int(leg.session_id), 0) + 1

    broker_by_session: dict[int, dict[str, Any]] = {}
    unattributed: list[dict[str, Any]] = []
    coverage = "db_only"
    broker_error: Optional[str] = None
    if include_broker:
        read = _read_broker_orders(
            execution_family=execution_family,
            after=since,
            until=anchor + timedelta(minutes=5),
        )
        if read.get("readable"):
            broker_by_session, unattributed = _build_broker_episodes(list(read.get("orders") or []))
            coverage = "truncated" if read.get("truncated") else "broker_and_db"
        else:
            broker_error = str(read.get("error") or "unreadable")

    rows: list[dict[str, Any]] = []
    for sess in sessions:
        sid = int(sess.id)
        outcome = outcomes.get(sid)
        broker = broker_by_session.get(sid)
        broker_pnl = _f((broker or {}).get("realized_pnl_usd")) if broker else None
        broker_open = bool((broker or {}).get("open"))
        booked_pnl = None
        legacy_pnl = None
        if outcome is not None:
            legacy_pnl = _f(outcome.realized_pnl_usd)
            booked_pnl = _f(outcome.broker_realized_pnl_usd)
            if booked_pnl is None:
                booked_pnl = legacy_pnl
        # A row whose authoritative broker_* column has been reconciled but whose
        # LEGACY realized_pnl_usd still disagrees is not a violation — the right
        # number is on the row — but it IS a trap, because every study built on
        # momentum_automation_outcomes reads the legacy column. CANF 19471 is the
        # live example: broker_realized_pnl_usd −186.98 (correct, reconciled
        # 2026-09-02T20:18:46) alongside realized_pnl_usd −78.13, because
        # UNIQUE(session_id) cannot represent the session's second trade cycle.
        legacy_split = bool(
            legacy_pnl is not None
            and booked_pnl is not None
            and abs(legacy_pnl - booked_pnl) > PNL_TOLERANCE_USD
        )

        integrity_stamp = {}
        if outcome is not None and isinstance(outcome.extracted_summary_json, dict):
            stamp = outcome.extracted_summary_json.get("ledger_integrity_v1")
            integrity_stamp = stamp if isinstance(stamp, dict) else {}
        # The ``entry_evidence_unreconciled`` stamp means "an entry order reached the
        # broker and we cannot see what it did". It is a statement about a MISSING
        # read, and it is SUPERSEDED the moment the broker-truth reconcile pass
        # succeeds on the row: that read is precisely what the stamp was waiting for.
        # Keeping the row loud afterwards would train operators to ignore the alarm.
        # Verified 2026-09-02 on the backfilled rows: XPON 15152 (−35.02), CELU 17712
        # (−47.20), MOVE 19214 (+3.90) and MOVE 19244 (−364.14) each reconciled to
        # broker truth to the cent after booking.
        broker_settled = bool(
            outcome is not None
            and str(getattr(outcome, "broker_recon_status", "") or "") == "reconciled"
            and _f(outcome.broker_realized_pnl_usd) is not None
        )

        traded_per_db = bool(mfe.get(sid) or legs.get(sid) or booked_pnl is not None)
        traded = bool(broker) or traded_per_db

        if not traded:
            status = STATUS_NEVER_FILLED
        elif outcome is None:
            status = STATUS_FILLED_NEVER_BOOKED
        elif broker is None and coverage.startswith("broker") and booked_pnl is not None:
            status = STATUS_BOOKED_NO_BROKER_FILL
        elif (
            broker_pnl is not None
            and booked_pnl is not None
            and not broker_open
            and abs(broker_pnl - booked_pnl) > PNL_TOLERANCE_USD
        ):
            status = STATUS_PNL_DISAGREES
        elif integrity_stamp.get("status") not in (None, "clean") and not broker_settled:
            status = STATUS_UNRECONCILED_ENTRY
        else:
            status = STATUS_CLEAN

        if status == STATUS_NEVER_FILLED:
            continue

        rows.append({
            "session_id": sid,
            "symbol": sess.symbol,
            "terminal_state": sess.state,
            "started_at_utc": sess.started_at.isoformat() if sess.started_at else None,
            "ended_at_utc": sess.ended_at.isoformat() if sess.ended_at else None,
            "status": status,
            "broker_realized_pnl_usd": broker_pnl,
            "broker_episodes": (broker or {}).get("episodes"),
            "broker_position_open": broker_open,
            "booked_pnl_usd": booked_pnl,
            "legacy_pnl_usd": legacy_pnl,
            "legacy_disagrees_with_authoritative": legacy_split,
            "delta_usd": (
                round(broker_pnl - booked_pnl, 6)
                if broker_pnl is not None and booked_pnl is not None
                else None
            ),
            "outcome_id": int(outcome.id) if outcome is not None else None,
            "outcome_class": outcome.outcome_class if outcome is not None else None,
            "has_mfe_event": sid in mfe,
            "mfe_r": (mfe.get(sid) or {}).get("mfe_r"),
            "realized_r": (mfe.get(sid) or {}).get("realized_r"),
            "fill_legs": legs.get(sid, 0),
            "ledger_integrity_status": integrity_stamp.get("status"),
            "entry_submission_evidence": integrity_stamp.get("entry_submission_evidence"),
        })

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    counts[STATUS_NEVER_FILLED] = len(sessions) - len(rows)

    violations = [r for r in rows if r["status"] in VIOLATION_STATUSES]
    broker_total = round(sum(r["broker_realized_pnl_usd"] or 0.0 for r in rows), 4)
    booked_total = round(sum(r["booked_pnl_usd"] or 0.0 for r in rows), 4)

    ok = not violations and not unattributed
    if include_broker and not coverage.startswith("broker"):
        # An unreadable broker is a FAILURE, not a pass with a caveat. The whole
        # premise of this check is that the DB cannot see its own blind spot.
        ok = False

    result = {
        "ok": bool(ok),
        "coverage": coverage,
        "broker_read_error": broker_error,
        "window_days": window_days,
        "window_start_utc": since.isoformat(),
        "window_end_utc": anchor.isoformat(),
        "execution_family": execution_family,
        "sessions_scanned": len(sessions),
        "sessions_traded": len(rows),
        "counts": counts,
        "broker_realized_pnl_usd": broker_total,
        "booked_pnl_usd": booked_total,
        "unbooked_pnl_usd": round(broker_total - booked_total, 4),
        "source_coverage": {
            "outcomes_with_pnl": sum(1 for r in rows if r["booked_pnl_usd"] is not None),
            "mfe_events": sum(1 for r in rows if r["has_mfe_event"]),
            "fill_leg_sessions": sum(1 for r in rows if r["fill_legs"]),
            "broker_filled_sessions": sum(1 for r in rows if r["broker_realized_pnl_usd"] is not None),
        },
        # Not violations (the authoritative column is correct) but a documented
        # trap: legacy-column consumers read a different number on these rows.
        "legacy_column_split_sessions": [
            r["session_id"] for r in rows if r["legacy_disagrees_with_authoritative"]
        ],
        "violations": violations,
        "unattributed_broker_episodes": unattributed,
        "rows": rows,
    }

    if not ok:
        _log.error(
            "[ledger_integrity] FAILED family=%s window=%sd coverage=%s violations=%s "
            "unattributed=%s broker=%.2f booked=%.2f unbooked=%.2f counts=%s",
            execution_family, window_days, coverage, len(violations), len(unattributed),
            broker_total, booked_total, broker_total - booked_total, counts,
        )
        for row in violations[:25]:
            _log.error(
                "[ledger_integrity]   session=%s %s state=%s status=%s broker=%s booked=%s delta=%s",
                row["session_id"], row["symbol"], row["terminal_state"], row["status"],
                row["broker_realized_pnl_usd"], row["booked_pnl_usd"], row["delta_usd"],
            )
    else:
        _log.info(
            "[ledger_integrity] ok family=%s window=%sd traded=%s broker=%.2f booked=%.2f",
            execution_family, window_days, len(rows), broker_total, booked_total,
        )
    return result
