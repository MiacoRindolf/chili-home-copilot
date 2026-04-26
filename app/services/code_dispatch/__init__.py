"""CHILI Dispatch — autonomous coding loop.

Architecture: see docs/CHILI_DISPATCH_AUTONOMOUS_DEV_PLAN.md
Operations:   see docs/CHILI_DISPATCH_RUNBOOK.md

This package is intentionally NOT imported by app.main on startup. It is
instantiated by scripts/scheduler_worker.py only when CHILI_DISPATCH_ENABLED=1.
"""
from __future__ import annotations

__all__ = [
    "run_code_learning_cycle",
    "is_code_agent_enabled",
    "activate_code_kill_switch",
    "deactivate_code_kill_switch",
]

from .cycle import run_code_learning_cycle
from .governance import (
    activate_code_kill_switch,
    deactivate_code_kill_switch,
    is_code_agent_enabled,
)
