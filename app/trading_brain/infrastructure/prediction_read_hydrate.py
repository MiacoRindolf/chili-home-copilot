"""Hydrate ORM lines → legacy-shaped dicts (Phase 5)."""

from __future__ import annotations

from typing import Any

from ...models.trading_brain_phase1 import BrainPredictionLine


def mirror_lines_to_legacy_rows(lines: list[BrainPredictionLine]) -> list[dict[str, Any]]:
    ordered = sorted(lines, key=lambda r: int(r.sort_rank))
    out: list[dict[str, Any]] = []
    for row in ordered:
        out.append(
            {
                "ticker": row.ticker,
                "price": row.price,
                "score": float(row.score),
                "meta_ml_probability": row.meta_ml_probability,
                "direction": row.direction,
                "confidence": row.confidence,
                "signals": list(row.signals_json or []),
                "matched_patterns": list(row.matched_patterns_json or []),
                "vix_regime": row.vix_regime,
                "suggested_stop": row.suggested_stop,
                "suggested_target": row.suggested_target,
                "risk_reward": row.risk_reward,
                "position_size_pct": row.position_size_pct,
            }
        )
    return out
