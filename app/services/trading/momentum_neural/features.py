"""Execution readiness / microstructure features (REST/L2/tape fill in later phases)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ExecutionReadinessFeatures:
    spread_bps: float | None = None
    bid_ask_drift_bps: float | None = None
    book_imbalance: float | None = None
    tape_velocity_z: float | None = None
    slippage_estimate_bps: float | None = None
    fee_to_target_ratio: float | None = None
    product_tradable: bool | None = None
    meta: dict[str, Any] | None = None

    @classmethod
    def from_meta(cls, meta: dict[str, Any] | None) -> ExecutionReadinessFeatures:
        if not meta:
            return cls()
        pt = meta.get("product_tradable")
        if pt is not None and not isinstance(pt, bool):
            pt = bool(pt)
        return cls(
            spread_bps=_f(meta.get("spread_bps")),
            bid_ask_drift_bps=_f(meta.get("bid_ask_drift_bps")),
            book_imbalance=_f(meta.get("book_imbalance")),
            tape_velocity_z=_f(meta.get("tape_velocity_z")),
            slippage_estimate_bps=_f(meta.get("slippage_estimate_bps")),
            fee_to_target_ratio=_f(meta.get("fee_to_target_ratio")),
            product_tradable=pt,
            meta=dict(meta),
        )

    def to_public_dict(self) -> dict[str, Any]:
        """JSON-safe execution-readiness snapshot for persistence."""
        out: dict[str, Any] = {}
        if self.spread_bps is not None:
            out["spread_bps"] = self.spread_bps
        if self.bid_ask_drift_bps is not None:
            out["bid_ask_drift_bps"] = self.bid_ask_drift_bps
        if self.book_imbalance is not None:
            out["book_imbalance"] = self.book_imbalance
        if self.tape_velocity_z is not None:
            out["tape_velocity_z"] = self.tape_velocity_z
        if self.slippage_estimate_bps is not None:
            out["slippage_estimate_bps"] = self.slippage_estimate_bps
        if self.fee_to_target_ratio is not None:
            out["fee_to_target_ratio"] = self.fee_to_target_ratio
        if self.product_tradable is not None:
            out["product_tradable"] = self.product_tradable
        if self.meta:
            out["extra"] = dict(self.meta)
        return out

    @classmethod
    def from_coinbase_normalized(
        cls,
        *,
        product: Any | None,
        ticker: Any | None,
    ) -> ExecutionReadinessFeatures:
        """Build features from ``venue.NormalizedProduct`` / ``NormalizedTicker`` (lazy import)."""
        from ..venue.readiness_bridge import execution_readiness_dict_from_normalized

        return cls.from_meta(
            execution_readiness_dict_from_normalized(product, ticker)  # type: ignore[arg-type]
        )


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
