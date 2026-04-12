"""Trading Brain network graph — layout + JSON from ``learning_cycle_architecture``.

Cluster and step definitions live in ``app.services.trading.learning_cycle_architecture``
(single source of truth). This module adds coordinates and edges only.

Pipeline edges connect consecutive macro phases. Governance edges: root→cluster,
cluster→step.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import math
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from .learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    TRADING_BRAIN_ROOT_METADATA,
)

_log = logging.getLogger(__name__)

_ROOT_ID = "tb_root"

# Public JSON ``meta.graph_version``; bump when graph shape or ordering contract changes.
TRADING_BRAIN_NETWORK_GRAPH_VERSION = 16

# Per-callable line cap: run_learning_cycle is ~700 lines; keep headroom for growth.
# Total cap: multi-part code_ref (e.g. ``a + b``) concatenates several callables.
_MAX_LINES_PER_CALLABLE = 20_000
_MAX_SNIPPET_CHARS = 2_000_000

# Bump when snippet rules change so in-process cache does not serve stale truncated text.
_SNIPPET_CACHE_VERSION = 3

_SHORT_MODULE_PREFIX: dict[str, str] = {
    "learning": "app.services.trading.learning",
    "prescreener": "app.services.trading.prescreener",
    "prescreen_job": "app.services.trading.prescreen_job",
    "scanner": "app.services.trading.scanner",
    "trading_scheduler": "app.services.trading_scheduler",
    "journal": "app.services.trading.journal",
    "pattern_ml": "app.services.trading.pattern_ml",
    "alerts": "app.services.trading.alerts",
    "learning_cycle_report": "app.services.trading.learning_cycle_report",
}

# Bare symbol names that are not on ``learning`` but appear in compound code_ref strings.
_BARE_SYMBOL_MODULE: dict[str, str] = {
    "log_learning_event": "app.services.trading.learning_events",
}

_SNIPPET_CACHE: dict[tuple[int, str], str] = {}


def _strip_trailing_parens_annotation(s: str) -> str:
    s = s.strip()
    while True:
        new = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
        if new == s:
            break
        s = new
    return s


def _cluster_synthetic_ref(ref: str) -> bool:
    return ref.strip().startswith("run_learning_cycle →")


def _first_dotted_identifier(token: str) -> str | None:
    m = re.match(r"^([\w.]+)", token.strip())
    return m.group(1) if m else None


def _resolve_ref_piece(piece: str, inherit_module: str | None) -> tuple[str, str] | None:
    piece = _strip_trailing_parens_annotation(piece)
    if not piece:
        return None
    head = _first_dotted_identifier(piece) or ""
    if not head:
        return None

    if head.startswith("app."):
        mod, _, attr = head.rpartition(".")
        if mod and attr:
            return mod, attr
        return None

    if "." in head:
        short, _, attr = head.partition(".")
        base = _SHORT_MODULE_PREFIX.get(short)
        if base:
            return base, attr
        return None

    mod = _BARE_SYMBOL_MODULE.get(head) or inherit_module or "app.services.trading.learning"
    return mod, head


def _split_ref_pieces(ref: str) -> list[str]:
    ref = _strip_trailing_parens_annotation(ref)
    return [p.strip() for p in re.split(r"\s+\+\s+", ref) if p.strip()]


def _snippet_for_callable(obj: Any, qual: str) -> str:
    try:
        lines, start = inspect.getsourcelines(obj)
    except (OSError, TypeError) as exc:
        return f"# {qual}\n# (source unavailable: {exc})\n"
    out = lines
    if len(out) > _MAX_LINES_PER_CALLABLE:
        extra = len(out) - _MAX_LINES_PER_CALLABLE
        end_line = start + len(out) - 1
        tail = start + _MAX_LINES_PER_CALLABLE - 1
        out = out[:_MAX_LINES_PER_CALLABLE] + [
            f"\n# ... truncated: omitted {extra} line(s); "
            f"source spans ~{start}–{end_line}, showing ~{start}–{tail} ...\n"
        ]
    return f"# --- {qual} (line {start}) ---\n" + "".join(out)


def build_code_snippet_from_ref(code_ref: str) -> str:
    """Resolve ``code_ref`` labels from ``learning_cycle_architecture`` to Python source excerpts."""
    ref = (code_ref or "").strip()
    if not ref:
        return ""
    ck = (_SNIPPET_CACHE_VERSION, ref)
    if ck in _SNIPPET_CACHE:
        return _SNIPPET_CACHE[ck]
    if _cluster_synthetic_ref(ref):
        out = (
            "# This cluster groups several steps inside run_learning_cycle.\n"
            "# Open the root node or step nodes for callable source.\n"
            f"# Reference: {ref}\n"
        )
        _SNIPPET_CACHE[ck] = out
        return out

    pieces = _split_ref_pieces(ref)
    if not pieces:
        out = "# (empty code reference)\n"
        _SNIPPET_CACHE[ck] = out
        return out

    chunks: list[str] = []
    inherit: str | None = None
    for piece in pieces:
        resolved = _resolve_ref_piece(piece, inherit)
        if not resolved:
            chunks.append(f"# Could not resolve: {piece!r}\n\n")
            continue
        mod_name, attr = resolved
        try:
            mod = importlib.import_module(mod_name)
            obj = getattr(mod, attr)
        except Exception as exc:
            chunks.append(f"# {mod_name}.{attr} — could not load: {exc}\n\n")
            continue
        inherit = mod_name
        if callable(obj):
            chunks.append(_snippet_for_callable(obj, f"{mod_name}.{attr}"))
        else:
            chunks.append(
                f"# {mod_name}.{attr} is not callable (type={type(obj).__name__})\n\n"
            )
        chunks.append("\n")

    out = "".join(chunks).strip()
    if len(out) > _MAX_SNIPPET_CHARS:
        out = (
            out[: _MAX_SNIPPET_CHARS - 120]
            + f"\n\n# ... truncated: total snippet exceeded {_MAX_SNIPPET_CHARS} characters ...\n"
        )
    _SNIPPET_CACHE[ck] = out
    return out


def _cluster_positions(n: int, cx: float, cy: float, radius: float) -> list[tuple[float, float]]:
    return [
        (
            cx + radius * math.cos(-math.pi / 2 + (2 * math.pi * i) / n),
            cy + radius * math.sin(-math.pi / 2 + (2 * math.pi * i) / n),
        )
        for i in range(n)
    ]


def get_trading_brain_network_graph(db: Optional[Session] = None) -> dict[str, Any]:
    clusters = TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
    n_cl = len(clusters)
    root_x, root_y = 800.0, 475.0
    positions = _cluster_positions(n_cl, root_x, root_y, 300.0)

    root_meta = TRADING_BRAIN_ROOT_METADATA
    root_ref = "app.services.trading.learning.run_learning_cycle"
    nodes: list[dict[str, Any]] = [
        {
            "id": _ROOT_ID,
            "label": "Trading Brain",
            "tier": "root",
            "x": root_x,
            "y": root_y,
            "code_ref": root_ref,
            "code_snippet": build_code_snippet_from_ref(root_ref),
            "description": root_meta.description,
            "remarks": root_meta.remarks,
            "inputs": list(root_meta.inputs),
            "outputs": list(root_meta.outputs),
        }
    ]
    edges: list[dict[str, str]] = []

    for ci, cdef in enumerate(clusters):
        cx, cy = positions[ci]
        cid = cdef.id
        cref = "run_learning_cycle → " + cid
        in_lc = cid != "c_universe"
        nodes.append(
            {
                "id": cid,
                "label": cdef.label,
                "tier": "cluster",
                "x": cx,
                "y": cy,
                "phase": cdef.phase_summary,
                "cluster_index": ci,
                "in_learning_cycle": in_lc,
                "code_ref": cref,
                "code_snippet": build_code_snippet_from_ref(cref),
                "description": cdef.description,
                "remarks": cdef.remarks,
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
                    "cluster_index": ci,
                    "step_index": si,
                    "in_learning_cycle": in_lc,
                    "code_ref": st.code_ref,
                    "code_snippet": build_code_snippet_from_ref(st.code_ref),
                    "description": st.description,
                    "remarks": st.remarks,
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
        "graph_version": TRADING_BRAIN_NETWORK_GRAPH_VERSION,
        "cluster_count": n_cl,
        "description": (
            "Macro phases follow the learning cycle call order; step labels align with "
            "run_learning_cycle current_step strings where applicable. Pipeline edges show "
            "sequential phase flow; governance edges show orchestration (root→subsystem, "
            "cluster→callable step). Node payloads include description, remarks (what/where/why), "
            "concrete inputs/outputs; prescreen persists to DB (daily job); scan reads "
            "active candidates; code_snippet per code_ref."
        ),
    }

    # Enrich step/cluster nodes with live mesh state when db is available
    if db is not None:
        try:
            _enrich_with_mesh_state(db, nodes)
        except Exception as e:
            _log.debug("mesh state enrichment skipped: %s", e)

    return {"ok": True, "meta": meta, "nodes": nodes, "edges": edges}


def _enrich_with_mesh_state(db: Session, nodes: list[dict[str, Any]]) -> None:
    """Merge BrainNodeState activation_score/confidence into display nodes."""
    from ...models.trading import BrainNodeState

    # Build lookup: display node id → mesh node id
    mesh_map: dict[str, str] = {}
    for n in nodes:
        nid = n["id"]
        tier = n.get("tier")
        if tier == "step":
            # Display id: s_{cluster}_{sid} → mesh id: nm_lc_{sid}
            parts = nid.split("_", 2)  # ["s", cluster_id, step_sid]
            if len(parts) == 3:
                mesh_map[nid] = f"nm_lc_{parts[2]}"
        elif tier == "cluster" and nid.startswith("c_"):
            mesh_map[nid] = f"nm_lc_{nid}"

    if not mesh_map:
        return

    mesh_ids = list(mesh_map.values())
    states = (
        db.query(BrainNodeState)
        .filter(BrainNodeState.node_id.in_(mesh_ids))
        .all()
    )
    state_by_id = {s.node_id: s for s in states}

    for n in nodes:
        mesh_nid = mesh_map.get(n["id"])
        if not mesh_nid:
            continue
        st = state_by_id.get(mesh_nid)
        if st:
            n["mesh_node_id"] = mesh_nid
            n["activation_score"] = round(float(st.activation_score or 0), 4)
            n["confidence"] = round(float(st.confidence or 0), 4)
            n["last_fired_at"] = st.last_fired_at.isoformat() if st.last_fired_at else None
            n["staleness_at"] = st.staleness_at.isoformat() if st.staleness_at else None
