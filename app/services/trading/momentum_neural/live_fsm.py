"""Live automation runner FSM (Phase 8) — disjoint from paper runner states."""

from __future__ import annotations

# Pre-run (shared string with operator / paper_fsm)
STATE_ARMED_PENDING_RUNNER = "armed_pending_runner"

# Live runner
STATE_QUEUED_LIVE = "queued_live"
STATE_WATCHING_LIVE = "watching_live"
STATE_LIVE_ENTRY_CANDIDATE = "live_entry_candidate"
STATE_LIVE_PENDING_ENTRY = "live_pending_entry"
STATE_LIVE_ENTERED = "live_entered"
STATE_LIVE_SCALING_OUT = "live_scaling_out"
STATE_LIVE_TRAILING = "live_trailing"
STATE_LIVE_BAILOUT = "live_bailout"
STATE_LIVE_EXITED = "live_exited"
STATE_LIVE_COOLDOWN = "live_cooldown"
STATE_LIVE_FINISHED = "live_finished"
STATE_LIVE_CANCELLED = "live_cancelled"
STATE_LIVE_ERROR = "live_error"

LIVE_RUNNER_RUNNABLE_STATES = frozenset(
    {
        STATE_ARMED_PENDING_RUNNER,
        STATE_QUEUED_LIVE,
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
        STATE_LIVE_EXITED,
        STATE_LIVE_COOLDOWN,
    }
)

LIVE_RUNNER_TERMINAL_STATES = frozenset(
    {STATE_LIVE_FINISHED, STATE_LIVE_CANCELLED, STATE_LIVE_ERROR}
)

# Concurrency / risk counting (active live automation until terminal).
LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY = frozenset(
    {
        STATE_ARMED_PENDING_RUNNER,
        STATE_QUEUED_LIVE,
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
        STATE_LIVE_EXITED,
        STATE_LIVE_COOLDOWN,
    }
)

# In-flight live runner (for Automation summary “active” count).
LIVE_RUNNER_ACTIVE_SUMMARY_STATES = frozenset(
    {
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
    }
)

LIVE_CANCELLABLE_STATES = frozenset(
    {
        STATE_ARMED_PENDING_RUNNER,
        STATE_QUEUED_LIVE,
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
        STATE_LIVE_EXITED,
        STATE_LIVE_COOLDOWN,
    }
)

_ALLOWED_LIVE: frozenset[tuple[str, str]] = frozenset(
    {
        (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE),
        (STATE_QUEUED_LIVE, STATE_WATCHING_LIVE),
        (STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE),
        (STATE_WATCHING_LIVE, STATE_WATCHING_LIVE),
        (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_PENDING_ENTRY),
        (STATE_LIVE_ENTRY_CANDIDATE, STATE_WATCHING_LIVE),
        (STATE_LIVE_PENDING_ENTRY, STATE_LIVE_ENTERED),
        (STATE_LIVE_PENDING_ENTRY, STATE_WATCHING_LIVE),
        (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT),
        (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING),
        (STATE_LIVE_ENTERED, STATE_LIVE_EXITED),
        (STATE_LIVE_ENTERED, STATE_LIVE_BAILOUT),
        (STATE_LIVE_SCALING_OUT, STATE_LIVE_EXITED),
        (STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING),
        (STATE_LIVE_TRAILING, STATE_LIVE_EXITED),
        (STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT),
        (STATE_LIVE_BAILOUT, STATE_LIVE_EXITED),
        (STATE_LIVE_EXITED, STATE_LIVE_COOLDOWN),
        (STATE_LIVE_COOLDOWN, STATE_LIVE_FINISHED),
        (STATE_ARMED_PENDING_RUNNER, STATE_LIVE_ERROR),
        (STATE_QUEUED_LIVE, STATE_LIVE_ERROR),
        (STATE_WATCHING_LIVE, STATE_LIVE_ERROR),
        (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_ERROR),
        (STATE_LIVE_PENDING_ENTRY, STATE_LIVE_ERROR),
        (STATE_LIVE_ENTERED, STATE_LIVE_ERROR),
        (STATE_LIVE_SCALING_OUT, STATE_LIVE_ERROR),
        (STATE_LIVE_TRAILING, STATE_LIVE_ERROR),
        (STATE_LIVE_BAILOUT, STATE_LIVE_ERROR),
    }
)


def is_live_runner_state(state: str) -> bool:
    return state in LIVE_RUNNER_RUNNABLE_STATES or state in LIVE_RUNNER_TERMINAL_STATES


def can_transition_live(from_state: str, to_state: str) -> bool:
    if from_state == to_state:
        return True
    return (from_state, to_state) in _ALLOWED_LIVE


def assert_transition_live(from_state: str, to_state: str) -> None:
    if not can_transition_live(from_state, to_state):
        raise ValueError(f"Invalid live FSM transition {from_state!r} -> {to_state!r}")
