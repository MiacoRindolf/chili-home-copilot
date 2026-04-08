"""Pure layout helpers for the Trading Brain neural graph projection."""

from __future__ import annotations

import math
from typing import Any, TypedDict

# Must match desk SVG in brain.html
VIEWPORT_W = 1600.0
VIEWPORT_H = 950.0
MARGIN = 80.0
CX = VIEWPORT_W / 2.0
CY = VIEWPORT_H / 2.0

# Layer 1 = outer (sensory), 7 = inner (meta). Tunable ladder (px from center).
CORE_RING_RADIUS: dict[int, float] = {
    1: 352.0,
    2: 300.0,
    3: 248.0,
    4: 200.0,
    5: 156.0,
    6: 116.0,
    7: 78.0,
}

# Hubs sit on a small diamond/square — not on the crowded L3 ring.
HUB_IDS = frozenset(
    {"nm_event_bus", "nm_working_memory", "nm_regime", "nm_contradiction", "nm_momentum_crypto_intel"}
)
HUB_DISTANCE_FROM_CENTER = 54.0
# Observers sit outside the core ring for their layer.
OBSERVER_RADIAL_OUTSET = 48.0

# Golden-ratio phase per layer so rings don't share identical rays.
PHI = 0.6180339887498949


class _NodeLay(TypedDict):
    id: str
    layer: int
    is_observer: bool


def truncate_neural_label(label: str, max_len: int = 20) -> str:
    """Short label for SVG; full string stays in API ``label`` and tooltips."""
    s = (label or "").strip()
    if len(s) <= max_len:
        return s
    if max_len < 3:
        return s[:max_len]
    return s[: max_len - 1] + "\u2026"


def _hub_positions_ordered(hub_ids_sorted: list[str]) -> dict[str, tuple[float, float]]:
    """Place up to 4 hubs on a diamond around (CX, CY)."""
    out: dict[str, tuple[float, float]] = {}
    # Start at top, go clockwise: N, E, S, W
    angles = [math.pi / 2, 0.0, -math.pi / 2, math.pi]
    r = HUB_DISTANCE_FROM_CENTER
    for i, nid in enumerate(hub_ids_sorted[:4]):
        th = angles[i % 4]
        out[nid] = (CX + r * math.cos(th), CY + r * math.sin(th))
    return out


def _even_angles(count: int, base_phase: float) -> list[float]:
    if count <= 0:
        return []
    return [base_phase + 2.0 * math.pi * i / count for i in range(count)]


def compute_neural_positions(nodes_min: list[_NodeLay]) -> tuple[dict[str, tuple[float, float]], dict[str, Any]]:
    """Compute pixel positions in viewBox space. Returns (id -> (x,y), layout_meta).

    layout_meta includes bounds before/after clamp and ring radii for guides.
    """
    positions: dict[str, tuple[float, float]] = {}
    used_core_radii: set[float] = set()

    hubs = sorted(
        (n for n in nodes_min if n["layer"] == 3 and n["id"] in HUB_IDS),
        key=lambda x: x["id"],
    )
    hub_pos = _hub_positions_ordered([h["id"] for h in hubs])
    for nid, p in hub_pos.items():
        positions[nid] = p

    for layer in range(1, 8):
        r_core = CORE_RING_RADIUS.get(layer, 78.0)
        layer_nodes = [n for n in nodes_min if n["layer"] == layer]
        # Exclude hubs from ring placement
        ring_candidates = [n for n in layer_nodes if not (layer == 3 and n["id"] in HUB_IDS)]
        core_nodes = sorted([n for n in ring_candidates if not n["is_observer"]], key=lambda x: x["id"])
        obs_nodes = sorted([n for n in ring_candidates if n["is_observer"]], key=lambda x: x["id"])

        if core_nodes:
            used_core_radii.add(r_core)
        base_phase = layer * PHI * 2.0 * math.pi
        for theta, n in zip(_even_angles(len(core_nodes), base_phase), core_nodes):
            positions[n["id"]] = (CX + r_core * math.cos(theta), CY + r_core * math.sin(theta))

        r_obs = r_core + OBSERVER_RADIAL_OUTSET
        base_o = base_phase + 0.47  # offset bundle from core rays
        for theta, n in zip(_even_angles(len(obs_nodes), base_o), obs_nodes):
            positions[n["id"]] = (CX + r_obs * math.cos(theta), CY + r_obs * math.sin(theta))

    if not positions:
        return positions, {
            "bounds": {"min_x": CX, "max_x": CX, "min_y": CY, "max_y": CY},
            "ring_radii_core": [],
        }

    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    # Pad for node circles + rough label extent
    pad = 56.0
    bw = max(max_x - min_x + 2 * pad, 120.0)
    bh = max(max_y - min_y + 2 * pad, 120.0)
    cx_box = 0.5 * (min_x + max_x)
    cy_box = 0.5 * (min_y + max_y)

    safe_w = VIEWPORT_W - 2 * MARGIN
    safe_h = VIEWPORT_H - 2 * MARGIN
    s = min(safe_w / bw, safe_h / bh, 1.0)

    fitted: dict[str, tuple[float, float]] = {}
    for nid, (x, y) in positions.items():
        nx = CX + s * (x - cx_box)
        ny = CY + s * (y - cy_box)
        fitted[nid] = (nx, ny)

    xs2 = [p[0] for p in fitted.values()]
    ys2 = [p[1] for p in fitted.values()]
    min_x2, max_x2 = min(xs2), max(xs2)
    min_y2, max_y2 = min(ys2), max(ys2)

    # Hard clamp (numerical safety)
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    final: dict[str, tuple[float, float]] = {}
    for nid, (x, y) in fitted.items():
        final[nid] = (
            _clamp(x, MARGIN + pad * 0.25, VIEWPORT_W - MARGIN - pad * 0.25),
            _clamp(y, MARGIN + pad * 0.25, VIEWPORT_H - MARGIN - pad * 0.25),
        )

    xs3 = [p[0] for p in final.values()]
    ys3 = [p[1] for p in final.values()]
    ring_radii_draw = sorted({round(s * CORE_RING_RADIUS[L], 2) for L in range(1, 8)})
    layer_ring_cues = [
        {"layer": L, "r": round(s * CORE_RING_RADIUS[L], 2), "abbr": f"L{L}"}
        for L in range(1, 8)
    ]
    layout_meta = {
        "bounds": {
            "min_x": round(min(xs3), 2),
            "max_x": round(max(xs3), 2),
            "min_y": round(min(ys3), 2),
            "max_y": round(max(ys3), 2),
        },
        "ring_radii_core": sorted(used_core_radii),
        "ring_radii_draw": ring_radii_draw,
        "layer_ring_cues": layer_ring_cues,
        "uniform_scale_applied": round(s, 6),
    }
    return final, layout_meta
