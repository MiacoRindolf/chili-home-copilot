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

# ── decouple_watching taxonomy (concurrency) ────────────────────────────────
# Positions are BORN at FILL (live_pending_entry → live_entered). Only these four
# hold capital + a live stop → the simultaneous-open-RISK budget cap charges THESE
# (byte-identical to the set aggregate_open_risk_usd trusts).
LIVE_POSITION_HOLDING_STATES = frozenset(
    {
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
    }
)
# Zero capital, zero stop, $0 at risk → governed by the watch-FANOUT cap, NOT the
# risk cap. live_pending_entry sits here: a resting gfd order encumbers nothing
# material (cancelled/re-watched on ack-timeout) and contributes $0 to open risk.
LIVE_WATCHING_PREFILL_STATES = frozenset(
    {
        STATE_ARMED_PENDING_RUNNER,
        STATE_QUEUED_LIVE,
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
        STATE_LIVE_PENDING_ENTRY,
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
        # Ross first-target scale-out can fire AFTER the runner has begun trailing
        # (price drifted up past the trail-activate level, then reached the 2:1
        # target) — take the partial, then resume trailing the runner.
        (STATE_LIVE_TRAILING, STATE_LIVE_SCALING_OUT),
        (STATE_LIVE_TRAILING, STATE_LIVE_EXITED),
        (STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT),
        (STATE_LIVE_BAILOUT, STATE_LIVE_EXITED),
        (STATE_LIVE_EXITED, STATE_LIVE_COOLDOWN),
        (STATE_LIVE_COOLDOWN, STATE_LIVE_FINISHED),
        (STATE_LIVE_COOLDOWN, STATE_WATCHING_LIVE),  # recycle: loop back for next trade
        # Completed-cycle cleanup: if restart/manual mutation leaves a previously
        # traded Ross equity session back in a pre-entry state with no active
        # order/position, terminalize it as finished instead of letting it re-enter.
        (STATE_WATCHING_LIVE, STATE_LIVE_FINISHED),
        (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_FINISHED),
        (STATE_LIVE_PENDING_ENTRY, STATE_LIVE_FINISHED),
        (STATE_ARMED_PENDING_RUNNER, STATE_LIVE_ERROR),
        (STATE_QUEUED_LIVE, STATE_LIVE_ERROR),
        (STATE_WATCHING_LIVE, STATE_LIVE_ERROR),
        (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_ERROR),
        (STATE_LIVE_PENDING_ENTRY, STATE_LIVE_ERROR),
        (STATE_LIVE_ENTERED, STATE_LIVE_ERROR),
        (STATE_LIVE_SCALING_OUT, STATE_LIVE_ERROR),
        (STATE_LIVE_TRAILING, STATE_LIVE_ERROR),
        (STATE_LIVE_BAILOUT, STATE_LIVE_ERROR),
        # CLEAN PRE-ENTRY DECLINE TERMINAL (2026-06-29): a deterministic policy decline at
        # the entry instant (no_bbo / not-live-eligible / spread-too-wide / product-not-
        # tradable — a KNOWN risk-eval BLOCK on a name that never held a position) terminalizes
        # CLEANLY in live_cancelled instead of the alarm-coloured live_error. live_error stays
        # reserved for genuine unexpected failures (zero-fill, place isError, missing snapshot).
        # These edges originate ONLY from pre-entry, no-position states, so a decline can never
        # short-circuit a held position's exit management. live_cancelled is ALREADY terminal
        # across every consumer (focus-set, reaper, feedback learner, busy-set, canonical
        # status), so this only changes the terminal LABEL — never whether the session trades.
        (STATE_ARMED_PENDING_RUNNER, STATE_LIVE_CANCELLED),
        (STATE_QUEUED_LIVE, STATE_LIVE_CANCELLED),
        (STATE_WATCHING_LIVE, STATE_LIVE_CANCELLED),
        (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_CANCELLED),
        # A live_pending_entry can be pre-submit: the runner first moves
        # candidate -> pending, then places the broker order on the next guarded
        # pass. If the schedule flips late before submit, no order exists to
        # reconcile, so the automation can terminalize cleanly. Call sites must
        # still keep submitted/tracked orders on the pending reconcile path.
        (STATE_LIVE_PENDING_ENTRY, STATE_LIVE_CANCELLED),
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
