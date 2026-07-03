"""Read-only CHILI momentum live Replay v3 audit.

Example:
  python scripts/audit_momentum_live_replay.py --limit 500 --capacity-limit 25
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.trading.momentum_neural.live_replay_audit import run_live_replay_audit  # noqa: E402

CANONICAL_MOMENTUM_WORKER = "chili-clean-recovery-momentum-exec"


def _certification_failures(
    summary: dict,
    *,
    require_current_window_evidence: bool = False,
    require_adapter_starvation_claim: bool = False,
    require_scheduler_priority_claim: bool = False,
    require_pnl_minmax_claim: bool = False,
    require_realized_vs_expected_pnl: bool = False,
    require_broker_outcome_attribution: bool = False,
    require_setup_trace_coverage: bool = False,
    require_setup_lifecycle_claim: bool = False,
) -> list[str]:
    cert = summary.get("certification") if isinstance(summary.get("certification"), dict) else {}
    setup_trace = summary.get("setup_trace") if isinstance(summary.get("setup_trace"), dict) else {}
    setup_cert = (
        setup_trace.get("certification")
        if isinstance(setup_trace.get("certification"), dict)
        else {}
    )
    failures: list[str] = []
    if require_current_window_evidence:
        inputs = summary.get("inputs") if isinstance(summary.get("inputs"), dict) else {}
        session_rows = int(inputs.get("session_rows") or 0)
        traces_seen = int(setup_trace.get("traces_seen") or 0)
        if session_rows <= 0 or traces_seen <= 0:
            failures.append(
                "current_window_evidence_not_ready:"
                f"sessions={session_rows}:"
                f"traces={traces_seen}:"
                f"findings={setup_trace.get('finding_count') or 0}"
            )
    if require_scheduler_priority_claim and not bool(cert.get("scheduler_priority_claim_ready")):
        failures.append(
            "scheduler_priority_claim_not_ready:"
            f"{cert.get('scheduler_priority_claim_level') or 'missing_certification'}"
        )
    if require_adapter_starvation_claim and not bool(cert.get("adapter_unavailable_starvation_claim_ready")):
        scheduler = summary.get("scheduler") if isinstance(summary.get("scheduler"), dict) else {}
        starvation = (
            scheduler.get("starvation_evidence")
            if isinstance(scheduler.get("starvation_evidence"), dict)
            else {}
        )
        failures.append(
            "adapter_unavailable_starvation_claim_not_ready:"
            f"free={starvation.get('unavailable_free_skip_count') or 0}:"
            f"same_step_selected={starvation.get('steps_with_unavailable_free_skip_and_selection') or 0}"
        )
    if require_pnl_minmax_claim and not bool(cert.get("pnl_minmax_claim_ready")):
        failures.append(
            "pnl_minmax_claim_not_ready:"
            f"{cert.get('pnl_minmax_blocker') or 'missing_certification'}"
        )
    if require_realized_vs_expected_pnl and not bool(cert.get("realized_vs_selected_expected_claim_ready")):
        pnl_ev = summary.get("pnl_evidence") if isinstance(summary.get("pnl_evidence"), dict) else {}
        failures.append(
            "realized_vs_expected_pnl_not_ready:"
            f"selected={pnl_ev.get('selected_count') or 0}:"
            f"missing={pnl_ev.get('selected_missing_outcome_count') or 0}:"
            f"outcomes={pnl_ev.get('broker_outcome_count') or 0}"
        )
    if require_broker_outcome_attribution and not bool(cert.get("broker_outcome_attribution_ready")):
        pnl = summary.get("pnl_attribution") if isinstance(summary.get("pnl_attribution"), dict) else {}
        failures.append(
            "broker_outcome_attribution_not_ready:"
            f"selected={len(pnl.get('selected_session_ids') or [])}:"
            f"realized={len(pnl.get('realized_session_ids') or [])}:"
            f"missing={len(pnl.get('selected_without_outcome_ids') or [])}"
        )
    if require_setup_trace_coverage and not bool(setup_cert.get("trace_coverage_ok")):
        inputs = summary.get("inputs") if isinstance(summary.get("inputs"), dict) else {}
        session_rows = int(inputs.get("session_rows") or 0)
        traces_seen = int(setup_trace.get("traces_seen") or 0)
        if session_rows > 0 or traces_seen > 0:
            reason_counts = Counter(str(r) for r in setup_trace.get("finding_reasons") or [] if str(r or ""))
            failures.append(
                "setup_trace_coverage_not_ready:"
                f"findings={setup_trace.get('finding_count') or 0}:"
                f"reasons={','.join(f'{reason}={count}' for reason, count in sorted(reason_counts.items()))}"
            )
    if require_setup_lifecycle_claim and not bool(setup_cert.get("lifecycle_claim_ready")):
        failures.append(
            "setup_lifecycle_claim_not_ready:"
            f"trace={bool(setup_cert.get('trace_coverage_ok'))}:"
            f"order={bool(setup_cert.get('lifecycle_order_ok'))}:"
            f"complete={setup_cert.get('complete_lifecycle_count') or 0}:"
            f"window={bool(setup_cert.get('window_completeness_ok', True))}:"
            f"truncated={setup_cert.get('possible_truncated_window_issue_count') or 0}"
        )
    return failures


def _session_ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _finding_timestamp_range(rows: list[dict]) -> dict:
    values = sorted(str(row.get("ts")) for row in rows if row.get("ts"))
    return {
        "count_with_ts": len(values),
        "first_ts": values[0] if values else None,
        "last_ts": values[-1] if values else None,
    }


def _canonical_worker_started_at(container_name: str = CANONICAL_MOMENTUM_WORKER) -> str:
    completed = subprocess.run(
        ["docker", "inspect", container_name, "--format", "{{.State.StartedAt}}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    started_at = str(completed.stdout or "").strip()
    if not started_at or started_at.startswith("<no value>"):
        raise RuntimeError(f"missing Docker StartedAt for {container_name}")
    return started_at


def _summary_only_payload(summary: dict) -> dict:
    setup_trace = summary.get("setup_trace") if isinstance(summary.get("setup_trace"), dict) else {}
    scheduler = summary.get("scheduler") if isinstance(summary.get("scheduler"), dict) else {}
    pnl = summary.get("pnl_attribution") if isinstance(summary.get("pnl_attribution"), dict) else {}
    arm_lifecycle = summary.get("arm_lifecycle") if isinstance(summary.get("arm_lifecycle"), dict) else {}
    lifecycle = (
        setup_trace.get("lifecycle_summary")
        if isinstance(setup_trace.get("lifecycle_summary"), dict)
        else {}
    )
    finding_reasons: dict[str, int] = {}
    for reason in setup_trace.get("finding_reasons") or []:
        key = str(reason or "")
        if key:
            finding_reasons[key] = finding_reasons.get(key, 0) + 1
    finding_samples: list[dict] = []
    finding_rows = [row for row in (setup_trace.get("findings") or []) if isinstance(row, dict)]
    for row in finding_rows:
        finding_samples.append(
            {
                "session_id": row.get("session_id"),
                "ts": row.get("ts"),
                "event_type": row.get("event_type"),
                "setup_alias": row.get("setup_alias"),
                "reason": row.get("reason"),
            }
        )
        if len(finding_samples) >= 8:
            break
    return {
        "ok": bool(summary.get("ok")),
        "read_only": bool(summary.get("read_only")),
        "certification_failures": list(summary.get("certification_failures") or []),
        "certification": summary.get("certification") or {},
        "setup_trace_certification": setup_trace.get("certification") or {},
        "setup_trace_lifecycle_counts": {
            "stage_counts": lifecycle.get("stage_counts") or {},
            "event_type_counts": lifecycle.get("event_type_counts") or {},
            "trace_alias_counts": lifecycle.get("trace_alias_counts") or {},
            "wait_reason_counts": lifecycle.get("wait_reason_counts") or {},
            "sessions_with_entry_fill": lifecycle.get("sessions_with_entry_fill"),
            "sessions_with_trailing_armed": lifecycle.get("sessions_with_trailing_armed"),
            "sessions_with_add_submit": lifecycle.get("sessions_with_add_submit"),
            "sessions_with_add_fill": lifecycle.get("sessions_with_add_fill"),
            "sessions_with_exit_fill": lifecycle.get("sessions_with_exit_fill"),
            "sessions_with_entry_and_exit": lifecycle.get("sessions_with_entry_and_exit"),
        },
        "arm_lifecycle": {
            "arm_requested_count": arm_lifecycle.get("arm_requested_count") or 0,
            "arm_confirmed_count": arm_lifecycle.get("arm_confirmed_count") or 0,
            "runner_started_count": arm_lifecycle.get("runner_started_count") or 0,
            "arm_expired_count": arm_lifecycle.get("arm_expired_count") or 0,
            "requested_without_confirm_count": arm_lifecycle.get("requested_without_confirm_count") or 0,
            "confirmed_without_runner_count": arm_lifecycle.get("confirmed_without_runner_count") or 0,
            "expired_without_runner_count": arm_lifecycle.get("expired_without_runner_count") or 0,
            "samples": arm_lifecycle.get("samples") or [],
        },
        "pnl_evidence": summary.get("pnl_evidence") or {},
        "scheduler_priority_evidence": scheduler.get("priority_evidence") or {},
        "scheduler_starvation_evidence": scheduler.get("starvation_evidence") or {},
        "setup_trace_finding_reasons": dict(sorted(finding_reasons.items())),
        "setup_trace_finding_ts_range": _finding_timestamp_range(finding_rows),
        "setup_trace_finding_samples": finding_samples,
        "counts": {
            "since_utc": (summary.get("inputs") or {}).get("since_utc")
            if isinstance(summary.get("inputs"), dict)
            else None,
            "until_utc": (summary.get("inputs") or {}).get("until_utc")
            if isinstance(summary.get("inputs"), dict)
            else None,
            "session_rows": (summary.get("inputs") or {}).get("session_rows")
            if isinstance(summary.get("inputs"), dict)
            else None,
            "scheduler_snapshot_steps": (summary.get("inputs") or {}).get("scheduler_snapshot_steps")
            if isinstance(summary.get("inputs"), dict)
            else None,
            "scheduler_selected": scheduler.get("selected_count"),
            "setup_trace_events_seen": setup_trace.get("events_seen"),
            "setup_trace_traces_seen": setup_trace.get("traces_seen"),
            "setup_trace_findings": setup_trace.get("finding_count"),
            "broker_realized": len(pnl.get("realized_session_ids") or []),
            "broker_missing": len(pnl.get("selected_without_outcome_ids") or []),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None, help="UTC ISO lower bound for updated/event timestamps.")
    parser.add_argument(
        "--since-canonical-worker-start",
        action="store_true",
        help=(
            "Use the canonical momentum worker Docker StartedAt as --since, so old telemetry "
            "cannot be confused with current-runtime findings."
        ),
    )
    parser.add_argument("--until", default=None, help="UTC ISO upper bound for updated/event timestamps.")
    parser.add_argument("--limit", type=int, default=500, help="Max sessions/events to export.")
    parser.add_argument("--session-ids", default=None, help="Comma-separated session id scope.")
    parser.add_argument("--capacity-limit", type=int, default=25, help="Replay scheduler useful-capacity slots.")
    parser.add_argument("--order-call-budget", type=int, default=None, help="Optional replay order-call budget.")
    parser.add_argument("--risk-budget-slots", type=int, default=None, help="Optional replay risk budget.")
    parser.add_argument("--setup-trace-limit", type=int, default=500, help="Recent setup-trace event audit limit.")
    parser.add_argument(
        "--require-current-window-evidence",
        action="store_true",
        help=(
            "Exit nonzero when the selected audit window has no sessions or no setup traces. "
            "Use this for certification runs; it is not a live-trading enablement gate."
        ),
    )
    parser.add_argument(
        "--require-adapter-starvation-claim",
        action="store_true",
        help=(
            "Exit nonzero unless replay evidence proves adapter-unavailable/wrong-venue rows "
            "were free skipped and a valid candidate was selected in the same scheduler step."
        ),
    )
    parser.add_argument(
        "--require-scheduler-priority-claim",
        action="store_true",
        help="Exit nonzero unless Replay v3 has enough evidence for scheduler-priority claims.",
    )
    parser.add_argument(
        "--require-pnl-minmax-claim",
        action="store_true",
        help="Exit nonzero unless Replay v3 has enough evidence for PnL min/max claims.",
    )
    parser.add_argument(
        "--require-realized-vs-expected-pnl",
        action="store_true",
        help=(
            "Exit nonzero unless every replay-selected session has a broker outcome, "
            "allowing realized-vs-selected-expected PnL attribution. This is narrower than PnL min/max."
        ),
    )
    parser.add_argument(
        "--require-broker-outcome-attribution",
        action="store_true",
        help="Exit nonzero unless selected replay sessions have broker/fill outcome attribution.",
    )
    parser.add_argument(
        "--require-setup-trace-coverage",
        action="store_true",
        help="Exit nonzero unless setup trace telemetry has clean alias/stop/floor/wait coverage.",
    )
    parser.add_argument(
        "--require-setup-lifecycle-claim",
        action="store_true",
        help="Exit nonzero unless setup trace evidence has a complete ordered lifecycle claim.",
    )
    parser.add_argument(
        "--require-live-trade-certification",
        action="store_true",
        help=(
            "Preset: require setup trace coverage, ordered setup lifecycle, and broker outcome "
            "attribution for live trade-path certification. Does not imply scheduler PnL min/max readiness."
        ),
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only compact certification/failure/count fields instead of full replay JSON.",
    )
    args = parser.parse_args(argv)
    if args.since and args.since_canonical_worker_start:
        parser.error("--since and --since-canonical-worker-start are mutually exclusive")
    since = _canonical_worker_started_at() if args.since_canonical_worker_start else args.since

    db = SessionLocal()
    try:
        summary = run_live_replay_audit(
            db,
            since=since,
            until=args.until,
            limit=args.limit,
            session_ids=_session_ids(args.session_ids),
            capacity_limit=args.capacity_limit,
            order_call_budget=args.order_call_budget,
            risk_budget_slots=args.risk_budget_slots,
            setup_trace_limit=args.setup_trace_limit,
        )
        certification_failures = _certification_failures(
            summary,
            require_current_window_evidence=args.require_current_window_evidence,
            require_adapter_starvation_claim=args.require_adapter_starvation_claim,
            require_scheduler_priority_claim=args.require_scheduler_priority_claim,
            require_pnl_minmax_claim=args.require_pnl_minmax_claim,
            require_realized_vs_expected_pnl=args.require_realized_vs_expected_pnl,
            require_broker_outcome_attribution=(
                args.require_broker_outcome_attribution or args.require_live_trade_certification
            ),
            require_setup_trace_coverage=(
                args.require_setup_trace_coverage or args.require_live_trade_certification
            ),
            require_setup_lifecycle_claim=(
                args.require_setup_lifecycle_claim or args.require_live_trade_certification
            ),
        )
        if certification_failures:
            summary = dict(summary)
            summary["ok"] = False
            summary["certification_failures"] = certification_failures
        payload = _summary_only_payload(summary) if args.summary_only else summary
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if summary.get("ok") else 2
    finally:
        try:
            db.rollback()
        finally:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
