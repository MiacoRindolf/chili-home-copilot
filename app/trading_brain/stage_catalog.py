"""Canonical ordered stage keys for future `brain_stage_job` rows (aligned to `run_learning_cycle` phases).

`TOTAL_STAGES` is the single source for `LearningStatusDTO.total_steps` when DB-backed status lands.
"""

from __future__ import annotations

# 25 keys: through breakout (11) + secondary miners block (8) + tail (6) — see learning.py step sequence.
STAGE_KEYS: tuple[str, ...] = (
    "pre_filter",
    "scan",
    "snapshots",
    "backfill",
    "confidence_decay",
    "mine",
    "active_seek",
    "legacy_insight_and_queue_backtests",
    "pattern_variant_evolution",
    "evolve",
    "breakout_outcomes",
    "intraday_mining",
    "refine_patterns",
    "exit_optimization",
    "fakeout_mining",
    "position_sizing",
    "inter_alert",
    "timeframe_learning",
    "synergy_mining",
    "market_journal",
    "signal_events",
    "ml_train",
    "proposals",
    "pattern_engine",
    "cycle_ai_report",
)

TOTAL_STAGES: int = len(STAGE_KEYS)
