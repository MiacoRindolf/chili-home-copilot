"""Paper automation runner FSM (Phase 7) — live-intent states are separate."""

from __future__ import annotations

# Pre-runner / operator (Phases 4–6)
STATE_DRAFT = "draft"
STATE_QUEUED = "queued"
STATE_LIVE_ARM_PENDING = "live_arm_pending"
STATE_ARMED_PENDING_RUNNER = "armed_pending_runner"
STATE_IDLE = "idle"

# Paper runner (Phase 7)
STATE_WATCHING = "watching"
STATE_ENTRY_CANDIDATE = "entry_candidate"
STATE_PENDING_ENTRY = "pending_entry"
STATE_ENTERED = "entered"
STATE_SCALING_OUT = "scaling_out"
STATE_TRAILING = "trailing"
STATE_BAILOUT = "bailout"
STATE_EXITED = "exited"
STATE_COOLDOWN = "cooldown"
STATE_FINISHED = "finished"

# Terminal / housekeeping
STATE_CANCELLED = "cancelled"
STATE_ARCHIVED = "archived"
STATE_EXPIRED = "expired"
STATE_ERROR = "error"

LIVE_INTENT_STATES = frozenset({STATE_LIVE_ARM_PENDING, STATE_ARMED_PENDING_RUNNER})

PAPER_RUNNER_RUNNABLE_STATES = frozenset(
    {
        STATE_QUEUED,
        STATE_WATCHING,
        STATE_ENTRY_CANDIDATE,
        STATE_PENDING_ENTRY,
        STATE_ENTERED,
        STATE_SCALING_OUT,
        STATE_TRAILING,
        STATE_BAILOUT,
        STATE_EXITED,
        STATE_COOLDOWN,
    }
)

PAPER_RUNNER_TERMINAL_STATES = frozenset(
    {STATE_FINISHED, STATE_CANCELLED, STATE_ARCHIVED, STATE_EXPIRED, STATE_ERROR}
)

# Count toward Phase 6 concurrency while session is active (pre-runner + paper runner until finished).
PAPER_CONCURRENT_STATES = frozenset(
    {
        STATE_DRAFT,
        STATE_QUEUED,
        STATE_WATCHING,
        STATE_ENTRY_CANDIDATE,
        STATE_PENDING_ENTRY,
        STATE_ENTERED,
        STATE_SCALING_OUT,
        STATE_TRAILING,
        STATE_BAILOUT,
        STATE_EXITED,
        STATE_COOLDOWN,
        STATE_IDLE,
    }
)

_ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (STATE_QUEUED, STATE_WATCHING),
        (STATE_WATCHING, STATE_ENTRY_CANDIDATE),
        (STATE_WATCHING, STATE_WATCHING),  # no-op refresh
        (STATE_ENTRY_CANDIDATE, STATE_PENDING_ENTRY),
        (STATE_ENTRY_CANDIDATE, STATE_WATCHING),
        (STATE_PENDING_ENTRY, STATE_ENTERED),
        (STATE_PENDING_ENTRY, STATE_WATCHING),
        (STATE_ENTERED, STATE_SCALING_OUT),
        (STATE_ENTERED, STATE_TRAILING),
        (STATE_ENTERED, STATE_EXITED),
        (STATE_ENTERED, STATE_BAILOUT),
        (STATE_SCALING_OUT, STATE_EXITED),
        (STATE_SCALING_OUT, STATE_TRAILING),
        (STATE_TRAILING, STATE_EXITED),
        (STATE_TRAILING, STATE_BAILOUT),
        (STATE_BAILOUT, STATE_EXITED),
        (STATE_EXITED, STATE_COOLDOWN),
        (STATE_COOLDOWN, STATE_FINISHED),
        # Risk / operator
        (STATE_WATCHING, STATE_ERROR),
        (STATE_ENTRY_CANDIDATE, STATE_ERROR),
        (STATE_PENDING_ENTRY, STATE_ERROR),
        (STATE_ENTERED, STATE_ERROR),
        (STATE_SCALING_OUT, STATE_ERROR),
        (STATE_TRAILING, STATE_ERROR),
        (STATE_BAILOUT, STATE_ERROR),
        (STATE_QUEUED, STATE_ERROR),
    }
)


def is_paper_runner_state(state: str) -> bool:
    return state in PAPER_RUNNER_RUNNABLE_STATES or state in PAPER_RUNNER_TERMINAL_STATES


def is_live_intent_state(state: str) -> bool:
    return state in LIVE_INTENT_STATES


def can_transition(from_state: str, to_state: str) -> bool:
    if from_state == to_state:
        return True
    return (from_state, to_state) in _ALLOWED_TRANSITIONS


def assert_transition(from_state: str, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise ValueError(f"Invalid paper FSM transition {from_state!r} -> {to_state!r}")
