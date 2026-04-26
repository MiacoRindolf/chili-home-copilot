"""The 8-step code learning cycle.

Mirrors app.services.trading.learning.run_learning_cycle() but for code.
Called from scripts/scheduler_worker.py every 60 seconds (configurable).

Modes (env CHILI_DISPATCH_MODE):
  shadow      — pick a task and report what it would do; never apply
  sandboxed   — apply in a worktree and validate; never commit
  branch      — commit to dispatch/<task_id> branch; never merge
  auto-merge  — merge clean branches to main if frozen-scope clean
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Optional

from sqlalchemy import text

from . import audit
from .frozen_scope import diff_touches_frozen_scope, is_blocked
from .governance import (
    is_code_agent_enabled,
    record_consecutive_failure,
    reset_consecutive_failures,
)
from .miner import pick_next_task, Candidate
from .notifier import escalate_to_user
from .rule_gate import RuleGateContext, passes_code_rule_gate
from . import validation_audit
from .scorer import TierChoice, choose_tier, task_complexity_score

logger = logging.getLogger(__name__)


def _reap_stuck_runs() -> int:
    """Mark runs as decision='draft_timeout' if started long ago and still unfinished.

    Safe to run every tick. Does not kill drafts younger than
    ``CHILI_DISPATCH_REAP_AGE_MIN`` (default 15) minutes.
    """
    from ...db import SessionLocal

    age_min = int(os.environ.get("CHILI_DISPATCH_REAP_AGE_MIN", "15"))
    try:
        sess = SessionLocal()
        try:
            result = sess.execute(
                text(
                    "UPDATE code_agent_runs "
                    "SET decision = 'draft_timeout', "
                    "    finished_at = NOW(), "
                    "    escalation_reason = 'reaped: stuck > ' || CAST(:age AS text) || ' min' "
                    "WHERE finished_at IS NULL "
                    "  AND started_at < NOW() - (CAST(:age AS int) * INTERVAL '1 minute') "
                    "RETURNING id"
                ),
                {"age": age_min},
            )
            rows = result.fetchall()
            ids = [int(r[0]) for r in rows]
            sess.commit()
            if ids:
                logger.warning(
                    "[code_dispatch] reaped stuck runs (>%dmin): %s",
                    age_min,
                    ids,
                )
            return len(ids)
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch] reaper failed", exc_info=True)
        return 0


def _set_code_agent_cycle_step(run_id: int, step: str) -> None:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            sess.execute(
                text("UPDATE code_agent_runs SET cycle_step = :s WHERE id = :i"),
                {"s": step, "i": run_id},
            )
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.debug("[code_dispatch] cycle_step update failed", exc_info=True)


def _dispatch_draft_suggestion(task_id: int, user_id: int) -> tuple[Optional[int], dict[str, Any], float]:
    """Run planner agent-suggest and persist a snapshot row. Returns (suggestion_id, meta, elapsed_ms)."""
    from ...config import settings
    from ...db import SessionLocal
    from ...models import PlanTask
    from ...services.coding_task.agent_suggest import run_agent_suggest_for_task
    from ...services.coding_task.agent_suggestion_store import (
        bound_payload_for_save,
        insert_suggestion,
    )

    t0 = time.monotonic()
    db = SessionLocal()
    try:
        task = db.query(PlanTask).filter(PlanTask.id == int(task_id)).first()
        if not task:
            return None, {"error": "task not found"}, (time.monotonic() - t0) * 1000.0

        timeout_s = float(os.environ.get("CHILI_DISPATCH_DRAFT_TIMEOUT_SEC", "180"))

        async def _run() -> dict[str, Any]:
            return await asyncio.wait_for(
                run_agent_suggest_for_task(
                    db,
                    task,
                    user_id,
                    None,
                ),
                timeout=timeout_s,
            )

        try:
            out = asyncio.run(_run())
        except asyncio.TimeoutError:
            return (
                None,
                {"error": f"draft_timeout after {timeout_s:g}s"},
                (time.monotonic() - t0) * 1000.0,
            )
        if out.get("error") or out.get("workspace_unbound"):
            return None, out, (time.monotonic() - t0) * 1000.0

        body = {
            "response": out.get("response", ""),
            "model": out.get("model", "unknown"),
            "diffs": out.get("diffs") or [],
            "files_changed": out.get("files_changed") or [],
            "validation": out.get("validation") or [],
            "context_used": out.get("context_used") or {},
        }
        cols, err = bound_payload_for_save(body)
        if err or not cols:
            return None, {"error": err or "bound_payload failed"}, (time.monotonic() - t0) * 1000.0

        sid = insert_suggestion(db, int(task_id), int(user_id), cols)
        db.commit()
        meta = {
            "model": out.get("model", "unknown"),
            "suggestion_id": sid,
        }
        return int(sid), meta, (time.monotonic() - t0) * 1000.0
    finally:
        db.close()


def _run_sandboxed(
    run_id: int,
    candidate: Candidate,
    tier: TierChoice,
    cx: float,
) -> dict[str, Any]:
    from pathlib import Path

    from ...config import settings
    from ...db import SessionLocal
    from ...models import PlanTask, PlanTaskCodingProfile
    from ..code_brain.runtime import resolve_repo_runtime_path
    from ..coding_task.workspaces import get_bound_workspace_repo_for_profile
    from . import runner

    user_id = int(getattr(settings, "brain_default_user_id", None) or 1)
    v_timeout = int(os.environ.get("CHILI_DISPATCH_VALIDATION_TIMEOUT_SEC", "300"))

    if candidate.task_id is None:
        if run_id:
            audit.close_run(
                run_id,
                decision="veto",
                escalation_reason="sandboxed requires task_id",
            )
        return {"status": "sandboxed_fail", "reason": "no_task_id"}

    _set_code_agent_cycle_step(run_id, "draft")
    suggestion_id, meta, ms_draft = _dispatch_draft_suggestion(int(candidate.task_id), user_id)

    if suggestion_id is None or meta.get("error"):
        err = (meta or {}).get("error", "draft_failed")
        if run_id:
            audit.close_run(
                run_id,
                decision="draft_failed",
                escalation_reason=str(err)[:2000],
                llm_snapshot={
                    "tier": tier.tier,
                    "complexity": cx,
                    "reason": tier.reason,
                    "elapsed_ms_draft": ms_draft,
                },
            )
        return {"status": "draft_failed", "reason": str(err)}

    repo_root: Optional[str] = None
    handle: Optional[runner.WorktreeHandle] = None
    val_run_id: Optional[int] = None
    val_passed = False
    timed_out = False
    diff_files: list[str] = []
    diff_loc = 0
    ms_apply = 0.0
    ms_val = 0.0

    try:
        _set_code_agent_cycle_step(run_id, "apply")
        sdb = SessionLocal()
        try:
            task = sdb.query(PlanTask).filter(PlanTask.id == int(candidate.task_id)).first()
            if not task:
                if run_id:
                    audit.close_run(
                        run_id,
                        decision="draft_failed",
                        escalation_reason="task_disappeared",
                    )
                return {"status": "sandboxed_fail", "reason": "task not found"}
            prof = (
                sdb.query(PlanTaskCodingProfile)
                .filter(PlanTaskCodingProfile.task_id == task.id)
                .first()
            )
            rrepo = get_bound_workspace_repo_for_profile(sdb, prof, user_id=user_id)
            if rrepo is None:
                if run_id:
                    audit.close_run(
                        run_id,
                        decision="validation_failed",
                        escalation_reason="no_workspace_binding",
                    )
                return {"status": "sandboxed_fail", "reason": "no workspace"}
            root = resolve_repo_runtime_path(rrepo)
            repo_root = str(root)
        finally:
            sdb.close()

        handle = runner.create_dispatch_worktree(repo_root, int(candidate.task_id))
        t_apply0 = time.monotonic()
        adb = SessionLocal()
        try:
            tsk = adb.query(PlanTask).filter(PlanTask.id == int(candidate.task_id)).first()
            if not tsk:
                if run_id:
                    audit.close_run(
                        run_id,
                        decision="validation_failed",
                        escalation_reason="task_missing_for_apply",
                    )
                return {"status": "sandboxed_fail", "reason": "task not found"}
            apply_out = runner.apply_suggestion_in_worktree(
                adb,
                int(candidate.task_id),
                user_id,
                suggestion_id,
                handle,
                task_title=str(getattr(tsk, "title", "") or ""),
                repo_root=repo_root,
            )
        finally:
            adb.close()
        ms_apply = (time.monotonic() - t_apply0) * 1000.0
        if not apply_out.get("ok"):
            if run_id:
                audit.close_run(
                    run_id,
                    decision="validation_failed",
                    escalation_reason=apply_out.get("message", "apply failed")[:2000],
                    llm_snapshot=_sandbox_llm_snapshot(
                        tier, cx, suggestion_id, meta, ms_draft, ms_apply, 0.0, meta.get("model", "")
                    ),
                    diff_summary={"files": apply_out.get("files") or [], "loc": 0},
                )
            return {"status": "sandboxed_fail", "reason": apply_out.get("message", "apply")}

        diff_files = list(apply_out.get("files") or [])
        diff_loc = int(apply_out.get("loc") or 0)
        findings = validation_audit.diff_adds_trivial_tests(
            diff_files=diff_files,
            worktree_path=handle.path,
        )
        if findings:
            if run_id:
                f0 = findings[0]
                audit.close_run(
                    run_id,
                    decision="validation_failed",
                    escalation_reason=(
                        f"trivial_tests_detected: {f0['file']}:{f0['line']} "
                        f"({f0['kind']})"
                    )[:2000],
                    diff_summary={
                        "files": diff_files,
                        "loc": diff_loc,
                        "trivial_test_findings": findings[:10],
                    },
                    llm_snapshot={
                        "tier": tier.tier,
                        "complexity": cx,
                        "elapsed_ms_draft": ms_draft,
                        "elapsed_ms_apply": ms_apply,
                    },
                )
            return {"status": "sandboxed_fail", "reason": "trivial_tests"}
        _set_code_agent_cycle_step(run_id, "validate")
        t_val0 = time.monotonic()
        vdb = SessionLocal()
        try:
            vrid, val_passed, timed_out = runner.run_validation_in_worktree(
                vdb,
                int(candidate.task_id),
                Path(handle.path),
                validation_timeout_sec=v_timeout,
            )
        finally:
            vdb.close()
        val_run_id = vrid
        ms_val = (time.monotonic() - t_val0) * 1000.0

        if timed_out and run_id:
            record_consecutive_failure(run_id)
            audit.close_run(
                run_id,
                decision="timeout",
                validation_run_id=val_run_id,
                escalation_reason=f"validation wall-clock > {v_timeout}s",
                llm_snapshot=_sandbox_llm_snapshot(
                    tier, cx, suggestion_id, meta, ms_draft, ms_apply, ms_val, meta.get("model", "")
                ),
                diff_summary={"files": diff_files, "loc": diff_loc},
            )
            return {"status": "sandboxed_fail", "reason": "timeout"}

        if val_passed:
            decision = "passed"
        else:
            decision = "validation_failed"

        if run_id:
            audit.close_run(
                run_id,
                decision=decision,
                validation_run_id=val_run_id,
                llm_snapshot=_sandbox_llm_snapshot(
                    tier, cx, suggestion_id, meta, ms_draft, ms_apply, ms_val, meta.get("model", "")
                ),
                diff_summary={
                    "files": diff_files,
                    "loc": diff_loc,
                    # Phase E.1: surface push status into the audit row.
                    "committed": bool(apply_out.get("committed")),
                    "pushed": bool(apply_out.get("pushed")),
                    "push_url": apply_out.get("push_url"),
                },
                branch_name=getattr(handle, "branch", None),
                commit_sha=apply_out.get("commit_sha"),
            )
        if val_passed:
            reset_consecutive_failures()
        return {
            "status": "sandboxed_ok" if val_passed else "sandboxed_fail",
            "validation_run_id": val_run_id,
            "suggestion_id": suggestion_id,
        }
    except Exception as exc:
        if run_id:
            audit.close_run(
                run_id,
                decision="validation_failed",
                escalation_reason=str(exc)[:2000],
                diff_summary={"files": diff_files, "loc": diff_loc},
            )
        raise
    finally:
        if handle is not None and repo_root is not None:
            try:
                runner.cleanup_worktree(handle, repo_root, keep_branch=False)
            except Exception:
                logger.exception("[code_dispatch] cleanup_worktree failed")


def _sandbox_llm_snapshot(
    tier: TierChoice,
    cx: float,
    suggestion_id: int,
    meta: dict[str, Any],
    ms_draft: float,
    ms_apply: float,
    ms_val: float,
    model_used: str,
) -> dict[str, Any]:
    return {
        "tier": tier.tier,
        "complexity": cx,
        "suggestion_id": suggestion_id,
        "model_used": model_used,
        "elapsed_ms_draft": ms_draft,
        "elapsed_ms_apply": ms_apply,
        "elapsed_ms_validate": ms_val,
    }


def run_code_learning_cycle() -> dict[str, Any]:
    """One pass. Returns a small status dict for the caller's logs."""
    if not is_code_agent_enabled():
        return {"status": "disabled"}

    _reap_stuck_runs()
    candidate = pick_next_task()
    if candidate is None:
        run_id = audit.open_run(
            task_id=None,
            repo_id=None,
            cycle_step="mine",
            rule_snapshot={"miner": "no_candidate"},
        )
        if run_id:
            audit.close_run(run_id, decision="idle")
        return {"status": "idle", "run_id": run_id}

    rule_ctx = RuleGateContext(
        task_id=candidate.task_id or -1,
        repo_id=candidate.repo_id,
        estimated_diff_loc=candidate.estimated_diff_loc,
        intended_files=candidate.intended_files,
        prior_failure_count=candidate.prior_failure_count,
    )
    gate = passes_code_rule_gate(rule_ctx)

    run_id = audit.open_run(
        task_id=candidate.task_id,
        repo_id=candidate.repo_id,
        cycle_step="gate",
        rule_snapshot={"gate": gate.reason, **gate.snapshot, **asdict(candidate)},
    )
    if not gate.proceed:
        if run_id:
            audit.close_run(run_id, decision="veto", escalation_reason=gate.reason)
        return {"status": "vetoed", "reason": gate.reason}

    pre_hits = diff_touches_frozen_scope(candidate.intended_files)
    if is_blocked(pre_hits):
        if run_id:
            audit.close_run(
                run_id,
                decision="escalate",
                escalation_reason=f"intended_files_in_blocked_scope: {pre_hits[0].glob}",
                notify_user=True,
            )
            escalate_to_user(
                run_id,
                title="Dispatch refused: blocked scope",
                body=f"Task {candidate.task_id} intended to touch {pre_hits[0].file_path} (glob {pre_hits[0].glob}). Reason: {pre_hits[0].reason}",
                severity="warning",
            )
        return {"status": "blocked_scope", "hit": pre_hits[0].glob}

    cx = task_complexity_score(candidate)
    tier = choose_tier(candidate, complexity=cx)

    mode = os.environ.get("CHILI_DISPATCH_MODE", "shadow")

    if mode == "shadow":
        if run_id:
            audit.close_run(
                run_id,
                decision="proceed",
                llm_snapshot={"shadow": True, "tier": tier.tier, "complexity": cx, "reason": tier.reason},
            )
        return {"status": "shadow", "tier": tier.tier, "complexity": cx, "task_id": candidate.task_id}

    if mode == "sandboxed" and run_id is not None:
        return _run_sandboxed(run_id, candidate, tier, cx)

    if run_id:
        audit.close_run(
            run_id,
            decision="deferred_until_phase_d3",
            llm_snapshot={"tier": tier.tier, "complexity": cx, "mode": mode},
        )
    return {"status": "deferred", "phase": f"{mode}_not_wired", "mode": mode}


def on_cycle_failure(run_id: Optional[int]) -> None:
    """Called by the upstream scheduler if the whole cycle errored out."""
    record_consecutive_failure(run_id)


def on_cycle_success() -> None:
    reset_consecutive_failures()
