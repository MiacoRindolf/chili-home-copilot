"""Trading Brain network graph — layout + JSON from ``learning_cycle_architecture``.

Cluster and step definitions live in ``app.services.trading.learning_cycle_architecture``
(single source of truth). This module adds coordinates and edges only.

Pipeline edges connect consecutive macro phases. Governance edges: root→cluster,
cluster→step.
"""
from __future__ import annotations

import math
from typing import Any

from .learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    TRADING_BRAIN_ROOT_METADATA,
)

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
    clusters = TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
    n_cl = len(clusters)
    root_x, root_y = 800.0, 475.0
    positions = _cluster_positions(n_cl, root_x, root_y, 300.0)

    root_meta = TRADING_BRAIN_ROOT_METADATA
    nodes: list[dict[str, Any]] = [
        {
            "id": _ROOT_ID,
            "label": "Trading Brain",
            "tier": "root",
            "x": root_x,
            "y": root_y,
            "code_ref": "app.services.trading.learning.run_learning_cycle",
            "description": root_meta.description,
            "inputs": list(root_meta.inputs),
            "outputs": list(root_meta.outputs),
        }
    ]
    edges: list[dict[str, str]] = []

    for ci, cdef in enumerate(clusters):
        cx, cy = positions[ci]
        cid = cdef.id
        nodes.append(
            {
                "id": cid,
                "label": cdef.label,
                "tier": "cluster",
                "x": cx,
                "y": cy,
                "phase": cdef.phase_summary,
                "code_ref": "run_learning_cycle → " + cid,
                "description": cdef.description,
                "inputs": list(cdef.inputs),
                "outputs": list(cdef.outputs),
            }
        )
        edges.append({"from": _ROOT_ID, "to": cid, "kind": "governance"})

        n_st = len(cdef.steps)
        sr = 92.0 + min(n_st, 8) * 5.0
        spread = 2.3
        half = spread / 2.0
        theta0 = math.atan2(cy - root_y, cx - root_x)
        for si, st in enumerate(cdef.steps):
            angle = theta0 - half + spread * (si + 0.5) / max(n_st, 1)
            sx = cx + sr * math.cos(angle)
            sy = cy + sr * math.sin(angle)
            sid = f"s_{cid}_{st.sid}"
            nodes.append(
                {
                    "id": sid,
                    "label": st.label,
                    "tier": "step",
                    "x": sx,
                    "y": sy,
                    "code_ref": st.code_ref,
                    "description": st.description,
                    "inputs": list(st.inputs),
                    "outputs": list(st.outputs),
                }
            )
            edges.append({"from": cid, "to": sid, "kind": "governance"})

    for i in range(n_cl - 1):
        a, b = clusters[i].id, clusters[i + 1].id
        edges.append({"from": a, "to": b, "kind": "pipeline"})

    meta = {
        "source_module": "app.services.trading.learning",
        "source_symbol": "run_learning_cycle",
        "architecture_source": "learning_cycle_architecture",
        "graph_version": 6,
        "cluster_count": n_cl,
        "description": (
            "Macro phases follow the learning cycle call order; step labels align with "
            "run_learning_cycle current_step strings where applicable. Pipeline edges show "
            "sequential phase flow; governance edges show orchestration (root→subsystem, "
            "cluster→callable step). Node data is generated from learning_cycle_architecture."
        ),
    }

    return {"ok": True, "meta": meta, "nodes": nodes, "edges": edges}
