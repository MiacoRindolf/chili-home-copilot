"""Canonical Trading Brain learning-cycle architecture (single source of truth).

Used by ``get_trading_brain_network_graph`` for the Network tab and by
``run_learning_cycle`` for ``current_step`` / ``phase`` strings.

When you add, remove, or reorder phases, edit **this module only** (then bump
``graph_version`` in ``brain_network_graph`` if consumers care).

Do **not** import ``learning`` from here (avoids circular imports).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CycleStepDef:
    sid: str
    label: str
    code_ref: str
    runner_phase: str


@dataclass(frozen=True)
class CycleClusterDef:
    id: str
    label: str
    phase_summary: str
    steps: tuple[CycleStepDef, ...]


TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS: tuple[CycleClusterDef, ...] = (
    CycleClusterDef(
        id="c_universe",
        label="Universe & scoring",
        phase_summary="pre-filtering → scanning",
        steps=(
            CycleStepDef(
                sid="prefilter",
                label="Pre-filtering market",
                code_ref="prescreener.get_prescreened_candidates",
                runner_phase="pre-filtering",
            ),
            CycleStepDef(
                sid="scan",
                label="Scanning market",
                code_ref="scanner.run_full_market_scan",
                runner_phase="scanning",
            ),
        ),
    ),
    CycleClusterDef(
        id="c_state",
        label="Market state & memory",
        phase_summary="snapshots → backfill → confidence decay",
        steps=(
            CycleStepDef(
                sid="snapshots",
                label="Taking market snapshots",
                code_ref="learning.take_snapshots_parallel",
                runner_phase="snapshots",
            ),
            CycleStepDef(
                sid="backfill",
                label="Backfilling future returns",
                code_ref="learning.backfill_future_returns (+ backfill_predicted_scores)",
                runner_phase="backfilling",
            ),
            CycleStepDef(
                sid="decay",
                label="Decaying stale insights",
                code_ref="learning.decay_stale_insights",
                runner_phase="confidence_decay",
            ),
        ),
    ),
    CycleClusterDef(
        id="c_discovery",
        label="Pattern discovery",
        phase_summary="mining → active seeking",
        steps=(
            CycleStepDef(
                sid="mine",
                label="Mining patterns",
                code_ref="learning.mine_patterns",
                runner_phase="mining",
            ),
            CycleStepDef(
                sid="seek",
                label="Active pattern seeking",
                code_ref="learning.seek_pattern_data",
                runner_phase="active_seeking",
            ),
        ),
    ),
    CycleClusterDef(
        id="c_validation",
        label="Evidence & backtests",
        phase_summary="insight BT (optional) → ScanPattern queue",
        steps=(
            CycleStepDef(
                sid="bt_insights",
                label="Backtesting insights",
                code_ref="learning._auto_backtest_patterns (brain_insight_backtest_on_cycle)",
                runner_phase="backtesting",
            ),
            CycleStepDef(
                sid="bt_queue",
                label="Backtesting patterns from queue",
                code_ref="learning._auto_backtest_from_queue",
                runner_phase="queue_backtesting",
            ),
        ),
    ),
    CycleClusterDef(
        id="c_evolution",
        label="Evolution & hypotheses",
        phase_summary="variants → validate_and_evolve → breakouts",
        steps=(
            CycleStepDef(
                sid="variants",
                label="Evolving pattern variants",
                code_ref="learning.evolve_pattern_strategies",
                runner_phase="pattern_variant_evolution",
            ),
            CycleStepDef(
                sid="hypotheses",
                label="Testing hypotheses & evolving strategy",
                code_ref="learning.validate_and_evolve",
                runner_phase="evolving",
            ),
            CycleStepDef(
                sid="breakout",
                label="Learning from breakout outcomes",
                code_ref="learning.learn_from_breakout_outcomes",
                runner_phase="breakout_learning",
            ),
        ),
    ),
    CycleClusterDef(
        id="c_secondary",
        label="Secondary miners",
        phase_summary="brain_secondary_miners_on_cycle",
        steps=(
            CycleStepDef(
                sid="intraday_hv",
                label="Mining intraday breakout patterns",
                code_ref="mine_intraday_patterns + mine_high_vol_regime_patterns",
                runner_phase="intraday_mining",
            ),
            CycleStepDef(
                sid="refine",
                label="Refining patterns",
                code_ref="learning.refine_patterns",
                runner_phase="refining",
            ),
            CycleStepDef(
                sid="exit",
                label="Learning exit optimization",
                code_ref="learning.learn_exit_optimization",
                runner_phase="exit_optimization",
            ),
            CycleStepDef(
                sid="fakeout",
                label="Mining fakeout patterns",
                code_ref="learning.mine_fakeout_patterns",
                runner_phase="fakeout_mining",
            ),
            CycleStepDef(
                sid="sizing",
                label="Tuning position sizing",
                code_ref="learning.tune_position_sizing",
                runner_phase="position_sizing",
            ),
            CycleStepDef(
                sid="inter_alert",
                label="Learning inter-alert patterns",
                code_ref="learning.learn_inter_alert_patterns",
                runner_phase="inter_alert",
            ),
            CycleStepDef(
                sid="timeframe",
                label="Learning timeframe performance",
                code_ref="learning.learn_timeframe_performance",
                runner_phase="timeframe_learning",
            ),
            CycleStepDef(
                sid="synergy",
                label="Mining signal synergies",
                code_ref="learning.mine_signal_synergies",
                runner_phase="synergy_mining",
            ),
        ),
    ),
    CycleClusterDef(
        id="c_journal",
        label="Journal & signals",
        phase_summary="journaling → signal events",
        steps=(
            CycleStepDef(
                sid="journal",
                label="Writing market journal",
                code_ref="journal.daily_market_journal",
                runner_phase="journaling",
            ),
            CycleStepDef(
                sid="signals",
                label="Checking signal events",
                code_ref="journal.check_signal_events",
                runner_phase="signals",
            ),
        ),
    ),
    CycleClusterDef(
        id="c_meta",
        label="Meta-learning & cycle close",
        phase_summary="ML → proposals → pattern engine → report → finalize",
        steps=(
            CycleStepDef(
                sid="ml",
                label="Training pattern meta-learner",
                code_ref="pattern_ml.get_meta_learner + apply_ml_feedback",
                runner_phase="ml_training",
            ),
            CycleStepDef(
                sid="proposals",
                label="Generating strategy proposals",
                code_ref="alerts.generate_strategy_proposals",
                runner_phase="proposals",
            ),
            CycleStepDef(
                sid="pattern_engine",
                label="Pattern discovery & evolution",
                code_ref="learning._run_pattern_engine_cycle",
                runner_phase="pattern_engine",
            ),
            CycleStepDef(
                sid="cycle_report",
                label="Generating cycle AI report",
                code_ref="learning_cycle_report.generate_and_store_cycle_report",
                runner_phase="cycle_ai_report",
            ),
            CycleStepDef(
                sid="depromote",
                label="Live vs research depromotion",
                code_ref="learning.run_live_pattern_depromotion",
                runner_phase="live_depromotion",
            ),
            CycleStepDef(
                sid="finalize",
                label="Finalizing",
                code_ref="run_learning_cycle finalize + log_learning_event",
                runner_phase="finalizing",
            ),
        ),
    ),
)


def _build_step_index() -> dict[tuple[str, str], CycleStepDef]:
    idx: dict[tuple[str, str], CycleStepDef] = {}
    for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        for s in c.steps:
            key = (c.id, s.sid)
            if key in idx:
                raise ValueError(f"duplicate cycle step key: {key}")
            idx[key] = s
    return idx


_CYCLE_STEP_INDEX: dict[tuple[str, str], CycleStepDef] = _build_step_index()


def get_cycle_step(cluster_id: str, step_sid: str) -> CycleStepDef:
    try:
        return _CYCLE_STEP_INDEX[(cluster_id, step_sid)]
    except KeyError as e:
        raise KeyError(f"unknown cycle step ({cluster_id!r}, {step_sid!r})") from e


def apply_learning_cycle_step_status(status_dict: dict[str, Any], cluster_id: str, step_sid: str) -> None:
    s = get_cycle_step(cluster_id, step_sid)
    status_dict["current_step"] = s.label
    status_dict["phase"] = s.runner_phase


def apply_learning_cycle_step_status_progress(
    status_dict: dict[str, Any],
    cluster_id: str,
    step_sid: str,
    done: int,
    total: int,
) -> None:
    s = get_cycle_step(cluster_id, step_sid)
    status_dict["current_step"] = f"{s.label} ({done}/{total})"
    status_dict["phase"] = s.runner_phase
