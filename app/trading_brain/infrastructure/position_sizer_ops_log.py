"""Structured one-line ops log for the canonical position sizer (Phase H).

Emitted whenever the canonical sizer writes a shadow proposal (or, in a
future Phase H.2, an authoritative sizing decision). Release blockers
assert that no ``event=proposal mode=authoritative`` line appears until
Phase H.2 is explicitly opened.

Log prefix: ``[position_sizer_ops]``.
"""
from __future__ import annotations

from typing import Any

CHILI_POSITION_SIZER_OPS_PREFIX = "[position_sizer_ops]"


def format_position_sizer_ops_line(
    *,
    event: str,  # "proposal" | "cap_triggered" | "divergence" | "summary"
    mode: str,
    proposal_id: str | None = None,
    source: str | None = None,
    ticker: str | None = None,
    direction: str | None = None,
    user_id: int | None = None,
    pattern_id: int | None = None,
    asset_class: str | None = None,
    regime: str | None = None,
    entry_price: float | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    capital: float | None = None,
    calibrated_prob: float | None = None,
    payoff_fraction: float | None = None,
    cost_fraction: float | None = None,
    expected_net_pnl: float | None = None,
    kelly_fraction: float | None = None,
    kelly_scaled_fraction: float | None = None,
    proposed_notional: float | None = None,
    proposed_quantity: float | None = None,
    proposed_risk_pct: float | None = None,
    correlation_cap_triggered: bool | None = None,
    correlation_bucket: str | None = None,
    max_bucket_notional: float | None = None,
    notional_cap_triggered: bool | None = None,
    legacy_notional: float | None = None,
    legacy_quantity: float | None = None,
    legacy_source: str | None = None,
    divergence_bps: float | None = None,
    proposals_total: int | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    """Format a single-line, whitespace-tokenized ops log entry.

    The format is deterministic so downstream grep / release-blocker
    scripts can rely on ``event=<e>`` and ``mode=<m>`` always appearing
    in the first two tokens after the prefix.
    """
    parts: list[str] = [
        CHILI_POSITION_SIZER_OPS_PREFIX,
        f"event={event}",
        f"mode={mode}",
    ]

    def _add(k: str, v: Any) -> None:
        if v is None:
            return
        if isinstance(v, bool):
            parts.append(f"{k}={str(v).lower()}")
        elif isinstance(v, str):
            if any(c.isspace() for c in v) or v == "":
                parts.append(f'{k}="{v}"')
            else:
                parts.append(f"{k}={v}")
        elif isinstance(v, float):
            parts.append(f"{k}={v:.6g}")
        else:
            parts.append(f"{k}={v}")

    _add("proposal_id", proposal_id)
    _add("source", source)
    _add("ticker", ticker)
    _add("direction", direction)
    _add("user_id", user_id)
    _add("pattern_id", pattern_id)
    _add("asset_class", asset_class)
    _add("regime", regime)
    _add("entry_price", entry_price)
    _add("stop_price", stop_price)
    _add("target_price", target_price)
    _add("capital", capital)
    _add("calibrated_prob", calibrated_prob)
    _add("payoff_fraction", payoff_fraction)
    _add("cost_fraction", cost_fraction)
    _add("expected_net_pnl", expected_net_pnl)
    _add("kelly_fraction", kelly_fraction)
    _add("kelly_scaled_fraction", kelly_scaled_fraction)
    _add("proposed_notional", proposed_notional)
    _add("proposed_quantity", proposed_quantity)
    _add("proposed_risk_pct", proposed_risk_pct)
    _add("correlation_cap_triggered", correlation_cap_triggered)
    _add("correlation_bucket", correlation_bucket)
    _add("max_bucket_notional", max_bucket_notional)
    _add("notional_cap_triggered", notional_cap_triggered)
    _add("legacy_notional", legacy_notional)
    _add("legacy_quantity", legacy_quantity)
    _add("legacy_source", legacy_source)
    _add("divergence_bps", divergence_bps)
    _add("proposals_total", proposals_total)
    _add("reason", reason)

    for k, v in extra.items():
        _add(k, v)

    return " ".join(parts)


__all__ = [
    "CHILI_POSITION_SIZER_OPS_PREFIX",
    "format_position_sizer_ops_line",
]
