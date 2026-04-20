"""Centralized constants for the coding-task pipeline (A5).

Before this module, readiness-state literals, HTTP-like timeouts, and
truncation byte limits were duplicated across ``po_v2.py``, ``service.py``,
``snapshot_apply.py``, ``agent_suggest.py``, and ``validator_runner.py``.
Everything in here is the single source of truth. Modules keep their own
re-imports (with deprecation comments) until their next touch.
"""
from __future__ import annotations

from typing import Literal

# ── Readiness states (see po_v2.READINESS_STATES) ────────────────────────
ReadinessState = Literal[
    "not_started",
    "needs_clarification",
    "brief_ready",
    "validation_pending",
    "blocked",
    "ready_for_future_impl",
]

READINESS_STATES: frozenset[str] = frozenset(
    {
        "not_started",
        "needs_clarification",
        "brief_ready",
        "validation_pending",
        "blocked",
        "ready_for_future_impl",
    }
)

# Terminal states that win over derivation (i.e. preview_readiness echoes
# them unless a new open clarification forces reopen).
TERMINAL_READINESS_STATES: frozenset[str] = frozenset(
    {"blocked", "ready_for_future_impl"}
)

# ── Workflow modes (see po_v2.WORKFLOW_MODES) ────────────────────────────
WorkflowMode = Literal["tracked", "planned", "assisted", "executing"]
WORKFLOW_MODES: frozenset[str] = frozenset(
    {"tracked", "planned", "assisted", "executing"}
)

# ── Snapshot apply timeouts ──────────────────────────────────────────────
APPLY_TIMEOUT_SEC: int = 120
AUDIT_MESSAGE_MAX_BYTES: int = 4000

# ── Agent-suggest prompt envelope bounds ─────────────────────────────────
BRIDGE_TITLE_MAX_BYTES: int = 1500
BRIDGE_BRIEF_MAX_BYTES: int = 12_000
BRIDGE_BLOCKER_MAX: int = 3
BRIDGE_EXTRA_MAX_BYTES: int = 4_000

# ── Execution-loop caps ──────────────────────────────────────────────────
EXECUTION_LOOP_MAX_ITERATIONS: int = 5
EXECUTION_LOOP_MAX_DURATION_SECONDS: int = 30 * 60

# ── Handoff / summary truncation bounds ──────────────────────────────────
HANDOFF_BRIEF_MAX_BYTES: int = 32_000
HANDOFF_ERR_MAX_BYTES: int = 4_000
HANDOFF_BLOCKER_SUMMARY_MAX_BYTES: int = 2_000
HANDOFF_ARTIFACT_PREVIEW_MAX_BYTES: int = 8_000
HANDOFF_BLOCKERS_LIMIT: int = 20
HANDOFF_ARTIFACTS_LIMIT: int = 5
HANDOFF_CLARIFICATIONS_LIMIT: int = 50
HANDOFF_CLAR_QUESTION_MAX_BYTES: int = 12_000
HANDOFF_CLAR_ANSWER_MAX_BYTES: int = 24_000

# ── Validation runs listing ──────────────────────────────────────────────
VALIDATION_RUNS_LIST_DEFAULT_LIMIT: int = 15
VALIDATION_RUNS_LIST_MAX_LIMIT: int = 50
