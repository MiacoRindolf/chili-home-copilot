"""Operator flatten of ONE momentum live session THROUGH the FSM (never around it).

WHY (2026-09-02, Alpaca PAPER session 19471 CANF). The FSM's own entry order
f3ed508d filled 165 @ 4.62 while the session sat paused in live_pending_entry
(reaper race). The operator sold the 165 shares OUTSIDE the FSM
(client_order_id chili_ops_flat_19471_...). The FSM later booked only an
``emergency_exit_unpriced`` leg (fill_price null), so the day's ledger reads
-145.93 while the broker reads -254.78: the loss guard undercounts by -108.85
and the session had no outcome row for 20 minutes (loss guard dead account-wide
until the stale-exited finalizer ran). A manual broker sale is invisible to the
ledger by construction.

THE FSM PATH THIS SCRIPT DRIVES (live_runner.tick_live_session):
  1. ``le["operator_flatten_requested_utc"]`` is the paused-session exit
     authority key (``_paused_session_has_exit_authority``): the row is placed
     in the PRIORITY lane of ``list_runnable_live_sessions`` even while
     operator-paused, and the tick preamble's paused early-return is skipped.
  2. ``_service_quote_independent_emergency_exit`` -> ``_handle_kill_switch_mid_run
     ("operator_flatten")``: exact-CID recovery of the entry order, cancel of a
     still-open entry (``live_order_cancelled``), adoption of any cancel-raced
     fill, signed broker-quantity read, then either the broker exit chokepoint
     (``live_exit_submitted`` -> ``live_exit_filled``, PRICED) or, when the
     broker already reads zero, an UNPRICED accounting leg
     (``live_emergency_exit_unpriced``).
  3. ``operator_flatten_executed`` (or ``operator_flatten_pending`` when the
     tick could not finish: cancel pending, ack-loss, identity mismatch,
     broker read unknown -- the persisted ``emergency_exit_authority`` makes
     the NEXT tick continue). Then ``_transition_completed_emergency``:
     held -> live_exited; pending with no quantity -> watching -> live_cancelled.
  4. ``automation_query.stop_automation_session`` terminalizes (live_cancelled,
     pause cleared, outcome row written by the terminal transition) once an
     independent broker-flat read succeeds.

THE TICK IS THE ONLY EXECUTOR. This script sets the key and WAITS for the
lane's tick to act. It is for a RUNNING lane (event-loop heartbeat fresh). When
the lane is DOWN, ``--execute --allow-lane-down`` only parks the request (the
first tick after relaunch flattens), and ``--execute --inline-tick`` runs
``tick_live_session`` in THIS process -- still the FSM chokepoint, refused while
a lane heartbeat is fresh so two tickers never race one row.

SAFETY POSTURE:
  * DRY-RUN BY DEFAULT: prints session state / pause / claim / outcome / lane
    health / broker truth and the eligibility verdict. No write, no order.
  * ``--execute`` writes exactly ONE key (plus the ``operator_flatten_requested``
    audit event) on a row-locked (FOR UPDATE NOWAIT) re-checked row.
  * Never places or cancels an order itself. Broker access is read-only
    (AlpacaSpotAdapter GETs). Never prints credentials.
  * Never clears the operator pause, never pops ``entry_*`` keys, never writes
    a synthetic position (all three re-open the naked-position door).

Usage:
    conda run -n chili-env python scripts/operator_flatten_session.py --session-id 19471
    conda run -n chili-env python scripts/operator_flatten_session.py --session-id 19471 --execute
    conda run -n chili-env python scripts/operator_flatten_session.py --session-id 19471 --stop-only

Exit codes: 0 done / dry-run; 1 refused or error; 2 flatten executed but stop
deferred (re-run --stop-only); 3 request parked (lane down); 4 wait timed out.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FLATTEN_KEY = "operator_flatten_requested_utc"
LIVE_EXEC_KEY = "momentum_live_execution"
ALPACA_FAMILIES = frozenset({"alpaca_spot", "alpaca_short"})
HELD_STATES = frozenset(
    {"live_entered", "live_scaling_out", "live_trailing", "live_bailout"}
)
FLATTENABLE_STATES = HELD_STATES | {"live_pending_entry"}
TERMINAL_STATES = frozenset({"live_finished", "live_cancelled", "live_error"})
EXECUTED_EVENT = "operator_flatten_executed"
PENDING_EVENT = "operator_flatten_pending"
WATCH_EVENTS = (
    EXECUTED_EVENT,
    PENDING_EVENT,
    "live_order_cancelled",
    "live_exit_submitted",
    "live_exit_filled",
    "live_emergency_exit_unpriced",
    "live_emergency_exit_pre_place_blocked",
    "live_tick_operator_paused_block",
    "live_error",
    "session_stopped",
)
LE_KEYS_OF_INTEREST = (
    FLATTEN_KEY,
    "entry_submitted",
    "entry_order_id",
    "entry_client_order_id",
    "entry_reconcile_pending_client_order_id",
    "entry_orders_resolved",
    "position",
    "side_long",
    "emergency_exit_authority",
    "emergency_exit_accounting_pending",
    "emergency_position_truth",
    "emergency_entry_cancel_pending",
    "emergency_entry_ack_loss_pending",
    "operator_stop_requested",
    "operator_paused_block_sig",
    "last_recycled_at_utc",
)
STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_WAIT_SECONDS = 90
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_INLINE_TICKS = 6


# ── pure helpers (DB-free, unit-tested) ──────────────────────────────────────


def live_exec_of(sess: Any) -> dict[str, Any]:
    snap = getattr(sess, "risk_snapshot_json", None)
    snap = snap if isinstance(snap, dict) else {}
    le = snap.get(LIVE_EXEC_KEY)
    return dict(le) if isinstance(le, dict) else {}


def pause_info_of(sess: Any) -> dict[str, Any]:
    from app.services.trading.momentum_neural.session_lifecycle import operator_pause_info

    return operator_pause_info(getattr(sess, "risk_snapshot_json", None))


def evaluate_eligibility(
    sess: Any,
    *,
    quarantine_reason: str | None,
    lane_ok: bool | None,
    allow_lane_down: bool,
    inline_tick: bool,
) -> tuple[bool, list[str]]:
    """Decide whether ``--execute`` may set the flatten key.

    ``lane_ok``: True = fresh live-loop heartbeat, False = stale/missing/error,
    None = driver is not the event loop (batch scheduler) -> cannot be proven
    from the DB; treated as running unless ``--inline-tick`` is requested.
    """
    reasons: list[str] = []
    if sess is None:
        return False, ["session_not_found"]
    if str(getattr(sess, "mode", "") or "") != "live":
        reasons.append(f"mode_not_live:{getattr(sess, 'mode', None)}")
    family = str(getattr(sess, "execution_family", "") or "").strip().lower()
    if family not in ALPACA_FAMILIES:
        reasons.append(f"execution_family_not_alpaca:{family or 'missing'}")
    state = str(getattr(sess, "state", "") or "")
    if state in TERMINAL_STATES:
        reasons.append(f"already_terminal:{state}")
    elif state not in FLATTENABLE_STATES:
        reasons.append(f"state_not_flattenable:{state}")
    if quarantine_reason:
        reasons.append(f"execution_quarantined:{quarantine_reason}")
    if getattr(sess, "user_id", None) is None:
        reasons.append("user_id_missing_stop_would_be_refused")
    if inline_tick:
        if lane_ok is True:
            reasons.append("inline_tick_refused_lane_heartbeat_fresh")
        if lane_ok is None:
            reasons.append("inline_tick_refused_lane_driver_not_event_loop")
    elif lane_ok is False and not allow_lane_down:
        reasons.append("lane_down_use_allow_lane_down_or_inline_tick")
    return not reasons, reasons


def apply_flatten_request(le: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Return a copy of ``le`` with ONLY the flatten key added (idempotent)."""
    out = dict(le)
    if not out.get(FLATTEN_KEY):
        out[FLATTEN_KEY] = now.isoformat()
    return out


def diff_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    """Keys whose value differs between two envelopes (write-scope guard)."""
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}


def classify_events(events: Iterable[Any]) -> str | None:
    """'executed' once operator_flatten_executed is seen, 'pending' if only the
    pending marker was seen, else None."""
    status: str | None = None
    for ev in events:
        et = str(getattr(ev, "event_type", None) or (ev.get("event_type") if isinstance(ev, dict) else "") or "")
        if et == EXECUTED_EVENT:
            return "executed"
        if et == PENDING_EVENT:
            status = "pending"
    return status


def stop_outcome_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") and not result.get("pending"):
        return 0
    if result.get("ok") and result.get("pending"):
        return 2
    return 1


def redact(obj: Any) -> Any:
    """Defensive: drop anything that looks like a credential before printing."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if any(t in str(k).lower() for t in ("secret", "token", "api_key", "password")) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _print(label: str, obj: Any = None) -> None:
    if obj is None:
        print(label)
        return
    print(f"{label}: {json.dumps(redact(obj), default=str, indent=2, sort_keys=True)}")


# ── DB / broker readers ───────────────────────────────────────────────────────


def open_db():
    from sqlalchemy import text

    from app.db import SessionLocal

    db = SessionLocal()
    db.execute(text(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
    return db


def load_session(db, session_id: int, *, for_update: bool = False):
    from app.models.trading import TradingAutomationSession

    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.id == int(session_id)
    )
    if for_update:
        q = q.populate_existing().with_for_update(nowait=True)
    return q.one_or_none()


def lane_status(db) -> dict[str, Any]:
    """{'ok': True|False|None, 'reason': str|None, ...} from the durable heartbeat."""
    from app.services.trading.momentum_neural.lane_health import (
        live_runner_driver_configuration,
        live_runner_loop_control_health,
    )

    mode, err = live_runner_driver_configuration()
    if err is not None:
        return {"ok": False, "reason": err, "driver": mode}
    if mode is None:
        return {"ok": False, "reason": "live_runner_no_driver_enabled", "driver": None}
    if mode != "event_loop":
        return {"ok": None, "reason": "driver_not_event_loop", "driver": mode}
    health = live_runner_loop_control_health(db)
    return {
        "ok": health.get("ok") is True,
        "reason": health.get("reason"),
        "driver": mode,
        "heartbeat_at": health.get("heartbeat_at"),
        "heartbeat_age_seconds": health.get("heartbeat_age_seconds"),
        "stale_seconds": health.get("stale_seconds"),
    }


def read_claim(db, sess) -> dict[str, Any]:
    from app.services.trading.momentum_neural.alpaca_orphan_claims import read_action_claim

    readable, claim = read_action_claim(db, symbol=sess.symbol, account_scope="alpaca:paper")
    return {"readable": readable, "claim": claim}


def read_outcome(db, session_id: int) -> dict[str, Any] | None:
    from app.models.trading import MomentumAutomationOutcome

    row = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(session_id))
        .one_or_none()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "terminal_state": row.terminal_state,
        "realized_pnl_usd": row.realized_pnl_usd,
        "broker_recon_status": row.broker_recon_status,
        "broker_realized_pnl_usd": row.broker_realized_pnl_usd,
        "broker_notional_basis_usd": row.broker_notional_basis_usd,
        "broker_reconciled_at": row.broker_reconciled_at,
    }


def recent_events(db, session_id: int, *, limit: int = 15, after_id: int | None = None) -> list[Any]:
    from app.models.trading import TradingAutomationEvent

    q = db.query(TradingAutomationEvent).filter(
        TradingAutomationEvent.session_id == int(session_id)
    )
    if after_id is not None:
        q = q.filter(TradingAutomationEvent.id > int(after_id)).order_by(TradingAutomationEvent.id.asc())
    else:
        q = q.order_by(TradingAutomationEvent.id.desc()).limit(int(limit))
    rows = q.all()
    return rows if after_id is not None else list(reversed(rows))


def event_brief(ev: Any) -> dict[str, Any]:
    return {
        "id": ev.id,
        "ts": ev.ts,
        "event_type": ev.event_type,
        "payload": ev.payload_json,
    }


def broker_truth(sess, le: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only Alpaca GETs through the app adapter (paper posture enforced there)."""
    out: dict[str, Any] = {}
    try:
        from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter

        adapter = AlpacaSpotAdapter()
    except Exception as exc:  # pragma: no cover - environment
        return {"error": f"adapter_unavailable:{type(exc).__name__}"}
    symbol = str(sess.symbol or "")
    try:
        out["position_quantity"] = adapter.get_position_quantity(symbol)
    except Exception as exc:
        out["position_quantity"] = f"unreadable:{type(exc).__name__}"
    oid = str(le.get("entry_order_id") or "").strip()
    if oid:
        try:
            truth = adapter.get_order_truth(oid)
            out["entry_order"] = _order_brief(truth)
        except Exception as exc:
            out["entry_order"] = f"unreadable:{type(exc).__name__}"
    cid = str(le.get("entry_client_order_id") or le.get("entry_reconcile_pending_client_order_id") or "").strip()
    if cid and not oid:
        try:
            truth = adapter.get_order_by_client_order_id_truth(cid)
            out["entry_order_by_cid"] = _order_brief(truth)
        except Exception as exc:
            out["entry_order_by_cid"] = f"unreadable:{type(exc).__name__}"
    try:
        orders, _meta = adapter.list_open_orders(product_id=symbol, strict=True)
        out["open_orders"] = (
            None if orders is None else [_order_fields(o) for o in orders]
        )
    except Exception as exc:
        out["open_orders"] = f"unreadable:{type(exc).__name__}"
    return out


def _order_fields(order: Any) -> dict[str, Any]:
    if order is None:
        return {}
    keys = ("order_id", "client_order_id", "product_id", "side", "status", "filled_size", "average_filled_price")
    if isinstance(order, dict):
        return {k: order.get(k) for k in keys}
    return {k: getattr(order, k, None) for k in keys}


def _order_brief(truth: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "readable": truth.get("readable"),
        "found": truth.get("found"),
        "order": _order_fields(truth.get("order")),
    }


# ── the write (one key) ───────────────────────────────────────────────────────


def request_flatten(db, session_id: int, *, now: datetime, actor: str) -> dict[str, Any]:
    """Row-locked, re-checked, single-key write + audit event. Commits."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.trading.momentum_neural.operator_actions import (
        _persisted_alpaca_execution_quarantine_reason,
    )
    from app.services.trading.momentum_neural.persistence import append_trading_automation_event

    sess = load_session(db, session_id, for_update=True)
    if sess is None:
        db.rollback()
        return {"ok": False, "error": "not_found"}
    # Guarded re-check on the LOCKED row (the recipe's guarded WHERE).
    ok, reasons = evaluate_eligibility(
        sess,
        quarantine_reason=_persisted_alpaca_execution_quarantine_reason(sess),
        lane_ok=True,
        allow_lane_down=True,
        inline_tick=False,
    )
    if not ok:
        db.rollback()
        return {"ok": False, "error": "guard_failed", "reasons": reasons}
    before = live_exec_of(sess)
    if before.get(FLATTEN_KEY):
        db.rollback()
        return {"ok": True, "already_requested": True, "requested_utc": before.get(FLATTEN_KEY)}
    after = apply_flatten_request(before, now=now)
    changed = diff_keys(before, after)
    if changed != {FLATTEN_KEY}:
        db.rollback()
        return {"ok": False, "error": "write_scope_violation", "changed": sorted(changed)}
    snap = dict(sess.risk_snapshot_json or {})
    snap[LIVE_EXEC_KEY] = after
    sess.risk_snapshot_json = snap
    flag_modified(sess, "risk_snapshot_json")
    sess.updated_at = now
    append_trading_automation_event(
        db,
        int(sess.id),
        "operator_flatten_requested",
        {"by": actor, "state": sess.state, "requested_utc": after[FLATTEN_KEY]},
        correlation_id=sess.correlation_id,
        source_node_id="operator_flatten_session_script",
    )
    db.commit()
    return {"ok": True, "already_requested": False, "requested_utc": after[FLATTEN_KEY], "state": sess.state}


def wait_for_execution(
    db,
    session_id: int,
    *,
    after_event_id: int | None,
    wait_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str | None, int | None]:
    """Poll the append-only event log until operator_flatten_executed or timeout."""
    deadline = clock() + max(0.0, float(wait_seconds))
    last_id = after_event_id
    status: str | None = None
    while True:
        db.rollback()  # fresh snapshot each poll (READ COMMITTED)
        new = recent_events(db, session_id, after_id=last_id if last_id is not None else 0)
        for ev in new:
            if ev.event_type in WATCH_EVENTS:
                _print("  event", event_brief(ev))
            last_id = ev.id
        seen = classify_events(new)
        if seen == "executed":
            return "executed", last_id
        if seen == "pending":
            status = "pending"
        sess = load_session(db, session_id)
        if sess is not None and sess.state in TERMINAL_STATES:
            return "terminal", last_id
        if clock() >= deadline:
            return status, last_id
        sleep(max(0.2, float(poll_seconds)))


def run_inline_ticks(db, session_id: int, *, max_ticks: int, sleep: Callable[[float], None] = time.sleep) -> str | None:
    """Lane-down remedy: drive tick_live_session in THIS process (FSM chokepoint)."""
    from app.services.trading.momentum_neural.live_runner import tick_live_session

    for i in range(max(1, int(max_ticks))):
        try:
            result = tick_live_session(db, int(session_id))
            db.commit()
        except Exception as exc:
            db.rollback()
            _print(f"  inline tick {i + 1} raised {type(exc).__name__}: {exc}")
            result = None
        if isinstance(result, dict):
            _print(f"  inline tick {i + 1}", result)
            if result.get("operator_flatten") is True or result.get("flattened") is True:
                return "executed"
        sess = load_session(db, session_id)
        if sess is not None and sess.state in TERMINAL_STATES:
            return "terminal"
        sleep(1.0)
    return None


def stop_session(db, sess) -> dict[str, Any]:
    from app.services.trading.momentum_neural.automation_query import stop_automation_session

    result = stop_automation_session(db, user_id=int(sess.user_id), session_id=int(sess.id))
    if result.get("ok"):
        db.commit()
    else:
        db.rollback()
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--session-id", type=int, required=True)
    p.add_argument("--execute", action="store_true", help="set the flatten key (default: dry-run)")
    p.add_argument("--stop-only", action="store_true", help="skip the request; only call stop_automation_session")
    p.add_argument("--no-stop", action="store_true", help="after operator_flatten_executed, do not terminalize")
    p.add_argument("--wait-seconds", type=float, default=DEFAULT_WAIT_SECONDS)
    p.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    p.add_argument("--allow-lane-down", action="store_true", help="park the request even if no fresh lane heartbeat")
    p.add_argument("--inline-tick", action="store_true", help="LANE DOWN ONLY: run tick_live_session in this process")
    p.add_argument("--inline-ticks", type=int, default=DEFAULT_INLINE_TICKS)
    p.add_argument("--skip-broker", action="store_true", help="do not perform the read-only broker GETs")
    p.add_argument("--verbose", action="store_true", help="dump the full momentum_live_execution envelope")
    p.add_argument("--actor", default="operator_flatten_session.py")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.inline_tick and not args.execute:
        print("refused: --inline-tick requires --execute")
        return 1
    if args.stop_only and args.execute:
        print("refused: --stop-only and --execute are mutually exclusive")
        return 1

    from app.services.trading.momentum_neural.operator_actions import (
        _persisted_alpaca_execution_quarantine_reason,
    )

    db = open_db()
    try:
        sess = load_session(db, args.session_id)
        if sess is None:
            print(f"session {args.session_id}: not found")
            return 1
        le = live_exec_of(sess)
        quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        lane = lane_status(db)
        _print("session", {
            "id": sess.id, "symbol": sess.symbol, "mode": sess.mode, "state": sess.state,
            "execution_family": sess.execution_family, "user_id": sess.user_id,
            "started_at": sess.started_at, "updated_at": sess.updated_at, "ended_at": sess.ended_at,
        })
        _print("operator_pause", pause_info_of(sess))
        _print("live_execution", le if args.verbose else {k: le.get(k) for k in LE_KEYS_OF_INTEREST if k in le})
        _print("execution_quarantine", quarantine)
        _print("durable_claim", read_claim(db, sess))
        _print("outcome_row", read_outcome(db, sess.id))
        _print("lane", lane)
        events = recent_events(db, sess.id)
        _print("recent_events", [event_brief(e) for e in events])
        last_event_id = events[-1].id if events else 0
        if not args.skip_broker:
            _print("broker_truth", broker_truth(sess, le))
        db.rollback()

        if args.stop_only:
            if sess.user_id is None:
                print("refused: session has no user_id; stop_automation_session needs one")
                return 1
            result = stop_session(db, sess)
            _print("stop_automation_session", result)
            return stop_outcome_exit_code(result)

        ok, reasons = evaluate_eligibility(
            sess, quarantine_reason=quarantine, lane_ok=lane.get("ok"),
            allow_lane_down=args.allow_lane_down, inline_tick=args.inline_tick,
        )
        _print("eligibility", {"ok": ok, "reasons": reasons, "already_requested": bool(le.get(FLATTEN_KEY))})
        if not args.execute:
            print("DRY-RUN (default). Re-run with --execute to set operator_flatten_requested_utc "
                  "and wait for the lane's tick to emit operator_flatten_executed.")
            return 0
        if not ok:
            print("refused: " + ", ".join(reasons))
            return 1

        req = request_flatten(db, sess.id, now=datetime.utcnow(), actor=args.actor)
        _print("request_flatten", req)
        if not req.get("ok"):
            return 1

        if lane.get("ok") is False and not args.inline_tick:
            print("request PARKED: no fresh lane heartbeat. The first tick after the lane relaunches "
                  "will flatten through the FSM. Do NOT sell at the broker; if the price is collapsing "
                  "re-run with --execute --inline-tick (see docs/RUNBOOKS/OPERATOR_FLATTEN_SESSION.md).")
            return 3

        status: str | None
        if args.inline_tick:
            status = run_inline_ticks(db, sess.id, max_ticks=args.inline_ticks)
        else:
            status, _ = wait_for_execution(
                db, sess.id, after_event_id=last_event_id,
                wait_seconds=args.wait_seconds, poll_seconds=args.poll_seconds,
            )
        _print("flatten_status", status)
        if status not in ("executed", "terminal"):
            print("timed out waiting for operator_flatten_executed. The key stays set; the persisted "
                  "emergency_exit_authority makes the next tick continue. Check the events above "
                  "(operator_flatten_pending payload / emergency_* markers) before doing anything else.")
            return 4
        db.rollback()
        sess = load_session(db, sess.id)
        if status == "terminal" or args.no_stop or sess is None:
            return 0
        if sess.state in TERMINAL_STATES:
            return 0
        result = stop_session(db, sess)
        _print("stop_automation_session", result)
        return stop_outcome_exit_code(result)
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
