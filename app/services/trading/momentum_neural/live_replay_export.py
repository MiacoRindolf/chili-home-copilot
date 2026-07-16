"""Read-only live DB export bridge for Replay v3.

This module turns recent momentum live sessions and automation events into the
plain row shapes accepted by `replay_v3`. It intentionally does not mutate DB
state, place orders, or infer fills beyond fields already present in the live
rows/events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from ....models.trading import TradingAutomationEvent, TradingAutomationSession
from ..execution_family_registry import normalize_execution_family
from .replay_v3 import (
    ReplayBrokerOutcome,
    ReplaySchedulerLiveSnapshotStep,
    ReplayVenueState,
    replay_broker_outcomes_from_rows,
)


_REPLAY_OUTCOME_EVENT_TYPES = frozenset(
    {
        "live_entry_cancelled",
        "live_entry_cancelled_confirmed",
        "live_entry_failed",
        "live_entry_fill",
        "live_entry_filled",
        "live_entry_no_fill",
        "live_entry_rejected",
        "live_exit_cancelled",
        "live_exit_failed",
        "live_exit_fill",
        "live_exit_filled",
        "live_exit_no_fill",
        "live_exit_rejected",
        "paper_exit_filled",
        "exit_fill",
        "entry_fill",
    }
)

_VENUE_UNAVAILABLE_REASONS = frozenset(
    {
        "coinbase_adapter_unavailable",
        "venue_adapter_unavailable",
        "venue_broker_not_connected",
    }
)

_REPLAY_SNAPSHOT_EVENT_TYPES = frozenset(
    {
        "live_replay_event_snapshot",
        "live_replay_scheduler_snapshot",
        "live_scheduler_snapshot",
        "momentum_replay_scheduler_snapshot",
    }
)


@dataclass(frozen=True)
class LiveReplayExport:
    """Replay-ready extract from live momentum DB state."""

    as_of_utc: str
    session_rows: tuple[dict[str, Any], ...]
    outcome_rows: tuple[dict[str, Any], ...]
    setup_attribution_rows: tuple[dict[str, Any], ...]
    opportunity_label_rows: tuple[dict[str, Any], ...]
    venue_states: tuple[ReplayVenueState, ...]
    snapshot_step: ReplaySchedulerLiveSnapshotStep
    snapshot_steps: tuple[ReplaySchedulerLiveSnapshotStep, ...]
    broker_outcomes: tuple[ReplayBrokerOutcome, ...]


def _iso_utc(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _datetime_utc(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _payload(row: Any) -> dict[str, Any]:
    payload = getattr(row, "payload_json", None)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _snapshot(row: Any) -> dict[str, Any]:
    snap = getattr(row, "risk_snapshot_json", None)
    return dict(snap) if isinstance(snap, Mapping) else {}


def _mapping(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _live_exec_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(snapshot.get("momentum_live_execution"))


def _float_or_none(raw: Any) -> float | None:
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _bool_or_none(raw: Any) -> bool | None:
    if raw is None:
        return None
    return bool(raw)


def _session_row(sess: TradingAutomationSession) -> dict[str, Any]:
    snap = _snapshot(sess)
    return {
        "id": int(sess.id),
        "session_id": int(sess.id),
        "symbol": str(sess.symbol or ""),
        "venue": str(sess.venue or ""),
        "execution_family": normalize_execution_family(sess.execution_family),
        "mode": str(sess.mode or ""),
        "state": str(sess.state or ""),
        "risk_snapshot_json": snap,
        "snapshot": snap,
        "started_at": _iso_utc(sess.started_at),
        "created_at": _iso_utc(sess.created_at),
        "updated_at": _iso_utc(sess.updated_at),
        "ended_at": _iso_utc(sess.ended_at),
        "correlation_id": sess.correlation_id,
        "source_node_id": sess.source_node_id,
    }


def _candidate_payloads_for_attribution(
    session_rows: Iterable[Mapping[str, Any]],
    outcome_rows: Iterable[Mapping[str, Any]],
) -> Iterable[tuple[int, dict[str, Any], dict[str, Any], str]]:
    for row in session_rows:
        sid = int(row.get("session_id") or row.get("id") or 0)
        if sid <= 0:
            continue
        snap = _mapping(row.get("risk_snapshot_json") or row.get("snapshot"))
        le = _live_exec_from_snapshot(snap)
        if le:
            payload = dict(le)
            trigger_debug = _mapping(le.get("entry_trigger_debug"))
            if trigger_debug:
                payload["entry_trigger_debug"] = trigger_debug
            yield sid, row, payload, "session_snapshot"
    for row in outcome_rows:
        sid = int(row.get("session_id") or 0)
        if sid <= 0:
            continue
        payload = _mapping(row.get("payload_json"))
        if payload:
            yield sid, row, payload, str(row.get("event_type") or "event_payload")


def _find_attribution_debug(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("entry_trigger_debug", "trigger_debug", "debug", "setup_debug"):
        val = payload.get(key)
        if isinstance(val, Mapping):
            return dict(val)
    trace = payload.get("setup_trace")
    return dict(trace) if isinstance(trace, Mapping) else dict(payload)


def _setup_attribution_row(
    session_id: int,
    base_row: Mapping[str, Any],
    payload: Mapping[str, Any],
    source: str,
) -> dict[str, Any] | None:
    debug = _find_attribution_debug(payload)
    trigger = str(
        payload.get("entry_trigger_reason")
        or payload.get("trigger_reason")
        or payload.get("setup_reason")
        or payload.get("trigger")
        or debug.get("trigger_reason")
        or debug.get("setup_alias")
        or ""
    ).strip()
    relevant_keys = (
        "ask_eaten_confirmed",
        "ask_eaten_frac",
        "ask_eaten_pctile",
        "ask_lift_print_confirmed",
        "ask_lift_volume",
        "target_print_volume",
        "ask_lift_ratio",
        "target_print_ratio",
        "n_target_prints",
    )
    has_relevant = any(debug.get(key) is not None or payload.get(key) is not None for key in relevant_keys)
    if "absorption_snap" not in trigger and not has_relevant:
        return None
    get = lambda key: debug.get(key) if debug.get(key) is not None else payload.get(key)
    ask_eaten = _bool_or_none(get("ask_eaten_confirmed"))
    ask_lift = _bool_or_none(get("ask_lift_print_confirmed"))
    if ask_eaten is True and ask_lift is True:
        bucket = "ask_eaten_with_lifted_prints"
    elif ask_eaten is True:
        bucket = "ask_eaten_quote_only"
    elif ask_lift is True:
        bucket = "lifted_prints_without_quote_eaten"
    else:
        bucket = "no_ask_eaten_attribution"
    return {
        "session_id": session_id,
        "symbol": base_row.get("symbol"),
        "execution_family": base_row.get("execution_family"),
        "source": source,
        "trigger_reason": trigger or None,
        "bucket": bucket,
        "ask_eaten_confirmed": ask_eaten,
        "ask_eaten_frac": _float_or_none(get("ask_eaten_frac")),
        "ask_eaten_pctile": _float_or_none(get("ask_eaten_pctile")),
        "ask_lift_print_confirmed": ask_lift,
        "ask_lift_volume": _float_or_none(get("ask_lift_volume")),
        "target_print_volume": _float_or_none(get("target_print_volume")),
        "ask_lift_ratio": _float_or_none(get("ask_lift_ratio")),
        "target_print_ratio": _float_or_none(get("target_print_ratio")),
        "n_target_prints": int(get("n_target_prints") or 0),
    }


def build_setup_attribution_rows(
    session_rows: Iterable[Mapping[str, Any]],
    outcome_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Extract replay/grouping attribution from persisted setup debug fields."""

    def score(row: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
        return (
            int(row.get("ask_lift_print_confirmed") is True),
            int(row.get("ask_eaten_confirmed") is True),
            int((_float_or_none(row.get("ask_lift_volume")) or 0.0) > 0.0),
            int((_float_or_none(row.get("target_print_volume")) or 0.0) > 0.0),
            int(row.get("source") != "session_snapshot"),
        )

    by_session: dict[int, dict[str, Any]] = {}
    for sid, base, payload, source in _candidate_payloads_for_attribution(session_rows, outcome_rows):
        row = _setup_attribution_row(sid, base, payload, source)
        if row is None:
            continue
        if sid not in by_session or score(row) > score(by_session[sid]):
            by_session[sid] = row
    return tuple(by_session[key] for key in sorted(by_session))


def _normalize_opportunity_label_row(
    raw: Mapping[str, Any],
    *,
    session_id: int | None = None,
    symbol: str | None = None,
    source: str,
) -> dict[str, Any] | None:
    status = str(raw.get("status") or raw.get("opportunity_status") or "").strip()
    if not status:
        return None
    ready_raw = raw.get("label_ready")
    label_ready = bool(ready_raw) if ready_raw is not None else status in {"labeled_taken", "labeled_missed"}
    sid_raw = raw.get("session_id") if raw.get("session_id") is not None else session_id
    try:
        sid = int(sid_raw) if sid_raw is not None else None
    except (TypeError, ValueError):
        sid = None
    pnl = _float_or_none(raw.get("pnl_usd"))
    if pnl is None:
        pnl = _float_or_none(raw.get("realized_pnl_usd"))
    return {
        "session_id": sid,
        "symbol": str(raw.get("symbol") or symbol or "").upper() or None,
        "status": status,
        "label_ready": label_ready,
        "opportunity_ts": _iso_utc(raw.get("opportunity_ts") or raw.get("entry_ts")),
        "first_certifiable_source_ts": _iso_utc(raw.get("first_certifiable_source_ts")),
        "pnl_usd": pnl,
        "source": source,
    }


def _iter_opportunity_label_payloads(
    payload: Mapping[str, Any],
    *,
    session_id: int | None,
    symbol: str | None,
    source: str,
) -> Iterable[dict[str, Any]]:
    summary = payload.get("opportunity_label_summary")
    if isinstance(summary, Mapping):
        rows = summary.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping):
                    normalized = _normalize_opportunity_label_row(
                        row,
                        session_id=session_id,
                        symbol=symbol,
                        source=f"{source}:opportunity_label_summary",
                    )
                    if normalized is not None:
                        yield normalized
        return
    for key in (
        "counterfactual_opportunity_label",
        "counterfactual_opportunity",
        "market_path_opportunity_label",
        "opportunity_label",
        "replay_opportunity_label",
    ):
        row = payload.get(key)
        if isinstance(row, Mapping):
            normalized = _normalize_opportunity_label_row(
                row,
                session_id=session_id,
                symbol=symbol,
                source=f"{source}:{key}",
            )
            if normalized is not None:
                yield normalized


def build_opportunity_label_rows(
    session_rows: Iterable[Mapping[str, Any]],
    outcome_rows: Iterable[Mapping[str, Any]],
    snapshot_payloads: Iterable[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    """Extract explicit counterfactual opportunity labels for PnL certification.

    This only trusts persisted labels. It does not infer labels from prices,
    candidates, or broker fills, because PnL min/max certification requires
    reviewed source-before-opportunity evidence plus market-path replay.
    """

    out: list[dict[str, Any]] = []
    for row in session_rows:
        sid = int(row.get("session_id") or row.get("id") or 0) or None
        symbol = str(row.get("symbol") or "")
        snapshot = _mapping(row.get("risk_snapshot_json") or row.get("snapshot"))
        live_exec = _live_exec_from_snapshot(snapshot)
        for label in _iter_opportunity_label_payloads(
            snapshot,
            session_id=sid,
            symbol=symbol,
            source="session_snapshot",
        ):
            out.append(label)
        if live_exec:
            for label in _iter_opportunity_label_payloads(
                live_exec,
                session_id=sid,
                symbol=symbol,
                source="session_live_execution",
            ):
                out.append(label)
    for row in outcome_rows:
        sid = int(row.get("session_id") or 0) or None
        symbol = str(row.get("symbol") or "")
        payload = _mapping(row.get("payload_json"))
        for label in _iter_opportunity_label_payloads(
            payload,
            session_id=sid,
            symbol=symbol,
            source=str(row.get("event_type") or "event_payload"),
        ):
            out.append(label)
    for idx, payload in enumerate(snapshot_payloads):
        for label in _iter_opportunity_label_payloads(
            payload,
            session_id=None,
            symbol=None,
            source=f"snapshot_event_{idx}",
        ):
            out.append(label)
    return tuple(out)


def _event_status_for_replay(event_type: str, payload: Mapping[str, Any]) -> str:
    status = str(
        payload.get("status")
        or payload.get("outcome_status")
        or payload.get("broker_status")
        or ""
    ).lower()
    if status:
        return status
    event = str(event_type or "").lower()
    if "fill" in event and "no_fill" not in event:
        return "filled"
    if "cancel" in event:
        return "cancelled"
    if "reject" in event or "failed" in event:
        return "rejected"
    if "no_fill" in event:
        return "no_fill"
    return event


def _event_row(ev: TradingAutomationEvent, session_by_id: Mapping[int, dict[str, Any]]) -> dict[str, Any]:
    payload = _payload(ev)
    sess = session_by_id.get(int(ev.session_id), {})
    return {
        "id": int(ev.id),
        "session_id": int(ev.session_id),
        "event_type": str(ev.event_type or ""),
        "status": _event_status_for_replay(str(ev.event_type or ""), payload),
        "payload_json": payload,
        "risk_snapshot_json": sess.get("risk_snapshot_json") or {},
        "symbol": sess.get("symbol"),
        "venue": sess.get("venue"),
        "execution_family": sess.get("execution_family"),
        "ts": _iso_utc(ev.ts),
        "created_at": _iso_utc(ev.ts),
        "correlation_id": ev.correlation_id,
        "source_node_id": ev.source_node_id,
    }


def _event_reason(row: Mapping[str, Any]) -> str:
    payload = row.get("payload_json")
    if not isinstance(payload, Mapping):
        payload = {}
    return str(
        payload.get("reason")
        or payload.get("skipped")
        or payload.get("error")
        or row.get("status")
        or row.get("event_type")
        or ""
    ).lower()


def infer_replay_venue_states(
    session_rows: Iterable[Mapping[str, Any]],
    outcome_rows: Iterable[Mapping[str, Any]],
) -> tuple[ReplayVenueState, ...]:
    """Infer per-family venue health from explicit evidence in the export window."""

    family_venues: dict[str, str] = {}
    for row in session_rows:
        ef = normalize_execution_family(row.get("execution_family"))
        if ef:
            family_venues.setdefault(ef, str(row.get("venue") or ef))
    unavailable: set[str] = set()
    for row in outcome_rows:
        ef = normalize_execution_family(row.get("execution_family"))
        if not ef:
            continue
        family_venues.setdefault(ef, str(row.get("venue") or ef))
        if _event_reason(row) in _VENUE_UNAVAILABLE_REASONS:
            unavailable.add(ef)
    return tuple(
        ReplayVenueState(
            venue=family_venues.get(ef) or ef,
            execution_family=ef,
            adapter_available=ef not in unavailable,
        )
        for ef in sorted(family_venues)
    )


def _venue_state_from_payload(raw: Any) -> ReplayVenueState | None:
    if not isinstance(raw, Mapping):
        return None
    execution_family = normalize_execution_family(raw.get("execution_family"))
    venue = str(raw.get("venue") or execution_family or "").strip()
    if not execution_family or not venue:
        return None
    return ReplayVenueState(
        venue=venue,
        execution_family=execution_family,
        adapter_available=bool(raw.get("adapter_available", True)),
        venue_enabled=bool(raw.get("venue_enabled", True)),
        order_call_budget=max(0, int(raw.get("order_call_budget", 1) or 0)),
        risk_budget_slots=max(0, int(raw.get("risk_budget_slots", 1) or 0)),
    )


def _snapshot_step_from_event(ev: TradingAutomationEvent) -> ReplaySchedulerLiveSnapshotStep | None:
    payload = _payload(ev)
    raw_rows = payload.get("session_rows")
    if raw_rows is None:
        raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    rows = tuple(dict(row) for row in raw_rows if isinstance(row, Mapping))
    if not rows:
        return None
    raw_venues = payload.get("venue_states")
    venue_states = tuple(
        state
        for state in (_venue_state_from_payload(raw) for raw in raw_venues)
        if state is not None
    ) if isinstance(raw_venues, list) else ()
    if not venue_states:
        venue_states = infer_replay_venue_states(rows, ())
    ts = _iso_utc(payload.get("ts") or payload.get("as_of_utc") or ev.ts)
    if not ts:
        ts = datetime.now(timezone.utc).isoformat()
    return ReplaySchedulerLiveSnapshotStep(
        ts=ts,
        rows=rows,
        venue_states=venue_states,
        capacity_limit=payload.get("capacity_limit"),
        order_call_budget=payload.get("order_call_budget"),
        risk_budget_slots=payload.get("risk_budget_slots"),
    )


def _snapshot_payload_from_event(ev: TradingAutomationEvent) -> dict[str, Any]:
    return _payload(ev)


def export_live_replay_inputs(
    db: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 500,
    session_ids: Iterable[int] | None = None,
) -> LiveReplayExport:
    """Export live momentum rows for Replay v3 without mutating DB state."""

    lim = max(1, min(int(limit), 5_000))
    q = db.query(TradingAutomationSession).filter(TradingAutomationSession.mode == "live")
    if since is not None:
        q = q.filter(TradingAutomationSession.updated_at >= since)
    if until is not None:
        q = q.filter(TradingAutomationSession.updated_at <= until)
    explicit_session_scope = session_ids is not None
    if session_ids is not None:
        scoped = [int(sid) for sid in session_ids]
        if not scoped:
            rows: list[TradingAutomationSession] = []
        else:
            rows = q.filter(TradingAutomationSession.id.in_(scoped)).order_by(
                TradingAutomationSession.updated_at.desc(),
                TradingAutomationSession.id.desc(),
            ).limit(lim).all()
    else:
        rows = q.order_by(
            TradingAutomationSession.updated_at.desc(),
            TradingAutomationSession.id.desc(),
        ).limit(lim).all()

    session_rows = tuple(_session_row(sess) for sess in rows)
    session_by_id = {int(row["session_id"]): row for row in session_rows}
    selected_times = [
        dt
        for row in session_rows
        for dt in (_datetime_utc(row.get("created_at")), _datetime_utc(row.get("updated_at")))
        if dt is not None
    ]
    snapshot_since = since
    snapshot_until = until
    if not explicit_session_scope:
        snapshot_since = since if since is not None else (min(selected_times) if selected_times else None)
        snapshot_until = until if until is not None else (max(selected_times) if selected_times else None)

    outcome_rows: tuple[dict[str, Any], ...]
    snapshot_steps: tuple[ReplaySchedulerLiveSnapshotStep, ...]
    if session_by_id:
        evq = db.query(TradingAutomationEvent).filter(
            TradingAutomationEvent.session_id.in_(session_by_id),
            TradingAutomationEvent.event_type.in_(_REPLAY_OUTCOME_EVENT_TYPES),
        )
        if since is not None:
            evq = evq.filter(TradingAutomationEvent.ts >= since)
        if until is not None:
            evq = evq.filter(TradingAutomationEvent.ts <= until)
        outcome_rows = tuple(
            _event_row(ev, session_by_id)
            for ev in evq.order_by(TradingAutomationEvent.ts.asc(), TradingAutomationEvent.id.asc()).limit(lim).all()
        )
        if not explicit_session_scope and since is None and until is None:
            # Historical unbounded snapshot payloads can be very large. Keep broad
            # audits usable and fail closed to the single current-session snapshot;
            # pass an explicit time window or session ids to certify multi-snapshot
            # scheduler/PnL evidence.
            snap_events = ()
        else:
            snap_q = db.query(
                TradingAutomationEvent.id,
                TradingAutomationEvent.session_id,
                TradingAutomationEvent.event_type,
                TradingAutomationEvent.ts,
                TradingAutomationEvent.payload_json,
            ).filter(
                TradingAutomationEvent.event_type.in_(_REPLAY_SNAPSHOT_EVENT_TYPES),
            )
            if explicit_session_scope:
                snap_q = snap_q.filter(TradingAutomationEvent.session_id.in_(session_by_id))
            if snapshot_since is not None:
                snap_q = snap_q.filter(TradingAutomationEvent.ts >= snapshot_since)
            if snapshot_until is not None:
                snap_q = snap_q.filter(TradingAutomationEvent.ts <= snapshot_until)
            snap_events = snap_q.order_by(TradingAutomationEvent.ts.asc(), TradingAutomationEvent.id.asc()).limit(lim).all()
        steps = [
            step
            for step in (
                _snapshot_step_from_event(ev)
                for ev in snap_events
            )
            if step is not None
        ]
        snapshot_payloads = tuple(_snapshot_payload_from_event(ev) for ev in snap_events)
        snapshot_steps = tuple(steps)
    else:
        outcome_rows = ()
        snapshot_steps = ()
        snapshot_payloads = ()

    setup_attribution_rows = build_setup_attribution_rows(session_rows, outcome_rows)
    opportunity_label_rows = build_opportunity_label_rows(session_rows, outcome_rows, snapshot_payloads)
    venue_states = infer_replay_venue_states(session_rows, outcome_rows)
    as_of = _iso_utc(until) or datetime.now(timezone.utc).isoformat()
    step = ReplaySchedulerLiveSnapshotStep(
        ts=as_of,
        rows=session_rows,
        venue_states=venue_states,
    )
    if not snapshot_steps:
        snapshot_steps = (step,)
    return LiveReplayExport(
        as_of_utc=as_of,
        session_rows=session_rows,
        outcome_rows=outcome_rows,
        setup_attribution_rows=setup_attribution_rows,
        opportunity_label_rows=opportunity_label_rows,
        venue_states=venue_states,
        snapshot_step=step,
        snapshot_steps=snapshot_steps,
        broker_outcomes=tuple(replay_broker_outcomes_from_rows(outcome_rows)),
    )
