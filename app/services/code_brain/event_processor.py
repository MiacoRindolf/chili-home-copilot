"""Event processor — single-event pump for the Code Brain.

This is the "actor" in the reactive architecture:

  trigger_watcher (cheap DB poll) → enqueue events
        ↓
  event_processor (this module)   → claim 1 event
        ↓
  decision_router.route()         → pick path (template / local / premium / escalate / skip)
        ↓
  appropriate executor            → applies the decision
        ↓
  decision_router.record_outcome() + event_bus.mark_processed()

The processor is deliberately small. It doesn't make routing decisions
itself — it just glues the queue to the router and back to the
appropriate execution path.

For the initial reactive build:

  Decision           Action
  ─────────          ──────
  TEMPLATE           Phase 2 — apply parameterized template
                     (right now: log + escalate so we don't silently no-op)
  LOCAL_MODEL        Phase 3 — call Ollama via llm_router (stub today)
  PREMIUM            Build TaskContext, call existing dispatch sandboxed
                     run via code_dispatch.cycle._run_sandboxed (the path
                     that already produces real diffs and pays OpenAI)
  ESCALATE           Mark event escalated, write notify_user audit row
                     so the Brain UI shows it
  SKIP               Mark event skipped with the rule reason
"""
from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import decision_router
from . import event_bus
from . import runtime_state

logger = logging.getLogger(__name__)


_WORKER_ID = f"event-processor@{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# TaskContext builder
# ---------------------------------------------------------------------------

def _build_task_context(
    db: Session, task_id: int
) -> Optional[decision_router.TaskContext]:
    # ── Schema notes ────────────────────────────────────────────────────
    # plan_tasks: id, title, description (NOT 'brief'), coding_readiness_state
    # plan_task_coding_profile: task_id, code_repo_id, sub_path
    #   (NO intended_files column on this table)
    # coding_task_brief: task_id, body, ... — richer brief content lives here
    #   when set; we LEFT JOIN to pick up the latest one if any.
    # code_repos: id, name
    row = db.execute(
        text(
            "SELECT t.id, "
            "       COALESCE(t.title, '') AS title, "
            "       COALESCE(t.description, '') AS description, "
            "       COALESCE(b.body, '') AS brief_body, "
            "       COALESCE(p.sub_path, '') AS sub_path, "
            "       p.code_repo_id, "
            "       COALESCE(r.name, '') AS repo_name "
            "FROM plan_tasks t "
            "LEFT JOIN plan_task_coding_profile p ON p.task_id = t.id "
            "LEFT JOIN code_repos r ON r.id = p.code_repo_id "
            "LEFT JOIN LATERAL ( "
            "    SELECT body FROM coding_task_brief "
            "    WHERE task_id = t.id "
            "    ORDER BY id DESC LIMIT 1 "
            ") b ON true "
            "WHERE t.id = :id"
        ),
        {"id": int(task_id)},
    ).fetchone()
    if not row:
        return None

    # Combine description + brief body so the router has all the signal it
    # needs for novelty scoring and pattern matching. Brief usually wins
    # when both exist (it's the richer artifact), so we prepend it.
    brief_body_combined = (
        (row[3] or "").strip() + ("\n\n" + (row[2] or "").strip()
                                   if (row[2] or "").strip() else "")
    ).strip()

    # plan_task_coding_profile has no 'intended_files' column on this branch,
    # so we leave it empty. Pattern matching falls back to sub_path.
    intended_files: list[str] = []

    fail_row = db.execute(
        text(
            "SELECT COUNT(*) FROM code_agent_runs "
            "WHERE task_id = :t AND decision IN ('failed','validation_failed')"
        ),
        {"t": int(task_id)},
    ).fetchone()
    prior_failures = int(fail_row[0]) if fail_row else 0

    high_stakes = False
    if intended_files:
        block_rows = db.execute(
            text("SELECT glob FROM frozen_scope_paths WHERE severity IN ('block','review_required')")
        ).fetchall()
        block_globs = [str(g[0]) for g in block_rows or []]
        if block_globs:
            from .decision_router import _glob_to_regex
            for glob in block_globs:
                rx = _glob_to_regex(glob)
                if any(rx.match(f) for f in intended_files):
                    high_stakes = True
                    break

    return decision_router.TaskContext(
        task_id=int(row[0]),
        title=str(row[1]),
        brief_body=brief_body_combined,
        sub_path=str(row[4]),
        repo_id=(int(row[5]) if row[5] is not None else None),
        repo_name=str(row[6]),
        intended_files=intended_files,
        estimated_diff_loc=0,
        prior_failure_count=prior_failures,
        is_high_stakes=high_stakes,
    )


# ---------------------------------------------------------------------------
# Dispatchers per Decision
# ---------------------------------------------------------------------------

def _dispatch_template(
    db: Session,
    ctx: decision_router.TaskContext,
    rd: decision_router.RoutingDecision,
) -> tuple[str, str]:
    """Phase 2 will apply the template diff. For now, escalate so we
    don't silently no-op tasks the brain *thought* it could handle.
    """
    msg = (
        f"Pattern matched ({rd.matched_pattern_name}@{rd.pattern_confidence}) "
        "but template apply not yet implemented (Phase 2). Escalating."
    )
    logger.info("[code_brain.event_processor] template-stub: %s", msg)
    return "escalated", msg


def _dispatch_local_model(
    db: Session,
    ctx: decision_router.TaskContext,
    rd: decision_router.RoutingDecision,
) -> tuple[str, str]:
    """Phase 3 — distilled local model. Today the local model has not
    yet been promoted (see runtime_state.local_model_promoted). If we
    ever land here without promotion the router has a bug; defensive
    escalate keeps us from accidentally calling Ollama before it's ready.
    """
    state = runtime_state.get_state(db)
    if not state.local_model_promoted:
        return (
            "escalated",
            "local_model decision but local_model_promoted=false (router bug?)",
        )
    return (
        "escalated",
        f"local_model={state.local_model_tag} call not yet implemented (Phase 3)",
    )


def _dispatch_premium(
    db: Session,
    ctx: decision_router.TaskContext,
    rd: decision_router.RoutingDecision,
) -> tuple[str, str]:
    """Hand off to the existing sandboxed dispatch cycle.

    The sandboxed runner already does the expensive work (worktree,
    multi-file LLM plan→edit, validation). Reusing it means we don't
    duplicate the apply / 3-tier-fallback logic. The cost is recorded
    via ``decision_router.record_outcome`` once the run completes.
    """
    try:
        # Lazy import — keeps this module free of code_dispatch when
        # only the watcher path is exercised in tests.
        from ..code_dispatch.cycle import run_code_learning_cycle  # type: ignore
    except Exception as e:
        return "failed", f"cannot import code_dispatch.cycle: {e}"

    try:
        result = run_code_learning_cycle()
    except Exception as e:
        logger.exception("[code_brain.event_processor] premium cycle raised")
        return "failed", f"premium cycle raised: {e!r}"[:1500]

    status = (result or {}).get("status", "unknown") if isinstance(result, dict) else "unknown"
    if status in {"sandboxed_ok", "applied", "merged"}:
        return "applied", str(status)
    if status in {"sandboxed_fail", "failed", "validation_failed"}:
        return "failed", str(status)
    return "escalated", str(status)


def _dispatch_escalate(
    db: Session,
    ctx: decision_router.TaskContext,
    rd: decision_router.RoutingDecision,
) -> tuple[str, str]:
    """Mark the run for operator review.

    Phase F enhancement: when the escalation reason matches a "permanent"
    rule (e.g. ``prior_failure_count>=3`` strikeout, frozen scope, kill
    switch), we ALSO flip the task's ``coding_readiness_state`` away from
    ``ready_for_dispatch`` so the trigger watcher stops re-enqueueing it
    every 30s. This prevents audit noise (the brain was correctly refusing
    each cycle but writing a code_agent_runs row every refusal).

    We only write ONE audit row per task per "permanent" reason so the
    History tab stays readable. The dedupe is best-effort: we check for
    a recent identical row (same task + same router_escalate step) in
    the last hour and skip the insert if found.
    """
    reason = rd.reason or ""
    is_permanent_block = (
        "prior_failure_count" in reason
        or "frozen_scope" in reason
        or "kill_switch" in reason
        or "strikeout" in reason
    )

    try:
        # Dedupe: don't write a fresh router_escalate row if we wrote one
        # for this task in the last hour with a similar reason. Cuts the
        # noise by ~95% while preserving "first time we saw this" trail.
        recent = db.execute(
            text(
                "SELECT id FROM code_agent_runs "
                "WHERE task_id = :t AND cycle_step = 'router_escalate' "
                "  AND started_at > NOW() - INTERVAL '1 hour' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"t": ctx.task_id},
        ).fetchone()
        if recent is None:
            db.execute(
                text(
                    "INSERT INTO code_agent_runs "
                    "(task_id, repo_id, cycle_step, decision, escalation_reason, notify_user) "
                    "VALUES (:t, :r, 'router_escalate', 'escalated', :why, true)"
                ),
                {
                    "t": ctx.task_id,
                    "r": ctx.repo_id,
                    "why": reason[:1900],
                },
            )
            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[code_brain.event_processor] failed to write escalate audit row: %s", e
        )

    # The big behavior fix: stop the watcher from firing again on
    # permanently-blocked tasks. Set readiness to needs_clarification —
    # operator can flip it back to ready_for_dispatch after fixing the
    # underlying issue (clarifying the brief, resetting failure count, etc).
    if is_permanent_block:
        try:
            db.execute(
                text(
                    "UPDATE plan_tasks "
                    "SET coding_readiness_state = 'needs_clarification', "
                    "    updated_at = NOW() "
                    "WHERE id = :t "
                    "  AND coding_readiness_state = 'ready_for_dispatch'"
                ),
                {"t": ctx.task_id},
            )
            db.commit()
            logger.info(
                "[code_brain.event_processor] task %d → needs_clarification "
                "(permanent escalate: %s)",
                ctx.task_id, reason[:120],
            )
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(
                "[code_brain.event_processor] failed to flip task readiness: %s", e
            )

    return "escalated", reason


def _dispatch_skip(
    db: Session,
    ctx: decision_router.TaskContext,
    rd: decision_router.RoutingDecision,
) -> tuple[str, str]:
    return "skipped", rd.reason


_DISPATCH = {
    decision_router.Decision.TEMPLATE: _dispatch_template,
    decision_router.Decision.LOCAL_MODEL: _dispatch_local_model,
    decision_router.Decision.PREMIUM: _dispatch_premium,
    decision_router.Decision.ESCALATE: _dispatch_escalate,
    decision_router.Decision.SKIP: _dispatch_skip,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def process_one_event(db: Session) -> Optional[dict[str, Any]]:
    """Claim, route, dispatch, and finalize a single event.

    Returns ``None`` if the queue was empty. Otherwise returns a small
    report dict suitable for the status endpoint or test assertions.
    """
    state = runtime_state.get_state(db)
    if state.mode == "paused":
        return None

    ev = event_bus.claim_next(db, worker_id=_WORKER_ID)
    if ev is None:
        return None

    if ev.subject_kind != "plan_task" or ev.subject_id is None:
        # Phase 1: only plan_task subjects are handled. Other event types
        # (source_changed, ci_failed, debt_aged) get logged + skipped
        # until their handlers exist.
        event_bus.mark_processed(
            db, ev.id,
            outcome="skipped",
            error_message=f"unsupported subject {ev.subject_kind}/{ev.subject_id}",
        )
        return {
            "event_id": ev.id,
            "decision": "skip",
            "reason": "unsupported subject kind",
        }

    ctx = _build_task_context(db, ev.subject_id)
    if ctx is None:
        event_bus.mark_processed(
            db, ev.id,
            outcome="skipped",
            error_message=f"task {ev.subject_id} not found",
        )
        return {
            "event_id": ev.id,
            "decision": "skip",
            "reason": f"task {ev.subject_id} not found",
        }

    t0 = time.monotonic()
    rd = decision_router.route(db, ctx, event_id=ev.id)

    handler = _DISPATCH.get(rd.decision)
    if handler is None:
        outcome, message = "failed", f"unknown decision {rd.decision!r}"
    else:
        outcome, message = handler(db, ctx, rd)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    decision_router.record_outcome(
        db,
        log_id=rd.log_id,
        outcome=outcome,
        cost_usd=0.0,  # premium handler will refine cost via llm_call_log later
        llm_tokens_used=0,
    )

    event_outcome = {
        "applied": "success",
        "merged": "success",
        "failed": "failure",
        "escalated": "escalated",
        "skipped": "skipped",
    }.get(outcome, "failure")

    event_bus.mark_processed(
        db, ev.id,
        outcome=event_outcome,
        error_message=(message if event_outcome != "success" else None),
    )

    report = {
        "event_id": ev.id,
        "task_id": ctx.task_id,
        "decision": rd.decision.value,
        "outcome": outcome,
        "reason": rd.reason,
        "message": message,
        "elapsed_ms": elapsed_ms,
        "log_id": rd.log_id,
    }
    logger.info("[code_brain.event_processor] %s", report)
    return report
