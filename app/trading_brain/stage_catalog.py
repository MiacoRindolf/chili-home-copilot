"""Canonical ordered stage keys derived from ``learning_cycle_architecture`` (single source of truth).

``STAGE_KEYS`` lists only step SIDs that bump ``steps_completed`` on the **normal**
runtime path (``snap_inline=False``): no scheduler-only cluster, no inline-only
snapshot steps, and no ``cycle_report`` / ``depromote`` / ``finalize``.

``TOTAL_STAGES`` is ``len(STAGE_KEYS)`` and matches ``count_cycle_progress_steps(snap_inline=False)``.
"""

from __future__ import annotations

from ..services.trading.learning_cycle_architecture import cycle_progress_stage_keys

STAGE_KEYS: tuple[str, ...] = cycle_progress_stage_keys(snap_inline=False)
TOTAL_STAGES: int = len(STAGE_KEYS)
