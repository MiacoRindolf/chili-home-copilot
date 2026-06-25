"""Execution readiness / microstructure features (REST/L2/tape fill in later phases)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ExecutionReadinessFeatures:
    spread_bps: float | None = None
    bid_ask_drift_bps: float | None = None
    book_imbalance: float | None = None
    # Order-flow imbalance (Cont/Kukanov/Stoikov), normalized net directional
    # fraction in [-1, 1] over a short window; >0 = net buying pressure.
    ofi: float | None = None
    # Micro-price (Stoikov) edge vs mid, in bps; >0 = micro above mid (bid-heavy).
    micro_price_edge: float | None = None
    # Executed-tape AGGRESSOR imbalance in [-1, 1] (signed-volume; Ross's "ask getting
    # eaten" = real buying thrust). CONFIRMS OFI; scales the OFI tilt, never votes alone.
    trade_flow: float | None = None
    tape_velocity_z: float | None = None
    slippage_estimate_bps: float | None = None
    fee_to_target_ratio: float | None = None
    product_tradable: bool | None = None
    # Ross SS101 float-rotation sustainability (cumulative session volume / shares float):
    # how many times the move has turned over the tradeable float so far today, and that
    # pace projected to the close. A SELECTION/sustainability annotation (computed by
    # ``ross_momentum.float_rotation_signal``), carried here for persistence + audit only —
    # default None ⇒ omitted from ``to_public_dict`` ⇒ byte-identical when absent. Equity-only.
    float_rotation: float | None = None
    projected_rotation_at_eod: float | None = None
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
            ofi=_f(meta.get("ofi")),
            micro_price_edge=_f(meta.get("micro_price_edge")),
            trade_flow=_f(meta.get("trade_flow")),
            tape_velocity_z=_f(meta.get("tape_velocity_z")),
            slippage_estimate_bps=_f(meta.get("slippage_estimate_bps")),
            fee_to_target_ratio=_f(meta.get("fee_to_target_ratio")),
            product_tradable=pt,
            float_rotation=_f(meta.get("float_rotation")),
            projected_rotation_at_eod=_f(meta.get("projected_rotation_at_eod")),
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
        if self.ofi is not None:
            out["ofi"] = self.ofi
        if self.micro_price_edge is not None:
            out["micro_price_edge"] = self.micro_price_edge
        if self.trade_flow is not None:
            out["trade_flow"] = self.trade_flow
        if self.tape_velocity_z is not None:
            out["tape_velocity_z"] = self.tape_velocity_z
        if self.slippage_estimate_bps is not None:
            out["slippage_estimate_bps"] = self.slippage_estimate_bps
        if self.fee_to_target_ratio is not None:
            out["fee_to_target_ratio"] = self.fee_to_target_ratio
        if self.product_tradable is not None:
            out["product_tradable"] = self.product_tradable
        if self.float_rotation is not None:
            out["float_rotation"] = self.float_rotation
        if self.projected_rotation_at_eod is not None:
            out["projected_rotation_at_eod"] = self.projected_rotation_at_eod
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
