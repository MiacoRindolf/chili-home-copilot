"""Optional secondary miner cluster for ``run_learning_cycle`` (lazy-imports ``learning``)."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..brain_resource_budget import BrainResourceBudget
from ..learning_cycle_architecture import apply_learning_cycle_step_status

logger = logging.getLogger(__name__)


def run_secondary_miners_phase(
    db: Session,
    user_id: int | None,
    *,
    settings: Any,
    cycle_budget: BrainResourceBudget,
    report: dict[str, Any],
    learning_status: dict[str, Any],
    bump_cycle_step: Callable[[], None],
    step_time: Callable[[str, float, str], None],
    commit_step: Callable[[], None],
    shutting_down_is_set: Callable[[], bool],
    mark_secondary_skipped: Callable[[], None],
) -> None:
    """Run intraday/HV mining, refine, exit, fakeout, sizing, inter-alert, timeframe, synergy."""
    if getattr(settings, "brain_secondary_miners_on_cycle", True):
        from .. import learning as L

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start_id = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "intraday_hv")
        intra_result = L.mine_intraday_patterns(db, user_id, cycle_budget)
        hv_result = L.mine_high_vol_regime_patterns(db, user_id, cycle_budget)
        report["intraday_discoveries"] = intra_result.get("discoveries", 0)
        report["high_vol_discoveries"] = hv_result.get("discoveries", 0)
        bump_cycle_step()
        step_time(
            "intraday_mining",
            step_start_id,
            f"compression {intra_result.get('discoveries', 0)} / high_vol {hv_result.get('discoveries', 0)} "
            f"from {intra_result.get('rows_mined', 0)} + {hv_result.get('rows_mined', 0)} bars",
        )
        commit_step()

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "refine")
        refine_result = L.refine_patterns(db, user_id)
        report["patterns_refined"] = refine_result.get("refined", 0)
        bump_cycle_step()
        step_time("refine", step_start, f"{refine_result.get('refined', 0)} patterns refined")
        commit_step()

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "exit")
        exit_result = L.learn_exit_optimization(db, user_id)
        report["exit_adjustments"] = exit_result.get("adjustments", 0)
        bump_cycle_step()
        step_time("exit_optimization", step_start, f"{exit_result.get('adjustments', 0)} adjustments")
        commit_step()

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "fakeout")
        fakeout_result = L.mine_fakeout_patterns(db, user_id)
        report["fakeout_patterns"] = fakeout_result.get("patterns_found", 0)
        bump_cycle_step()
        step_time("fakeout_mining", step_start, f"{fakeout_result.get('patterns_found', 0)} fakeout patterns")
        commit_step()

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "sizing")
        sizing_result = L.tune_position_sizing(db, user_id)
        report["sizing_adjustments"] = sizing_result.get("adjustments", 0)
        bump_cycle_step()
        step_time("position_sizing", step_start, f"{sizing_result.get('adjustments', 0)} sizing adjustments")
        commit_step()

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "inter_alert")
        inter_result = L.learn_inter_alert_patterns(db, user_id)
        report["inter_alert_insights"] = inter_result.get("insights", 0)
        bump_cycle_step()
        step_time("inter_alert", step_start, f"{inter_result.get('insights', 0)} inter-alert insights")
        commit_step()

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "timeframe")
        tf_result = L.learn_timeframe_performance(db, user_id)
        report["timeframe_insights"] = tf_result.get("insights", 0)
        bump_cycle_step()
        step_time("timeframe_learning", step_start, f"{tf_result.get('insights', 0)} timeframe insights")
        commit_step()

        if shutting_down_is_set():
            raise InterruptedError("shutdown")
        step_start = time.time()
        apply_learning_cycle_step_status(learning_status, "c_secondary", "synergy")
        synergy_result = L.mine_signal_synergies(db, user_id)
        report["synergies_found"] = synergy_result.get("synergies_found", 0)
        bump_cycle_step()
        step_time("synergy_mining", step_start, f"{synergy_result.get('synergies_found', 0)} synergies found")
        commit_step()
    else:
        report["secondary_miners_skipped"] = True
        mark_secondary_skipped()
        logger.info("[learning] Secondary miners skipped (brain_secondary_miners_on_cycle=false)")
