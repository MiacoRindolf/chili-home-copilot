"""Trading Brain network graph — **grounded in** ``run_learning_cycle``.

This is not decorative: cluster order matches the main learning pipeline in
``app.services.trading.learning.run_learning_cycle`` (roughly lines 7460–7923).
Step labels match the user-facing ``current_step`` strings set in that function
where applicable; ``code_ref`` on each step points maintainers to the callable.

Optional secondary miners are gated by ``settings.brain_secondary_miners_on_cycle``;
the graph always lists them so the architecture is complete.

Pipeline edges (``kind: "pipeline"``) connect consecutive macro phases in execution
order. Governance edges go from the orchestrator root to each cluster, and from
each cluster to its concrete steps.
"""
from __future__ import annotations

import math
from typing import Any, TypedDict


class _StepDef(TypedDict):
    sid: str
    label: str
    code_ref: str


class _ClusterDef(TypedDict):
    id: str
    label: str
    phase: str
    steps: list[_StepDef]


# Macro phases in **execution order** inside run_learning_cycle.
_CLUSTERS: list[_ClusterDef] = [
    {
        "id": "c_universe",
        "label": "Universe & scoring",
        "phase": "pre-filtering → scanning",
        "steps": [
            {
                "sid": "prefilter",
                "label": "Pre-filtering market",
                "code_ref": "prescreener.get_prescreened_candidates",
            },
            {
                "sid": "scan",
                "label": "Scanning market",
                "code_ref": "scanner.run_full_market_scan",
            },
        ],
    },
    {
        "id": "c_state",
        "label": "Market state & memory",
        "phase": "snapshots → backfill → confidence decay",
        "steps": [
            {
                "sid": "snapshots",
                "label": "Taking market snapshots",
                "code_ref": "learning.take_snapshots_parallel",
            },
            {
                "sid": "backfill",
                "label": "Backfilling future returns",
                "code_ref": "learning.backfill_future_returns (+ backfill_predicted_scores)",
            },
            {
                "sid": "decay",
                "label": "Decaying stale insights",
                "code_ref": "learning.decay_stale_insights",
            },
        ],
    },
    {
        "id": "c_discovery",
        "label": "Pattern discovery",
        "phase": "mining → active seeking",
        "steps": [
            {
                "sid": "mine",
                "label": "Mining patterns",
                "code_ref": "learning.mine_patterns",
            },
            {
                "sid": "seek",
                "label": "Active pattern seeking",
                "code_ref": "learning.seek_pattern_data",
            },
        ],
    },
    {
        "id": "c_validation",
        "label": "Evidence & backtests",
        "phase": "insight BT (optional) → ScanPattern queue",
        "steps": [
            {
                "sid": "bt_insights",
                "label": "Backtesting insights",
                "code_ref": "learning._auto_backtest_patterns (brain_insight_backtest_on_cycle)",
            },
            {
                "sid": "bt_queue",
                "label": "Backtesting patterns from queue",
                "code_ref": "learning._auto_backtest_from_queue",
            },
        ],
    },
    {
        "id": "c_evolution",
        "label": "Evolution & hypotheses",
        "phase": "variants → validate_and_evolve → breakouts",
        "steps": [
            {
                "sid": "variants",
                "label": "Evolving pattern variants",
                "code_ref": "learning.evolve_pattern_strategies",
            },
            {
                "sid": "hypotheses",
                "label": "Testing hypotheses & evolving strategy",
                "code_ref": "learning.validate_and_evolve",
            },
            {
                "sid": "breakout",
                "label": "Learning from breakout outcomes",
                "code_ref": "learning.learn_from_breakout_outcomes",
            },
        ],
    },
    {
        "id": "c_secondary",
        "label": "Secondary miners",
        "phase": "brain_secondary_miners_on_cycle",
        "steps": [
            {
                "sid": "intraday_hv",
                "label": "Mining intraday breakout patterns",
                "code_ref": "mine_intraday_patterns + mine_high_vol_regime_patterns",
            },
            {
                "sid": "refine",
                "label": "Refining patterns",
                "code_ref": "learning.refine_patterns",
            },
            {
                "sid": "exit",
                "label": "Learning exit optimization",
                "code_ref": "learning.learn_exit_optimization",
            },
            {
                "sid": "fakeout",
                "label": "Mining fakeout patterns",
                "code_ref": "learning.mine_fakeout_patterns",
            },
            {
                "sid": "sizing",
                "label": "Tuning position sizing",
                "code_ref": "learning.tune_position_sizing",
            },
            {
                "sid": "inter_alert",
                "label": "Learning inter-alert patterns",
                "code_ref": "learning.learn_inter_alert_patterns",
            },
            {
                "sid": "timeframe",
                "label": "Learning timeframe performance",
                "code_ref": "learning.learn_timeframe_performance",
            },
            {
                "sid": "synergy",
                "label": "Mining signal synergies",
                "code_ref": "learning.mine_signal_synergies",
            },
        ],
    },
    {
        "id": "c_journal",
        "label": "Journal & signals",
        "phase": "journaling → signal events",
        "steps": [
            {
                "sid": "journal",
                "label": "Writing market journal",
                "code_ref": "journal.daily_market_journal",
            },
            {
                "sid": "signals",
                "label": "Checking signal events",
                "code_ref": "journal.check_signal_events",
            },
        ],
    },
    {
        "id": "c_meta",
        "label": "Meta-learning & cycle close",
        "phase": "ML → proposals → pattern engine → report → finalize",
        "steps": [
            {
                "sid": "ml",
                "label": "Training pattern meta-learner",
                "code_ref": "pattern_ml.get_meta_learner + apply_ml_feedback",
            },
            {
                "sid": "proposals",
                "label": "Generating strategy proposals",
                "code_ref": "alerts.generate_strategy_proposals",
            },
            {
                "sid": "pattern_engine",
                "label": "Pattern discovery & evolution",
                "code_ref": "learning._run_pattern_engine_cycle",
            },
            {
                "sid": "cycle_report",
                "label": "Generating cycle AI report",
                "code_ref": "learning_cycle_report.generate_and_store_cycle_report",
            },
            {
                "sid": "depromote",
                "label": "Live vs research depromotion",
                "code_ref": "learning.run_live_pattern_depromotion",
            },
            {
                "sid": "finalize",
                "label": "Finalizing",
                "code_ref": "run_learning_cycle finalize + log_learning_event",
            },
        ],
    },
]

_ROOT_ID = "tb_root"


def _cluster_positions(n: int, cx: float, cy: float, radius: float) -> list[tuple[float, float]]:
    return [
        (
            cx + radius * math.cos(-math.pi / 2 + (2 * math.pi * i) / n),
            cy + radius * math.sin(-math.pi / 2 + (2 * math.pi * i) / n),
        )
        for i in range(n)
    ]


def get_trading_brain_network_graph() -> dict[str, Any]:
    n_cl = len(_CLUSTERS)
    root_x, root_y = 800.0, 475.0
    positions = _cluster_positions(n_cl, root_x, root_y, 300.0)

    nodes: list[dict[str, Any]] = [
        {
            "id": _ROOT_ID,
            "label": "Trading Brain",
            "tier": "root",
            "x": root_x,
            "y": root_y,
            "code_ref": "app.services.trading.learning.run_learning_cycle",
        }
    ]
    edges: list[dict[str, str]] = []

    for ci, cdef in enumerate(_CLUSTERS):
        cx, cy = positions[ci]
        cid = cdef["id"]
        nodes.append(
            {
                "id": cid,
                "label": cdef["label"],
                "tier": "cluster",
                "x": cx,
                "y": cy,
                "phase": cdef["phase"],
                "code_ref": "run_learning_cycle → " + cid,
            }
        )
        edges.append({"from": _ROOT_ID, "to": cid, "kind": "governance"})

        n_st = len(cdef["steps"])
        sr = 92.0 + min(n_st, 8) * 5.0
        for si, st in enumerate(cdef["steps"]):
            angle = -1.15 + (2.3 * (si + 0.5) / max(n_st, 1))
            sx = cx + sr * math.cos(angle)
            sy = cy + sr * math.sin(angle)
            sid = f"s_{cid}_{st['sid']}"
            nodes.append(
                {
                    "id": sid,
                    "label": st["label"],
                    "tier": "step",
                    "x": sx,
                    "y": sy,
                    "code_ref": st["code_ref"],
                }
            )
            edges.append({"from": cid, "to": sid, "kind": "governance"})

    for i in range(n_cl - 1):
        a, b = _CLUSTERS[i]["id"], _CLUSTERS[i + 1]["id"]
        edges.append({"from": a, "to": b, "kind": "pipeline"})

    meta = {
        "source_module": "app.services.trading.learning",
        "source_symbol": "run_learning_cycle",
        "graph_version": 3,
        "cluster_count": n_cl,
        "description": (
            "Macro phases follow the learning cycle call order; step labels align with "
            "run_learning_cycle current_step strings where applicable. Pipeline edges show "
            "sequential phase flow; governance edges show orchestration (root→subsystem, "
            "cluster→callable step)."
        ),
    }

    return {"ok": True, "meta": meta, "nodes": nodes, "edges": edges}
