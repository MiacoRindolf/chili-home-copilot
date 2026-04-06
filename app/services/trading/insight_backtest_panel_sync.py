"""Align TradingInsight.win_count / loss_count with Brain evidence panel math.

The panel uses deduped (ticker, strategy) representatives and trade-weighted simulated
wins/losses from stored per-backtest win_rate × trade_count. Legacy paths used
``return_pct > 0`` per row (undeduplicated), which disagreed with the UI and learning
messages that implied the panel definition.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def sync_insight_backtest_tallies_from_evidence_panel(
    db: "Session",
    insight: Any,
    *,
    learning_event_descriptions: list[str] | None = None,
) -> dict[str, Any]:
    """Recompute win/loss tallies from the same aggregate as the evidence modal.

    Mutates ``insight`` in memory; caller commits. Returns the panel dict from
    ``_compute_deduped_backtest_win_stats`` for optional confidence updates.
    """
    from ...routers.trading_sub import ai as _brain_ai

    sp_resolved = _brain_ai._resolve_scan_pattern_id_for_insight(db, insight)
    desc = insight.pattern_description or ""
    univ = _brain_ai._evidence_backtest_asset_universe(
        db,
        desc,
        sp_resolved,
        insight_id=insight.id,
        learning_event_descriptions=learning_event_descriptions or [],
    )
    panel = _brain_ai._compute_deduped_backtest_win_stats(
        db,
        [int(insight.id)],
        asset_universe=univ,
        scan_pattern_id=sp_resolved,
    )
    insight.win_count = int(panel["bt_wins"])
    insight.loss_count = int(panel["bt_losses"])
    return panel
