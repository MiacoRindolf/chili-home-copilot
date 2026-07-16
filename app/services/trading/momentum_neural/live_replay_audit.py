"""Read-only live momentum Replay v3 audit runner."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ....models.trading import TradingAutomationEvent
from .live_replay_export import export_live_replay_inputs
from .replay_v3 import attribute_scheduler_timeline_pnl, replay_scheduler_timeline_from_live_snapshots
from .setup_trace_audit import audit_recent_setup_trace_events, summarize_setup_trace_certification


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).replace(tzinfo=None)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def _compact_trace_findings(findings: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for finding in findings:
        try:
            out.append(asdict(finding))
        except TypeError:
            out.append(dict(finding))
    return out


def _counter_dict(values: Iterable[Any]) -> dict[str, int]:
    counter = Counter(str(value or "") for value in values)
    counter.pop("", None)
    return dict(sorted(counter.items()))


def _sum_field(rows: Iterable[dict[str, Any]], key: str) -> float:
    total = 0.0
    for row in rows:
        try:
            total += float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
    return round(total, 4)


def _scheduler_starvation_evidence(timeline: Any) -> dict[str, Any]:
    free_skip_reasons = {
        "venue_adapter_unavailable",
        "venue_asset_class_mismatch",
        "venue_disabled",
        "wrong_venue",
        "pre_entry_terminal",
    }
    reason_counts: Counter[str] = Counter()
    free_skip_reason_counts: Counter[str] = Counter()
    capacity_consuming_reason_counts: Counter[str] = Counter()
    free_skip_count = 0
    unavailable_free_skip_count = 0
    steps_with_free_skip_and_selection = 0
    steps_with_unavailable_free_skip_and_selection = 0
    for step in getattr(timeline, "steps", []) or []:
        decisions = list(getattr(getattr(step, "batch", None), "decisions", []) or [])
        has_free_skip = False
        has_unavailable_free_skip = False
        has_selected = False
        for decision in decisions:
            reason = str(getattr(decision, "reason", "") or "")
            consumes = bool(getattr(decision, "consumes_capacity", True))
            if reason:
                reason_counts[reason] += 1
                if consumes:
                    capacity_consuming_reason_counts[reason] += 1
            if reason == "selected":
                has_selected = True
            if reason in free_skip_reasons and not consumes:
                free_skip_count += 1
                free_skip_reason_counts[reason] += 1
                has_free_skip = True
                if reason in {
                    "venue_adapter_unavailable",
                    "venue_asset_class_mismatch",
                    "venue_disabled",
                    "wrong_venue",
                }:
                    unavailable_free_skip_count += 1
                    has_unavailable_free_skip = True
        if has_free_skip and has_selected:
            steps_with_free_skip_and_selection += 1
        if has_unavailable_free_skip and has_selected:
            steps_with_unavailable_free_skip_and_selection += 1
    return {
        "free_skip_count": free_skip_count,
        "unavailable_free_skip_count": unavailable_free_skip_count,
        "decision_reason_counts": dict(sorted(reason_counts.items())),
        "free_skip_reason_counts": dict(sorted(free_skip_reason_counts.items())),
        "capacity_consuming_reason_counts": dict(sorted(capacity_consuming_reason_counts.items())),
        "steps_with_free_skip_and_selection": steps_with_free_skip_and_selection,
        "steps_with_unavailable_free_skip_and_selection": steps_with_unavailable_free_skip_and_selection,
        "adapter_unavailable_starvation_claim_ready": bool(
            unavailable_free_skip_count > 0 and steps_with_unavailable_free_skip_and_selection > 0
        ),
        "claim_model": (
            "A scheduler starvation claim requires observed free pre-capacity skips "
            "for unavailable/wrong-venue rows plus a selected candidate in the same replay step."
        ),
    }


def _scheduler_priority_evidence(timeline: Any) -> dict[str, Any]:
    budget_reasons = {
        "capacity_exhausted",
        "order_call_budget_exhausted",
        "risk_budget_exhausted",
    }
    budget_skip_count = 0
    steps_with_budget_skip_and_selection = 0
    for step in getattr(timeline, "steps", []) or []:
        decisions = list(getattr(getattr(step, "batch", None), "decisions", []) or [])
        has_budget_skip = False
        has_selected = False
        for decision in decisions:
            reason = str(getattr(decision, "reason", "") or "")
            if reason == "selected":
                has_selected = True
            if reason in budget_reasons:
                budget_skip_count += 1
                has_budget_skip = True
        if has_budget_skip and has_selected:
            steps_with_budget_skip_and_selection += 1

    delayed_selected_count = 0
    for rows in (getattr(timeline, "decision_trace", {}) or {}).values():
        reasons = [str(row.get("reason") or "") for row in rows if isinstance(row, dict)]
        if "selected" in reasons and any(reason in budget_reasons for reason in reasons):
            delayed_selected_count += 1

    selected_count = len(getattr(timeline, "selected_session_ids", []) or [])
    ready = bool(selected_count > 0 and (steps_with_budget_skip_and_selection > 0 or delayed_selected_count > 0))
    return {
        "budget_skip_count": budget_skip_count,
        "steps_with_budget_skip_and_selection": steps_with_budget_skip_and_selection,
        "delayed_then_selected_count": delayed_selected_count,
        "selected_count": selected_count,
        "scheduler_priority_claim_ready": ready,
        "claim_model": (
            "A scheduler-priority claim requires observed capacity/order/risk-budget pressure "
            "with a selected row in the same step, or a budget-delayed row selected later."
        ),
    }


def _pnl_evidence(attribution: Any, *, broker_outcome_count: int) -> dict[str, Any]:
    selected = list(getattr(attribution, "selected_session_ids", []) or [])
    missing = list(getattr(attribution, "selected_without_outcome_ids", []) or [])
    realized = list(getattr(attribution, "realized_session_ids", []) or [])
    rejected = list(getattr(attribution, "rejected_session_ids", []) or [])
    no_fill = list(getattr(attribution, "no_fill_session_ids", []) or [])
    selected_count = len(selected)
    outcome_count = len(realized) + len(rejected) + len(no_fill)
    complete_selected_outcomes = bool(selected_count > 0 and outcome_count == selected_count and not missing)
    return {
        "selected_count": selected_count,
        "broker_outcome_count": int(broker_outcome_count),
        "selected_with_realized_count": len(realized),
        "selected_rejected_count": len(rejected),
        "selected_no_fill_count": len(no_fill),
        "selected_missing_outcome_count": len(missing),
        "complete_selected_outcomes": complete_selected_outcomes,
        "realized_vs_selected_expected_claim_ready": complete_selected_outcomes,
        "realized_pnl_usd": float(getattr(attribution, "realized_pnl_usd", 0.0) or 0.0),
        "selected_expected_pnl_usd": float(getattr(attribution, "selected_expected_pnl_usd", 0.0) or 0.0),
        "realized_vs_selected_expected_usd": float(
            getattr(attribution, "realized_vs_selected_expected_usd", 0.0) or 0.0
        ),
        "pnl_minmax_claim_ready": False,
        "pnl_minmax_missing_evidence": [
            "market_path_counterfactual_opportunity_labels",
            "complete_missed_vs_taken_outcome_labels",
        ],
        "claim_model": (
            "Realized-vs-selected-expected PnL is certifiable when every replay-selected "
            "session has a broker outcome. PnL min/max remains blocked until replay also "
            "has market-path counterfactual opportunity labels for missed and taken rows."
        ),
    }


def _opportunity_label_evidence(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    labels = [dict(row) for row in rows]
    total = len(labels)
    ready = [row for row in labels if bool(row.get("label_ready"))]
    status_counts = _counter_dict(row.get("status") for row in labels)
    taken = [row for row in ready if str(row.get("status") or "") == "labeled_taken"]
    missed = [row for row in ready if str(row.get("status") or "") == "labeled_missed"]
    complete = bool(total > 0 and len(ready) == total)
    return {
        "row_count": total,
        "label_ready_count": len(ready),
        "taken_label_count": len(taken),
        "missed_label_count": len(missed),
        "status_counts": status_counts,
        "has_market_path_counterfactual_opportunity_labels": total > 0,
        "complete_missed_vs_taken_outcome_labels": complete,
        "pnl_usd_labeled": round(_sum_field(ready, "pnl_usd"), 4),
        "rows": labels,
        "claim_model": (
            "PnL min/max needs explicit market-path counterfactual opportunity labels. "
            "The audit trusts persisted labels only; it does not infer labels from live fills."
        ),
    }


def _arm_lifecycle_evidence(
    db: Session,
    *,
    session_ids: Iterable[int],
    until: datetime | None = None,
) -> dict[str, Any]:
    """Summarize live arm -> confirm -> runner lifecycle gaps for exported sessions.

    This is read-only diagnostic evidence. It intentionally does not use the
    audit's lower-bound timestamp because an arm can be requested before the
    visible incident window and expire inside it. That exact shape should be
    reported as "no confirmed runner", not misclassified as a setup gate miss.
    """

    ids = tuple(sorted({int(sid) for sid in session_ids if sid is not None}))
    if not ids:
        return {
            "session_count": 0,
            "arm_requested_count": 0,
            "arm_confirmed_count": 0,
            "runner_started_count": 0,
            "arm_expired_count": 0,
            "requested_without_confirm_count": 0,
            "confirmed_without_runner_count": 0,
            "expired_without_runner_count": 0,
            "samples": [],
            "claim_model": (
                "A setup-gate conclusion requires a confirmed runner. Arm rows "
                "that expire before confirmation/runner start are lifecycle gaps, "
                "not entry setup refusals."
            ),
        }

    event_types = {
        "live_arm_requested",
        "live_arm_confirmed",
        "live_runner_started",
        "live_watch_started",
        "live_arm_expired",
        "live_declined",
    }
    q = db.query(TradingAutomationEvent).filter(
        TradingAutomationEvent.session_id.in_(ids),
        TradingAutomationEvent.event_type.in_(event_types),
    )
    if until is not None:
        q = q.filter(TradingAutomationEvent.ts <= until)
    events_by_session: dict[int, list[TradingAutomationEvent]] = {sid: [] for sid in ids}
    for ev in q.order_by(TradingAutomationEvent.ts.asc(), TradingAutomationEvent.id.asc()).all():
        events_by_session.setdefault(int(ev.session_id), []).append(ev)

    counters: Counter[str] = Counter()
    requested_without_confirm: list[int] = []
    confirmed_without_runner: list[int] = []
    expired_without_runner: list[int] = []
    samples: list[dict[str, Any]] = []
    for sid in ids:
        events = events_by_session.get(sid) or []
        types = [str(ev.event_type or "") for ev in events]
        type_set = set(types)
        for event_type in event_types:
            if event_type in type_set:
                counters[event_type] += 1
        has_requested = "live_arm_requested" in type_set
        has_confirmed = "live_arm_confirmed" in type_set
        has_runner = "live_runner_started" in type_set or "live_watch_started" in type_set
        has_expired = "live_arm_expired" in type_set
        reason: str | None = None
        if has_requested and not has_confirmed:
            requested_without_confirm.append(sid)
            reason = "arm_requested_without_confirm"
        elif has_confirmed and not has_runner:
            confirmed_without_runner.append(sid)
            reason = "arm_confirmed_without_runner"
        if has_expired and not has_runner:
            expired_without_runner.append(sid)
            reason = reason or "arm_expired_without_runner"
        if reason and len(samples) < 8:
            samples.append(
                {
                    "session_id": sid,
                    "reason": reason,
                    "event_types": types,
                    "first_event_ts": _iso_utc(events[0].ts) if events else None,
                    "last_event_ts": _iso_utc(events[-1].ts) if events else None,
                }
            )

    return {
        "session_count": len(ids),
        "arm_requested_count": counters["live_arm_requested"],
        "arm_confirmed_count": counters["live_arm_confirmed"],
        "runner_started_count": counters["live_runner_started"],
        "watch_started_count": counters["live_watch_started"],
        "arm_expired_count": counters["live_arm_expired"],
        "declined_count": counters["live_declined"],
        "requested_without_confirm_count": len(requested_without_confirm),
        "confirmed_without_runner_count": len(confirmed_without_runner),
        "expired_without_runner_count": len(expired_without_runner),
        "requested_without_confirm_session_ids": requested_without_confirm,
        "confirmed_without_runner_session_ids": confirmed_without_runner,
        "expired_without_runner_session_ids": expired_without_runner,
        "samples": samples,
        "claim_model": (
            "A setup-gate conclusion requires a confirmed runner. Arm rows that "
            "expire before confirmation/runner start are lifecycle gaps, not "
            "entry setup refusals."
        ),
    }


def _certification_boundary(
    *,
    timeline: Any,
    attribution: Any,
    broker_outcome_count: int,
    session_row_count: int,
    opportunity_labels: dict[str, Any],
) -> dict[str, Any]:
    step_count = len(getattr(timeline, "steps", []) or [])
    selected_count = len(getattr(timeline, "selected_session_ids", []) or [])
    has_multi_tick = step_count >= 2
    has_broker_outcomes = broker_outcome_count > 0
    has_sessions = session_row_count > 0
    starvation = _scheduler_starvation_evidence(timeline)
    priority = _scheduler_priority_evidence(timeline)
    pnl = _pnl_evidence(attribution, broker_outcome_count=broker_outcome_count)
    priority_ready = bool(has_sessions and priority["scheduler_priority_claim_ready"])
    complete_selected_outcomes = bool(pnl["complete_selected_outcomes"])
    has_opportunity_labels = bool(opportunity_labels["has_market_path_counterfactual_opportunity_labels"])
    complete_opportunity_labels = bool(opportunity_labels["complete_missed_vs_taken_outcome_labels"])
    pnl_minmax_ready = bool(has_multi_tick and complete_selected_outcomes and complete_opportunity_labels)
    missing_evidence: list[str] = []
    if not has_sessions:
        missing_evidence.append("live_session_rows")
    if not has_multi_tick:
        missing_evidence.append("multi_snapshot_scheduler_timeline")
    if selected_count <= 0:
        missing_evidence.append("replay_selected_sessions")
    elif not complete_selected_outcomes:
        missing_evidence.append("complete_selected_broker_outcomes")
    if not priority_ready:
        missing_evidence.append("scheduler_pressure_or_delayed_selection_evidence")
    if not bool(starvation["adapter_unavailable_starvation_claim_ready"]):
        missing_evidence.append("adapter_unavailable_same_step_selection_evidence")
    if not has_opportunity_labels:
        missing_evidence.append("market_path_counterfactual_opportunity_labels")
    if not complete_opportunity_labels:
        missing_evidence.append("complete_missed_vs_taken_outcome_labels")
    return {
        "scheduler_timeline_step_count": step_count,
        "input_shape": "multi_snapshot_timeline" if has_multi_tick else "single_snapshot_batch",
        "evidence_status": {
            "has_live_session_rows": has_sessions,
            "has_multi_snapshot_timeline": has_multi_tick,
            "has_scheduler_pressure_or_delay_evidence": priority_ready,
            "has_adapter_unavailable_same_step_selection_evidence": bool(
                starvation["adapter_unavailable_starvation_claim_ready"]
            ),
            "has_selected_sessions": selected_count > 0,
            "has_broker_outcomes": has_broker_outcomes,
            "has_complete_selected_outcomes": complete_selected_outcomes,
            "has_market_path_counterfactual_opportunity_labels": has_opportunity_labels,
            "has_complete_missed_vs_taken_outcome_labels": complete_opportunity_labels,
        },
        "missing_evidence": missing_evidence,
        "scheduler_priority_claim_ready": priority_ready,
        "scheduler_priority_claim_level": (
            "scheduler_pressure_observed"
            if priority_ready
            else (
                "multi_snapshot_no_scheduler_pressure"
                if has_multi_tick and has_sessions
                else "single_snapshot_batch_only"
            )
        ),
        "adapter_unavailable_starvation_claim_ready": bool(
            starvation["adapter_unavailable_starvation_claim_ready"]
        ),
        "broker_outcome_attribution_ready": bool(selected_count and has_broker_outcomes),
        "realized_vs_selected_expected_claim_ready": bool(
            pnl["realized_vs_selected_expected_claim_ready"]
        ),
        "pnl_minmax_claim_ready": pnl_minmax_ready,
        "pnl_minmax_blocker": (
            "live_export_has_no_historical_intra_session_snapshot_timeline"
            if not has_multi_tick
            else (
                ""
                if pnl_minmax_ready
                else "requires_market_path_counterfactuals_and_complete_outcome_labels"
            )
        ),
        "claim_boundary": (
            "Single-snapshot live export can prove candidate extraction and current-batch "
            "capacity behavior, but cannot certify scheduler-slot PnL min/max or delayed "
            "candidate outcomes over time."
            if not has_multi_tick
            else "Multi-tick scheduler replay can test starvation/delay mechanics, but PnL "
            "min/max still needs complete broker outcomes and counterfactual opportunity labels."
        ),
    }


def _replay_evidence_debt(certification: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate missing replay proof legs into operator-actionable evidence debt.

    This is deliberately diagnostic. Missing proof must block certification claims,
    but it must not become a hidden live-trading enablement gate.
    """

    missing = set(str(item) for item in certification.get("missing_evidence") or [])
    recipes: dict[str, dict[str, Any]] = {
        "live_session_rows": {
            "blocks": ["setup_lifecycle", "scheduler_priority", "pnl_minmax"],
            "evidence_needed": "At least one live momentum session row in the selected replay window.",
            "how_to_collect": (
                "Run the audit over a real trading window after the event-driven worker has seen live Ross sessions; "
                "avoid using an empty post-restart/market-holiday window as lifecycle proof."
            ),
        },
        "multi_snapshot_scheduler_timeline": {
            "blocks": ["scheduler_priority", "adapter_starvation", "pnl_minmax"],
            "evidence_needed": "Two or more persisted scheduler/event-loop snapshot steps for the same replay window.",
            "how_to_collect": (
                "Keep live_replay_event_snapshot emission enabled, then audit a tradable window with event-loop ticks "
                "or explicit scheduler snapshot events. This proves delay/selection over time instead of a one-shot batch."
            ),
        },
        "replay_selected_sessions": {
            "blocks": ["broker_outcome_attribution", "pnl_minmax"],
            "evidence_needed": "At least one replay-selected candidate in the scheduler timeline.",
            "how_to_collect": (
                "Use a window with actionable queued/watch candidates and nonzero capacity; inspect scheduler.decision_trace "
                "if all rows are terminalized, wrong-venue, market-closed, or risk-blocked."
            ),
        },
        "complete_selected_broker_outcomes": {
            "blocks": ["realized_vs_selected_expected", "pnl_minmax"],
            "evidence_needed": "Every replay-selected session must have a realized, rejected, or no-fill broker outcome.",
            "how_to_collect": (
                "Run after broker reconciliation/outcome events have landed; missing outcomes are evidence debt, not assumed fills."
            ),
        },
        "scheduler_pressure_or_delayed_selection_evidence": {
            "blocks": ["scheduler_priority"],
            "evidence_needed": "Observed capacity/order/risk-budget pressure with a same-step selection, or delayed row selected later.",
            "how_to_collect": (
                "Audit a multi-session window where useful capacity is actually constrained; Replay should show budget skips "
                "plus selected rows or delayed-then-selected traces."
            ),
        },
        "adapter_unavailable_same_step_selection_evidence": {
            "blocks": ["adapter_starvation"],
            "evidence_needed": "Unavailable/wrong-venue/pre-entry terminal rows skipped free of capacity while an eligible row is selected in the same step.",
            "how_to_collect": (
                "Use a window containing adapter-unavailable or wrong-venue rows ahead of valid equity candidates; "
                "the replay decision trace must show free skips and same-step equity selection."
            ),
        },
        "market_path_counterfactual_opportunity_labels": {
            "blocks": ["pnl_minmax"],
            "evidence_needed": "Persisted market-path opportunity labels for missed/taken candidates.",
            "how_to_collect": (
                "Run the joined Replay/visual certification queue and add reviewed chart-context source-before-opportunity "
                "labels where appropriate. The audit will not infer opportunity labels from fills alone."
            ),
        },
        "complete_missed_vs_taken_outcome_labels": {
            "blocks": ["pnl_minmax"],
            "evidence_needed": "All persisted opportunity rows must be label-ready as taken or missed.",
            "how_to_collect": (
                "Finish the source-before-opportunity visual/replay labeling queue; noncertified or late-source rows must remain fail-closed."
            ),
        },
    }
    out: list[dict[str, Any]] = []
    for key in sorted(missing):
        row = dict(recipes.get(key) or {})
        row.setdefault("blocks", ["unknown_claim"])
        row.setdefault("evidence_needed", key)
        row.setdefault("how_to_collect", "Add explicit Replay telemetry or tests for this proof leg.")
        row["missing_evidence"] = key
        row["enablement_gate"] = False
        row["claim_gate"] = True
        out.append(row)
    return out


def run_live_replay_audit(
    db: Session,
    *,
    since: datetime | str | None = None,
    until: datetime | str | None = None,
    limit: int = 500,
    session_ids: Iterable[int] | None = None,
    capacity_limit: int = 25,
    order_call_budget: int | None = None,
    risk_budget_slots: int | None = None,
    setup_trace_limit: int = 500,
) -> dict[str, Any]:
    """Run the read-only export -> replay -> attribution audit.

    The caller owns transaction handling. This function performs no writes and no
    broker calls. It only reads automation sessions/events and returns a compact
    JSON-friendly summary for operator certification.
    """

    since_dt = _parse_dt(since) if isinstance(since, str) else since
    until_dt = _parse_dt(until) if isinstance(until, str) else until
    requested_session_ids = tuple(int(sid) for sid in session_ids) if session_ids is not None else None
    export = export_live_replay_inputs(
        db,
        since=since_dt,
        until=until_dt,
        limit=limit,
        session_ids=requested_session_ids,
    )
    timeline = replay_scheduler_timeline_from_live_snapshots(
        export.snapshot_steps,
        default_capacity_limit=max(1, int(capacity_limit)),
        default_order_call_budget=order_call_budget,
        default_risk_budget_slots=risk_budget_slots,
    )
    attribution = attribute_scheduler_timeline_pnl(timeline, export.broker_outcomes)
    trace_report = audit_recent_setup_trace_events(
        db,
        limit=setup_trace_limit,
        session_ids=tuple(
            int(row.get("session_id"))
            for row in export.session_rows
            if row.get("session_id") is not None
        ),
    )
    unavailable_families = [
        state.execution_family
        for state in export.venue_states
        if not bool(state.adapter_available and state.venue_enabled)
    ]
    state_counts = _counter_dict(row.get("state") for row in export.session_rows)
    family_counts = _counter_dict(row.get("execution_family") for row in export.session_rows)
    attribution_rows = list(export.setup_attribution_rows)
    attribution_buckets = _counter_dict(row.get("bucket") for row in attribution_rows)
    selected_count = len(timeline.selected_session_ids)
    terminalized_count = len(timeline.terminalized_session_ids)
    pending_count = len(timeline.pending_session_ids)
    certification = _certification_boundary(
        timeline=timeline,
        attribution=attribution,
        broker_outcome_count=len(export.broker_outcomes),
        session_row_count=len(export.session_rows),
        opportunity_labels=_opportunity_label_evidence(export.opportunity_label_rows),
    )
    evidence_debt = _replay_evidence_debt(certification)
    exported_session_ids = tuple(
        int(row.get("session_id"))
        for row in export.session_rows
        if row.get("session_id") is not None
    )
    arm_lifecycle = _arm_lifecycle_evidence(db, session_ids=exported_session_ids, until=until_dt)
    starvation_evidence = _scheduler_starvation_evidence(timeline)
    priority_evidence = _scheduler_priority_evidence(timeline)
    pnl_evidence = _pnl_evidence(attribution, broker_outcome_count=len(export.broker_outcomes))
    opportunity_label_evidence = _opportunity_label_evidence(export.opportunity_label_rows)
    pnl_evidence["pnl_minmax_claim_ready"] = bool(certification["pnl_minmax_claim_ready"])
    pnl_evidence["pnl_minmax_missing_evidence"] = [
        item
        for item in pnl_evidence["pnl_minmax_missing_evidence"]
        if item in certification["missing_evidence"]
    ]
    no_selected_reason = None
    if selected_count == 0:
        if export.session_rows and terminalized_count >= len(export.session_rows):
            no_selected_reason = "no_selectable_sessions_in_export"
        elif not export.session_rows:
            no_selected_reason = "no_session_rows_exported"
        else:
            no_selected_reason = "all_candidates_skipped_or_budgeted"
    return {
        "ok": bool(trace_report.ok),
        "as_of_utc": export.as_of_utc,
        "read_only": True,
        "inputs": {
            "since_utc": _iso_utc(since_dt),
            "until_utc": _iso_utc(until_dt),
            "session_rows": len(export.session_rows),
            "outcome_rows": len(export.outcome_rows),
            "setup_attribution_rows": len(export.setup_attribution_rows),
            "opportunity_label_rows": len(export.opportunity_label_rows),
            "scheduler_snapshot_steps": len(export.snapshot_steps),
            "venue_states": len(export.venue_states),
            "broker_outcomes": len(export.broker_outcomes),
            "session_state_counts": state_counts,
            "execution_family_counts": family_counts,
        },
        "scheduler": {
            "selected_count": selected_count,
            "terminalized_count": terminalized_count,
            "pending_count": pending_count,
            "no_selected_reason": no_selected_reason,
            "selected_session_ids": timeline.selected_session_ids,
            "terminalized_session_ids": timeline.terminalized_session_ids,
            "pending_session_ids": timeline.pending_session_ids,
            "selected_expected_pnl_usd": timeline.selected_expected_pnl_usd,
            "missed_expected_pnl_usd": timeline.missed_expected_pnl_usd,
            "open_expected_pnl_usd": timeline.open_expected_pnl_usd,
            "skipped_expected_pnl_by_reason": timeline.skipped_expected_pnl_by_reason,
            "unavailable_execution_families": unavailable_families,
            "starvation_evidence": starvation_evidence,
            "priority_evidence": priority_evidence,
        },
        "pnl_attribution": asdict(attribution),
        "pnl_evidence": pnl_evidence,
        "opportunity_label_evidence": opportunity_label_evidence,
        "certification": certification,
        "replay_evidence_debt": evidence_debt,
        "arm_lifecycle": arm_lifecycle,
        "setup_attribution": {
            "row_count": len(attribution_rows),
            "bucket_counts": attribution_buckets,
            "ask_lift_volume": _sum_field(attribution_rows, "ask_lift_volume"),
            "target_print_volume": _sum_field(attribution_rows, "target_print_volume"),
            "rows": attribution_rows,
        },
        "setup_trace": {
            "ok": trace_report.ok,
            "events_seen": trace_report.events_seen,
            "traces_seen": trace_report.traces_seen,
            "finding_count": len(trace_report.findings),
            "finding_reasons": trace_report.finding_reasons,
            "findings": _compact_trace_findings(trace_report.findings),
            "lifecycle_summary": trace_report.lifecycle_summary,
            "certification": summarize_setup_trace_certification(trace_report),
        },
        "boundary": (
            "Replay uses exported live DB snapshots/events. It does not execute broker calls, "
            "simulate DB locks, or reconstruct historical intra-session snapshots that were not persisted. "
            "Use certification.scheduler_priority_claim_ready and certification.pnl_minmax_claim_ready "
            "before making scheduler/PnL claims."
        ),
    }
