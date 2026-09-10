"""Settle unconfirmed outcomes from fills we already recorded.

THE PROBLEM. Six legs carry 91.33% of the live book's net measured loss (-$9,879.24 of
-$10,817.06) and every one is UNCONFIRMED against broker truth. Until they settle, no A/B
this project runs is measured against a single-valued book.

WHY THE BATCH RECONCILER WILL NEVER FIX THEM. `needs_reconcile` only admits rows inside a
2.0-day lookback (app/config.py, `chili_momentum_outcome_recon_lookback_days`). These rows
are ~60 days old: no pass will ever select them again. They are outside the window, not
beyond repair.

WHY WE DO NOT NEED THE BROKER. The recorded events already carry what a settlement needs.
`momentum_fill_outcomes` holds the ledger legs WITH their `broker_order_id`, and
`trading_automation_events` holds the submit/fill/pending-confirmation events that name the
same ids, their prices, sizes and timestamps. This script assembles those into the same
`broker_orders_reader` shape the live Alpaca reader returns, and hands it to the REAL
`reconcile_one_outcome`. No reconciliation logic is reimplemented here; the seam is the one
`tests/test_broker_truth_recorded_fills_0908.py` exercises.

WHAT IT WILL NOT DO. It never invents an order. Every order it presents comes from a
recorded row, and a session whose ledger legs cannot all be matched is reported and SKIPPED
rather than settled — the reconciler's own `ledger_ids_missing_from_broker` check would
refuse it anyway, and a settlement built on a guessed fill is worse than none.

    python scripts/backfill_broker_truth_from_recorded.py --dry-run
    python scripts/backfill_broker_truth_from_recorded.py --ids 199025,199049 --apply

Default is --dry-run. Nothing is written without --apply.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text          # noqa: E402
from sqlalchemy.orm import sessionmaker             # noqa: E402

from app.services.trading.momentum_neural import outcome_reconcile as orc   # noqa: E402
from app.services.trading.venue.protocol import NormalizedOrder             # noqa: E402

# Events that can name a broker order id, and where the id lives inside the payload.
_ID_PATHS = (
    ("live_entry_submitted", ("result", "order_id")),
    ("live_entry_filled", ("order_id",)),
    ("live_exit_pending_confirmation", ("order_id",)),
    ("scale_out_limit_placed", ("order_id",)),
    ("tranche_oco_placed", ("order_id",)),
)


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    cur = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _f(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out == out else None          # NaN guard
    except (TypeError, ValueError):
        return None


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat() + ("" if ts.tzinfo else "+00:00")
    return str(ts)


def _unconfirmed(conn, ids: list[int] | None, limit: int) -> list[dict]:
    """The rows this script exists for: real money, no broker verdict."""
    where = "o.broker_recon_status IS NULL OR o.broker_recon_status NOT IN ('reconciled')"
    params: dict[str, Any] = {"lim": limit}
    if ids:
        where = "o.id = ANY(:ids)"
        params["ids"] = ids
    rows = conn.execute(text(f"""
        SELECT o.id, o.session_id, o.symbol, o.realized_pnl_usd, o.broker_recon_status,
               o.outcome_class, o.exit_reason, o.execution_family, o.terminal_at,
               s.started_at, s.ended_at, s.mode, s.state, s.risk_snapshot_json
        FROM momentum_automation_outcomes o
        JOIN trading_automation_sessions s ON s.id = o.session_id
        WHERE ({where})
          AND o.mode = 'live'
          AND o.realized_pnl_usd IS NOT NULL
          AND o.realized_pnl_usd <> 0
        ORDER BY abs(o.realized_pnl_usd) DESC
        LIMIT :lim
    """), params).fetchall()
    cols = ("id", "session_id", "symbol", "realized_pnl_usd", "broker_recon_status",
            "outcome_class", "exit_reason", "execution_family", "terminal_at",
            "started_at", "ended_at", "mode", "state", "risk_snapshot_json")
    return [dict(zip(cols, r)) for r in rows]


def _ledger_legs(conn, session_id: int) -> list[tuple]:
    """The 12-column tuple the attribution path reads, in its documented order."""
    rows = conn.execute(text("""
        SELECT side, leg_seq, fill_source, broker_fill_price, qty, fees_usd,
               settled_pnl_usd, settled_fees_usd, realized_pnl_usd, entry_price,
               broker_order_id, fill_ts
        FROM momentum_fill_outcomes
        WHERE session_id = :sid
        ORDER BY leg_seq, side
    """), {"sid": session_id}).fetchall()
    return [tuple(r) for r in rows]


def _orders_from_events(conn, session_id: int, symbol: str) -> list[NormalizedOrder]:
    """Rebuild the broker's order list from what we recorded at the time.

    One NormalizedOrder per distinct order id. Fill size and price come from the ledger
    where the ledger names the id (it is the settled number), and from the recorded fill
    event otherwise. An order we only ever saw submitted is presented as it was last
    seen — unfilled — because that is what the broker would have shown.
    """
    rows = conn.execute(text("""
        SELECT ts, event_type, payload_json
        FROM trading_automation_events
        WHERE session_id = :sid
        ORDER BY ts, id
    """), {"sid": session_id}).fetchall()

    seen: dict[str, dict] = {}
    order_of: list[str] = []

    def _touch(oid: str) -> dict:
        if oid not in seen:
            seen[oid] = {"order_id": oid, "client_order_id": None, "side": None,
                         "status": "new", "order_type": "limit", "filled_size": 0.0,
                         "avg": None, "at": None}
            order_of.append(oid)
        return seen[oid]

    for ts, etype, payload in rows:
        payload = payload if isinstance(payload, dict) else {}
        for want, path in _ID_PATHS:
            if etype != want:
                continue
            oid = _dig(payload, path)
            if not oid:
                continue
            rec = _touch(str(oid))
            cid = _dig(payload, ("result", "client_order_id")) or payload.get("client_order_id")
            if cid:
                rec["client_order_id"] = str(cid)
            if etype == "live_entry_submitted":
                rec["side"] = "buy"
                rec["at"] = rec["at"] or _iso(ts)
            elif etype == "live_entry_filled":
                rec["side"] = "buy"
                rec["status"] = "filled"
                rec["filled_size"] = _f(payload.get("filled_size")) or rec["filled_size"]
                rec["avg"] = _f(payload.get("avg")) or rec["avg"]
                rec["at"] = _iso(ts)
            else:
                rec["side"] = "sell"
                rec["at"] = rec["at"] or _iso(ts)

    # The ledger is the settled truth for any id it names.
    for side, _seq, _src, px, qty, _fees, _sp, _sf, _rp, _ep, oid, fill_ts in _ledger_legs(
            conn, session_id):
        if not oid:
            continue
        rec = _touch(str(oid))
        rec["side"] = "buy" if str(side).lower() == "entry" else "sell"
        rec["status"] = "filled"
        rec["filled_size"] = _f(qty) or rec["filled_size"]
        rec["avg"] = _f(px) if _f(px) is not None else rec["avg"]
        rec["at"] = _iso(fill_ts) or rec["at"]

    out: list[NormalizedOrder] = []
    for oid in order_of:
        r = seen[oid]
        if r["side"] is None:
            continue                       # never learned which side: do not guess
        at = r["at"]
        out.append(NormalizedOrder(
            order_id=r["order_id"],
            client_order_id=r["client_order_id"],
            product_id=symbol,
            side=r["side"],
            status=r["status"] if r["filled_size"] else "canceled",
            order_type=r["order_type"],
            filled_size=float(r["filled_size"] or 0.0),
            average_filled_price=r["avg"],
            created_time=at,
            raw={"filled_at": at, "submitted_at": at, "source": "recorded_events"},
        ))
    return out


def _settle(Session, target: dict, orders: list[NormalizedOrder], *, apply: bool) -> dict:
    """Run the REAL reconciler with a reader built from the recording.

    No reconciliation logic lives here. `reconcile_one_outcome` is the same function the
    live pass calls; only its broker reader is replaced, through the kwarg that
    tests/test_broker_truth_recorded_fills_0908.py exercises. Without --apply the
    transaction is rolled back, so a dry run reports the numbers the reconciler WOULD
    write without writing them.
    """
    from app.models.trading import MomentumAutomationOutcome, TradingAutomationSession

    out: dict[str, Any] = {}
    db = Session()
    try:
        outcome = db.get(MomentumAutomationOutcome, target["id"])
        sess = db.get(TradingAutomationSession, target["session_id"])
        if outcome is None or sess is None:
            return {"action": "SKIPPED_row_vanished"}

        def reader(symbol, after, until):
            return {"readable": True, "orders": orders, "truncated": False}

        res = orc.reconcile_one_outcome(db, outcome, sess, broker_orders_reader=reader)
        detail = outcome.broker_recon_detail_json or (res or {}).get("detail") or {}
        ba = (detail or {}).get("broker_attribution") or {}
        out = {
            "new_status": outcome.broker_recon_status,
            "broker_pnl": _f(outcome.broker_realized_pnl_usd),
            "broker_notional": _f(outcome.broker_notional_basis_usd),
            "divergence": _f(outcome.broker_divergence_usd),
            "attr_status": ba.get("attr_status"),
            "attribution_version": (detail or {}).get("attribution_version"),
            "source": (detail or {}).get("source"),
        }
        if apply:
            db.commit()
            out["action"] = "SETTLED"
        else:
            db.rollback()
            out["action"] = "WOULD_SETTLE"
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        db.rollback()
        out = {"action": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:220]}
    finally:
        db.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get(
        "DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili"))
    ap.add_argument("--ids", default="", help="comma-separated outcome ids; default = the "
                                              "largest unconfirmed losses")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--apply", action="store_true", help="commit; default is dry-run")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip()] or None
    engine = create_engine(args.db, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, future=True)

    report: list[dict] = []
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout='120s'"))
        targets = _unconfirmed(conn, ids, args.limit)
        print(f"[backfill] {len(targets)} unconfirmed outcome(s) selected")

        for t in targets:
            orders = _orders_from_events(conn, t["session_id"], t["symbol"])
            ledger = _ledger_legs(conn, t["session_id"])
            ledger_ids = {str(r[10]) for r in ledger if r[10]}
            have = {o.order_id for o in orders}
            missing = sorted(ledger_ids - have)
            row = {
                "outcome_id": t["id"], "session_id": t["session_id"],
                "symbol": t["symbol"], "realized_pnl_usd": _f(t["realized_pnl_usd"]),
                "prior_status": t["broker_recon_status"],
                "orders_rebuilt": len(orders), "ledger_legs": len(ledger),
                "ledger_ids_not_rebuilt": missing,
            }
            if missing:
                row["action"] = "SKIPPED_ledger_id_not_in_recording"
                report.append(row)
                print(f"  [skip] {t['symbol']:6} outcome={t['id']} — ledger names "
                      f"{missing} but the recording does not; refusing to settle")
                continue
            if not ledger:
                row["action"] = "SKIPPED_no_ledger_legs"
                report.append(row)
                print(f"  [skip] {t['symbol']:6} outcome={t['id']} — no ledger legs")
                continue
            settled = _settle(Session, t, orders, apply=args.apply)
            row.update(settled)
            report.append(row)
            mark = "apply" if args.apply else "dry"
            print(f"  [{mark}] {t['symbol']:6} outcome={t['id']} sess={t['session_id']} "
                  f"realized={row['realized_pnl_usd']:+.2f} "
                  f"broker={settled.get('broker_pnl')} "
                  f"attr={settled.get('attr_status')} "
                  f"status={settled.get('new_status')} "
                  f"div={settled.get('divergence')}")

    if args.json_out and report:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"schema": "chili.broker_truth_backfill.v1", "rows": report},
                      fh, indent=2, default=str)
        print(f"[backfill] wrote {args.json_out}")

    would = sum(1 for r in report if r["action"].startswith(("WOULD", "SETTLED")))
    skipped = len(report) - would
    print(f"[backfill] settleable={would} skipped={skipped} "
          f"({'APPLIED' if args.apply else 'DRY RUN — nothing written'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
