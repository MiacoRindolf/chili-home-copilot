"""Pure setup-trace coverage audit for momentum live events.

This module intentionally does not query the DB or touch brokers. Feed it
`TradingAutomationEvent`-shaped rows (or dict exports) and it reports the
coverage holes that caused the recent dark-flag incidents: setup aliases with
no stop/floor coverage, actionable waits that did not tick-arm, and waits whose
structural levels were not persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .entry_gates import TAPE_HOLD_VALID_WAIT_REASONS, TICK_ARMED_WAIT_REASONS

_KNOWN_NON_STRUCTURAL_ENTRY_ALIASES = frozenset(
    {
        "momentum_ok_rel_vol",
        "momentum_ok_abs_vol",
        "momentum_fallback_bar_only",
    }
)
_POSSIBLE_TRUNCATED_WINDOW_ISSUES = frozenset(
    {
        "exit_without_entry_fill",
        "partial_exit_without_entry_fill",
        "add_fill_without_prior_submit",
    }
)
_WAIT_EVENTS_REQUIRING_SETUP_TRACE = frozenset(
    {
        "live_entry_tick_scalp_wait",
        "live_entry_trigger_wait",
    }
)


@dataclass(frozen=True)
class SetupTraceAuditFinding:
    session_id: int | None
    ts: str | None
    event_type: str | None
    setup_alias: str | None
    reason: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class SetupTraceAuditReport:
    events_seen: int
    traces_seen: int
    findings: list[SetupTraceAuditFinding]
    lifecycle_summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def finding_reasons(self) -> list[str]:
        return [finding.reason for finding in self.findings]


def summarize_setup_trace_certification(report: SetupTraceAuditReport) -> dict[str, Any]:
    lifecycle = report.lifecycle_summary if isinstance(report.lifecycle_summary, Mapping) else {}
    issue_counts = lifecycle.get("issue_counts") if isinstance(lifecycle.get("issue_counts"), Mapping) else {}
    complete_counts = {
        "ordered_entry_add_exit": int(lifecycle.get("sessions_with_ordered_entry_add_exit") or 0),
        "runner_add": int(lifecycle.get("sessions_with_complete_runner_add_lifecycle") or 0),
        "anticipation_remainder": int(
            lifecycle.get("sessions_with_complete_anticipation_remainder_lifecycle") or 0
        ),
        "runner_exit": int(lifecycle.get("sessions_with_complete_runner_exit_lifecycle") or 0),
    }
    complete_lifecycle_count = sum(complete_counts.values())
    lifecycle_issue_count = sum(int(v or 0) for v in issue_counts.values())
    possible_truncated_window_issues = {
        str(key): int(value or 0)
        for key, value in issue_counts.items()
        if str(key) in _POSSIBLE_TRUNCATED_WINDOW_ISSUES
    }
    trace_coverage_ok = bool(report.ok and report.traces_seen > 0)
    lifecycle_order_ok = lifecycle_issue_count == 0
    return {
        "trace_coverage_ok": trace_coverage_ok,
        "trace_coverage_blocker": "" if trace_coverage_ok else (
            "setup_trace_findings_present" if report.findings else "no_setup_trace_events"
        ),
        "lifecycle_order_ok": lifecycle_order_ok,
        "window_completeness_ok": not possible_truncated_window_issues,
        "lifecycle_claim_ready": bool(trace_coverage_ok and lifecycle_order_ok and complete_lifecycle_count > 0),
        "complete_lifecycle_count": complete_lifecycle_count,
        "complete_lifecycle_counts": complete_counts,
        "lifecycle_issue_count": lifecycle_issue_count,
        "lifecycle_issue_counts": dict(sorted(issue_counts.items())),
        "possible_truncated_window_issue_count": sum(possible_truncated_window_issues.values()),
        "possible_truncated_window_issue_counts": dict(sorted(possible_truncated_window_issues.items())),
        "claim_boundary": (
            "Setup trace coverage can be clean while lifecycle certification is still not ready. "
            "Lifecycle claims require at least one complete ordered entry/add-or-exit lifecycle "
            "and no lifecycle ordering issues. If window_completeness_ok is false, rerun with "
            "a wider setup-trace window before classifying the lifecycle as a strategy failure."
        ),
    }


def _attr(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _payload(row: Any) -> dict[str, Any]:
    raw = _attr(row, "payload_json", None)
    if raw is None and isinstance(row, Mapping):
        raw = row.get("payload") or row.get("payload_json")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _truthy_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes"}:
            return True
        if v in {"false", "0", "no"}:
            return False
    return None


def _has_level(value: Any) -> bool:
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def _trace_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    trace = payload.get("setup_trace")
    if isinstance(trace, Mapping):
        return dict(trace)
    # Older event payloads may carry the same fields top-level. Normalize just
    # enough for the audit to remain useful on pre-telemetry rows.
    keys = {
        "setup_alias",
        "setup_reason",
        "trigger_reason",
        "source_wait_reason",
        "source_wait_tick_armed",
        "source_wait_tape_hold_eligible",
        "source_wait_has_pullback_levels",
        "structural_stop_covered",
        "a_setup_floor_covered",
        "pullback_high",
        "pullback_low",
        "structural_stop_price",
        "breakout_level_price",
        "setup_coverage",
    }
    out = {key: payload.get(key) for key in keys if key in payload}
    if "setup_alias" not in out:
        alias = payload.get("setup_reason") or payload.get("trigger_reason")
        if alias:
            out["setup_alias"] = alias
    has_trace_identity = any(
        out.get(key) is not None
        for key in (
            "setup_alias",
            "trigger_reason",
            "source_wait_reason",
            "structural_stop_covered",
            "a_setup_floor_covered",
            "setup_coverage",
        )
    )
    if not has_trace_identity:
        return {}
    return out


def _coverage_bool_from_trace(trace: Mapping[str, Any], key: str) -> bool | None:
    explicit = _truthy_bool(trace.get(key))
    if explicit is not None:
        return explicit
    coverage = str(trace.get("setup_coverage") or "").strip()
    if coverage == "structural_a_setup":
        return True
    if coverage == "non_structural_volume_fallback":
        return False
    return None


_ENTRY_FILL_EVENTS = frozenset(
    {
        "live_entry_fill",
        "live_entry_filled",
        "paper_entry_filled",
        "entry_fill",
    }
)
_TRAILING_EVENTS = frozenset({"live_trailing_armed"})
_ADD_SUBMIT_EVENTS = frozenset(
    {
        "live_anticipation_remainder_submitted",
        "live_pyramid_add_submitted",
        "live_micro_pullback_reentry_submitted",
        "live_pullback_add_fired",
        "live_flag_breakout_add_fired",
    }
)
_ADD_FILL_EVENTS = frozenset(
    {
        "live_pyramid_add",
        "live_micro_pullback_reentry_fill",
        "live_pullback_add_fill",
        "live_flag_breakout_add_fill",
        "live_anticipation_remainder_filled",
    }
)
_ADD_TERMINAL_NO_FILL_EVENTS = frozenset({"live_anticipation_remainder_terminal_no_fill"})
_EXIT_FILL_EVENTS = frozenset(
    {
        "live_exit_fill",
        "live_exit_filled",
        "paper_exit_filled",
        "exit_fill",
    }
)
_PARTIAL_EXIT_EVENTS = frozenset(
    {
        "live_partial_exit",
        "live_partial_exit_filled",
        "paper_partial_exit",
    }
)
_EXIT_ATTEMPT_PREFIXES = ("live_exit_",)


def _event_lifecycle_stage(event_type: str, payload: Mapping[str, Any]) -> str | None:
    event = str(event_type or "").strip()
    state = str(
        payload.get("state")
        or payload.get("live_state")
        or payload.get("new_state")
        or payload.get("session_state")
        or ""
    ).strip()
    if event in _ENTRY_FILL_EVENTS:
        return "entry_fill"
    if event in _TRAILING_EVENTS or state == "live_trailing":
        return "trailing_armed"
    if event in _ADD_SUBMIT_EVENTS:
        return "add_submit"
    if event in _ADD_FILL_EVENTS:
        return "add_fill"
    if event in _ADD_TERMINAL_NO_FILL_EVENTS:
        return "add_terminal_no_fill"
    if event in _PARTIAL_EXIT_EVENTS:
        return "partial_exit"
    if event in _EXIT_FILL_EVENTS:
        return "exit_fill"
    if event.startswith(_EXIT_ATTEMPT_PREFIXES):
        return "exit_attempt"
    if "setup_trace" in payload or _trace_from_payload(payload):
        return "setup_trace"
    return None


def _event_add_family(event_type: str) -> str | None:
    event = str(event_type or "")
    if "anticipation_remainder" in event:
        return "anticipation_remainder"
    if "pyramid_add" in event:
        return "pyramid"
    if "micro_pullback_reentry" in event:
        return "micro_pullback_reentry"
    if "pullback_add" in event:
        return "pullback_add"
    if "flag_breakout_add" in event:
        return "flag_breakout_add"
    return None


def _ordered_lifecycle_summary(
    session_events: Mapping[int, list[tuple[int, str, str]]],
) -> dict[str, Any]:
    """Summarize ordered entry/add/exit lifecycle evidence per session."""

    session_rows: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}

    def add_issue(issues: list[str], issue: str) -> None:
        issues.append(issue)
        issue_counts[issue] = issue_counts.get(issue, 0) + 1

    for sid, events in sorted(session_events.items()):
        ordered = sorted(events, key=lambda item: item[0])
        stages = [stage for _idx, _event, stage in ordered]
        event_names = [event for _idx, event, _stage in ordered]
        add_families = sorted(
            {
                family
                for _idx, event, stage in ordered
                if stage in {"add_submit", "add_fill", "add_terminal_no_fill"}
                for family in (_event_add_family(event),)
                if family
            }
        )
        first_idx: dict[str, int] = {}
        for idx, _event, stage in ordered:
            first_idx.setdefault(stage, idx)

        has_entry = "entry_fill" in first_idx
        has_trailing = "trailing_armed" in first_idx
        has_add_submit = "add_submit" in first_idx
        has_add_fill = "add_fill" in first_idx
        has_add_terminal = "add_terminal_no_fill" in first_idx
        has_partial = "partial_exit" in first_idx
        has_exit = "exit_fill" in first_idx
        has_runner_add_family = any(fam != "anticipation_remainder" for fam in add_families)
        has_anticipation = "anticipation_remainder" in add_families

        issues: list[str] = []
        if has_exit and not has_entry:
            add_issue(issues, "exit_without_entry_fill")
        if has_partial and not has_entry:
            add_issue(issues, "partial_exit_without_entry_fill")
        if has_add_fill and not has_add_submit:
            add_issue(issues, "add_fill_without_prior_submit")
        if has_runner_add_family and not has_trailing:
            add_issue(issues, "runner_add_without_trailing_arm")
        if (
            has_runner_add_family
            and has_trailing
            and has_add_submit
            and first_idx["add_submit"] < first_idx["trailing_armed"]
        ):
            add_issue(issues, "runner_add_submit_before_trailing_arm")
        if has_add_submit and not has_add_fill and not has_add_terminal and has_exit:
            add_issue(issues, "add_submit_unresolved_before_exit")

        session_rows.append(
            {
                "session_id": sid,
                "events": event_names,
                "stages": stages,
                "add_families": add_families,
                "issues": issues,
                "has_entry_fill": has_entry,
                "has_trailing_armed": has_trailing,
                "has_add_submit": has_add_submit,
                "has_add_fill": has_add_fill,
                "has_add_terminal_no_fill": has_add_terminal,
                "has_partial_exit": has_partial,
                "has_exit_fill": has_exit,
                "complete_entry_add_exit": bool(has_entry and has_add_fill and has_exit),
                "complete_runner_add_lifecycle": bool(
                    has_entry and has_trailing and has_runner_add_family and has_add_submit and has_add_fill and has_exit
                ),
                "complete_anticipation_remainder_lifecycle": bool(
                    has_entry and has_anticipation and has_add_submit and has_add_fill and has_exit
                ),
                "complete_runner_exit_lifecycle": bool(has_entry and has_partial and has_trailing and has_exit),
            }
        )

    return {
        "sessions": session_rows,
        "issue_counts": dict(sorted(issue_counts.items())),
        "sessions_with_ordered_entry_add_exit": sum(1 for row in session_rows if row["complete_entry_add_exit"]),
        "sessions_with_complete_runner_add_lifecycle": sum(
            1 for row in session_rows if row["complete_runner_add_lifecycle"]
        ),
        "sessions_with_complete_anticipation_remainder_lifecycle": sum(
            1 for row in session_rows if row["complete_anticipation_remainder_lifecycle"]
        ),
        "sessions_with_complete_runner_exit_lifecycle": sum(
            1 for row in session_rows if row["complete_runner_exit_lifecycle"]
        ),
    }


def audit_setup_trace_events(events: Iterable[Any]) -> SetupTraceAuditReport:
    """Audit setup trace coverage from event-shaped rows."""

    findings: list[SetupTraceAuditFinding] = []
    seen = 0
    traces = 0
    stage_counts: dict[str, int] = {}
    trace_alias_counts: Counter[str] = Counter()
    wait_reason_counts: Counter[str] = Counter()
    event_type_counts: Counter[str] = Counter()
    session_stages: dict[int, set[str]] = defaultdict(set)
    session_events: dict[int, list[tuple[int, str, str]]] = defaultdict(list)

    def add(row: Any, trace: Mapping[str, Any], reason: str, detail: dict[str, Any]) -> None:
        raw_ts = _attr(row, "ts", None)
        findings.append(
            SetupTraceAuditFinding(
                session_id=_attr(row, "session_id", None),
                ts=str(raw_ts) if raw_ts is not None else None,
                event_type=str(_attr(row, "event_type", "") or "") or None,
                setup_alias=str(trace.get("setup_alias") or trace.get("trigger_reason") or "") or None,
                reason=reason,
                detail=detail,
            )
        )

    for row in events:
        seen += 1
        payload = _payload(row)
        event_type = str(_attr(row, "event_type", "") or "")
        if event_type:
            event_type_counts[event_type] += 1
        stage = _event_lifecycle_stage(event_type, payload)
        if stage:
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            sid = _attr(row, "session_id", None)
            try:
                sid_int = int(sid)
                session_stages[sid_int].add(stage)
                session_events[sid_int].append((seen, event_type, stage))
            except (TypeError, ValueError):
                pass
        trace = _trace_from_payload(payload)
        if not trace:
            if event_type in _WAIT_EVENTS_REQUIRING_SETUP_TRACE:
                add(
                    row,
                    {"setup_alias": payload.get("setup_reason") or payload.get("trigger_reason") or ""},
                    "wait_event_missing_setup_trace",
                    {
                        "event_type": event_type,
                        "reason": payload.get("reason"),
                        "payload_keys": sorted(str(key) for key in payload.keys()),
                    },
                )
            continue
        traces += 1

        alias = str(trace.get("setup_alias") or trace.get("trigger_reason") or "").strip()
        wait = str(trace.get("source_wait_reason") or "").strip()
        if alias:
            trace_alias_counts[alias] += 1
        if wait:
            wait_reason_counts[wait] += 1
        pullback_high = trace.get("pullback_high") or payload.get("pullback_high")
        pullback_low = (
            trace.get("pullback_low")
            or trace.get("structural_stop_price")
            or payload.get("pullback_low")
            or payload.get("structural_stop_price")
        )
        has_levels = _has_level(pullback_high) and _has_level(pullback_low)

        structural = _coverage_bool_from_trace(trace, "structural_stop_covered")
        floor = _coverage_bool_from_trace(trace, "a_setup_floor_covered")
        non_structural = (
            trace.get("setup_coverage") == "non_structural_volume_fallback"
            or alias in _KNOWN_NON_STRUCTURAL_ENTRY_ALIASES
        )
        if alias and structural is not True and not non_structural:
            add(row, trace, "setup_alias_missing_structural_stop", {"setup_alias": alias})
        if alias and floor is not True and not non_structural:
            add(row, trace, "setup_alias_missing_a_setup_floor", {"setup_alias": alias})
        levels_flag = _truthy_bool(trace.get("source_wait_has_pullback_levels"))
        stop_level_required = event_type not in {"live_entry_pre_candidate_ross_shape_block"}
        if (
            event_type in _WAIT_EVENTS_REQUIRING_SETUP_TRACE
            and levels_flag is False
            and wait not in TICK_ARMED_WAIT_REASONS
            and wait not in TAPE_HOLD_VALID_WAIT_REASONS
        ):
            stop_level_required = False
        if structural is True and stop_level_required and not _has_level(pullback_low):
            add(row, trace, "structural_setup_missing_stop_level", {"setup_alias": alias})

        if wait:
            expected_tick = wait in TICK_ARMED_WAIT_REASONS
            expected_tape = wait in TAPE_HOLD_VALID_WAIT_REASONS
            tick_flag = _truthy_bool(trace.get("source_wait_tick_armed"))
            tape_flag = _truthy_bool(trace.get("source_wait_tape_hold_eligible"))

            if expected_tick and tick_flag is not True:
                add(row, trace, "wait_reason_not_tick_armed", {"source_wait_reason": wait})
            if expected_tape and tape_flag is not True:
                add(row, trace, "wait_reason_missing_tape_hold_eligibility", {"source_wait_reason": wait})
            if expected_tick and not has_levels:
                add(
                    row,
                    trace,
                    "wait_reason_missing_pullback_levels",
                    {
                        "source_wait_reason": wait,
                        "pullback_high_present": _has_level(pullback_high),
                        "pullback_low_present": _has_level(pullback_low),
                    },
                )
            if levels_flag is False and has_levels:
                add(row, trace, "wait_level_flag_false_but_levels_present", {"source_wait_reason": wait})

    ordered_summary = _ordered_lifecycle_summary(session_events)
    lifecycle_summary = {
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "trace_alias_counts": dict(sorted(trace_alias_counts.items())),
        "wait_reason_counts": dict(sorted(wait_reason_counts.items())),
        "stage_counts": dict(sorted(stage_counts.items())),
        "sessions_with_entry_fill": sum(1 for stages in session_stages.values() if "entry_fill" in stages),
        "sessions_with_trailing_armed": sum(1 for stages in session_stages.values() if "trailing_armed" in stages),
        "sessions_with_add_submit": sum(1 for stages in session_stages.values() if "add_submit" in stages),
        "sessions_with_add_fill": sum(1 for stages in session_stages.values() if "add_fill" in stages),
        "sessions_with_add_terminal_no_fill": sum(
            1 for stages in session_stages.values() if "add_terminal_no_fill" in stages
        ),
        "sessions_with_partial_exit": sum(1 for stages in session_stages.values() if "partial_exit" in stages),
        "sessions_with_exit_fill": sum(1 for stages in session_stages.values() if "exit_fill" in stages),
        "sessions_with_entry_and_trailing": sum(
            1 for stages in session_stages.values() if {"entry_fill", "trailing_armed"}.issubset(stages)
        ),
        "sessions_with_entry_and_add_submit": sum(
            1 for stages in session_stages.values() if {"entry_fill", "add_submit"}.issubset(stages)
        ),
        "sessions_with_entry_and_add": sum(
            1 for stages in session_stages.values() if {"entry_fill", "add_fill"}.issubset(stages)
        ),
        "sessions_with_entry_partial_and_exit": sum(
            1 for stages in session_stages.values() if {"entry_fill", "partial_exit", "exit_fill"}.issubset(stages)
        ),
        "sessions_with_entry_and_exit": sum(
            1 for stages in session_stages.values() if {"entry_fill", "exit_fill"}.issubset(stages)
        ),
        **ordered_summary,
    }
    return SetupTraceAuditReport(
        events_seen=seen,
        traces_seen=traces,
        findings=findings,
        lifecycle_summary=lifecycle_summary,
    )


def audit_recent_setup_trace_events(
    db: Any,
    *,
    limit: int = 200,
    session_id: int | None = None,
    session_ids: Iterable[int] | None = None,
    event_types: Sequence[str] | None = None,
) -> SetupTraceAuditReport:
    """Read-only DB wrapper for recent setup-trace event audits."""

    from ....models.trading import TradingAutomationEvent

    q = db.query(TradingAutomationEvent)
    scoped_ids: list[int] = []
    if session_ids is not None:
        scoped_ids.extend(int(sid) for sid in session_ids)
        if not scoped_ids and session_id is None:
            return audit_setup_trace_events(())
    if session_id is not None:
        scoped_ids.append(int(session_id))
    if scoped_ids:
        q = q.filter(TradingAutomationEvent.session_id.in_(sorted(set(scoped_ids))))
    if event_types:
        wanted = [str(t) for t in event_types if str(t or "").strip()]
        if wanted:
            q = q.filter(TradingAutomationEvent.event_type.in_(wanted))
    rows = (
        q.order_by(TradingAutomationEvent.ts.desc())
        .limit(max(1, min(int(limit or 1), 1000)))
        .all()
    )
    return audit_setup_trace_events(reversed(rows))
