"""Structured one-line ops log for the Phase M.2-autopilot
auto-advance engine.

Single prefix ``[pattern_regime_autopilot_ops]``. Every decision the
autopilot makes (advance, hold, revert, weekly summary, gate failure,
master-kill disable) emits exactly one line so the release blocker
can grep per-event without structured parsing.

Events (closed enum, additive-only):

* ``autopilot_advance`` - slice moved one stage forward.
* ``autopilot_hold`` - slice held in current stage (one or more gates
  not yet satisfied).
* ``autopilot_revert`` - slice moved one stage back after an anomaly.
* ``autopilot_weekly_summary`` - Monday 09:00 roll-up (one line for
  all slices).
* ``autopilot_gate_fail`` - specific gate failed; recorded for
  diagnostics but ``autopilot_hold`` is still emitted for the whole
  evaluation tick.
* ``autopilot_killswitch_disabled`` - master kill flipped on; engine
  halted this cycle.
* ``autopilot_skipped`` - master kill / enabled=false short circuit.

The ``mode`` key on each line is ALWAYS the autopilot's own runtime
mode (``enabled`` / ``kill`` / ``disabled``), NOT the slice mode being
manipulated (that is conveyed as ``from_mode``/``to_mode``). This
avoids collision with the existing M.2 ops grep patterns.
"""
from __future__ import annotations

from typing import Any

CHILI_PATTERN_REGIME_AUTOPILOT_OPS_PREFIX = "[pattern_regime_autopilot_ops]"


def _format_val(k: str, v: Any, parts: list[str]) -> None:
    if v is None:
        return
    if isinstance(v, bool):
        parts.append(f"{k}={str(v).lower()}")
    elif isinstance(v, str):
        if any(c.isspace() for c in v) or v == "":
            parts.append(f'{k}="{v}"')
        else:
            parts.append(f"{k}={v}")
    elif isinstance(v, float):
        parts.append(f"{k}={v:.6g}")
    else:
        parts.append(f"{k}={v}")


def format_autopilot_ops_line(
    *,
    event: str,
    mode: str,
    slice_name: str | None = None,
    from_mode: str | None = None,
    to_mode: str | None = None,
    reason_code: str | None = None,
    days_in_stage: int | None = None,
    total_decisions: int | None = None,
    approval_id: int | None = None,
    approval_live: bool | None = None,
    order_lock_blocked: bool | None = None,
    gate_name: str | None = None,
    gate_ok: bool | None = None,
    as_of_date: str | None = None,
    evaluation_tick: str | None = None,
    mean_multiplier: float | None = None,
    block_ratio: float | None = None,
    mean_fires_per_day: float | None = None,
    **extra: Any,
) -> str:
    """Render one structured ops-log line for the autopilot engine.

    Key order is stable so regex-based release blockers stay simple.
    """
    parts: list[str] = [
        CHILI_PATTERN_REGIME_AUTOPILOT_OPS_PREFIX,
        f"event={event}",
        f"mode={mode}",
    ]
    _format_val("slice", slice_name, parts)
    _format_val("from_mode", from_mode, parts)
    _format_val("to_mode", to_mode, parts)
    _format_val("reason_code", reason_code, parts)
    _format_val("days_in_stage", days_in_stage, parts)
    _format_val("total_decisions", total_decisions, parts)
    _format_val("approval_id", approval_id, parts)
    _format_val("approval_live", approval_live, parts)
    _format_val("order_lock_blocked", order_lock_blocked, parts)
    _format_val("gate_name", gate_name, parts)
    _format_val("gate_ok", gate_ok, parts)
    _format_val("mean_multiplier", mean_multiplier, parts)
    _format_val("block_ratio", block_ratio, parts)
    _format_val("mean_fires_per_day", mean_fires_per_day, parts)
    _format_val("as_of_date", as_of_date, parts)
    _format_val("evaluation_tick", evaluation_tick, parts)
    for k, v in extra.items():
        _format_val(k, v, parts)
    return " ".join(parts)


__all__ = [
    "CHILI_PATTERN_REGIME_AUTOPILOT_OPS_PREFIX",
    "format_autopilot_ops_line",
]
