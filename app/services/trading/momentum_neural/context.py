"""Crypto-oriented session / regime context for neural momentum."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class VolatilityRegime(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    extreme = "extreme"


class ChopExpansionRegime(str, Enum):
    chop = "chop"
    expansion = "expansion"
    mixed = "mixed"


@dataclass(frozen=True)
class MomentumRegimeContext:
    utc_iso: str
    utc_hour: int
    session_label: str
    vol_regime: VolatilityRegime
    chop_expansion: ChopExpansionRegime
    spread_regime: str
    fee_burden_regime: str
    liquidity_regime: str
    exhaustion_cooldown: str
    rolling_range_state: str
    breakout_continuity: str
    meta: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        meta = dict(self.meta)
        # Promote key microstructure / vol fields for consumers that read top-level only (e.g. regime_atr_pct).
        atr_top = meta.get("atr_pct")
        out: dict[str, Any] = {
            "utc_iso": self.utc_iso,
            "utc_hour": self.utc_hour,
            "session_label": self.session_label,
            "volatility_regime": self.vol_regime.value,
            "chop_expansion": self.chop_expansion.value,
            "spread_regime": self.spread_regime,
            "fee_burden_regime": self.fee_burden_regime,
            "liquidity_regime": self.liquidity_regime,
            "exhaustion_cooldown": self.exhaustion_cooldown,
            "rolling_range_state": self.rolling_range_state,
            "breakout_continuity": self.breakout_continuity,
            "meta": meta,
        }
        if atr_top is not None:
            try:
                out["atr_pct"] = float(atr_top)
            except (TypeError, ValueError):
                pass
        # Phase 6c: continuous regime enrichments (optional; pipeline may omit).
        for k in (
            "chop_expansion_score",
            "adx_strength",
            "hurst_proxy",
            "realized_vol_rank",
        ):
            if k in meta and k not in out:
                out[k] = meta[k]
        return out


def _session_label_from_utc_hour(hour: int) -> str:
    if 0 <= hour < 8:
        return "asia"
    if 7 <= hour < 14:
        return "europe"
    if 13 <= hour < 21:
        return "us"
    return "americas_late"


def build_momentum_regime_context(
    *,
    now: datetime | None = None,
    realized_vol_rank: float | None = None,
    atr_pct: float | None = None,
    meta: dict[str, Any] | None = None,
) -> MomentumRegimeContext:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    hour = int(now.hour)
    session = _session_label_from_utc_hour(hour)

    vol = VolatilityRegime.normal
    if realized_vol_rank is not None:
        if realized_vol_rank < 0.25:
            vol = VolatilityRegime.low
        elif realized_vol_rank < 0.55:
            vol = VolatilityRegime.normal
        elif realized_vol_rank < 0.8:
            vol = VolatilityRegime.high
        else:
            vol = VolatilityRegime.extreme
    elif atr_pct is not None:
        if atr_pct < 0.008:
            vol = VolatilityRegime.low
        elif atr_pct < 0.02:
            vol = VolatilityRegime.normal
        elif atr_pct < 0.045:
            vol = VolatilityRegime.high
        else:
            vol = VolatilityRegime.extreme

    chop = ChopExpansionRegime.mixed
    if atr_pct is not None:
        chop = ChopExpansionRegime.expansion if atr_pct > 0.025 else ChopExpansionRegime.chop

    m = dict(meta or {})
    # Continuous regime score (Phase 6c): centered ~0.012 ATR/price, scaled.
    if atr_pct is not None:
        try:
            ap = float(atr_pct)
            m.setdefault("chop_expansion_score", max(-1.0, min(1.0, (ap - 0.012) / 0.02)))
        except (TypeError, ValueError):
            pass
    # ADX strength 0..1 when pipeline supplies adx (14) in meta.
    adx_raw = m.get("adx") or m.get("adx_14")
    if adx_raw is not None and "adx_strength" not in m:
        try:
            m["adx_strength"] = max(0.0, min(1.0, float(adx_raw) / 50.0))
        except (TypeError, ValueError):
            pass
    if m.get("hurst_proxy") is None:
        m["hurst_proxy"] = 0.5

    return MomentumRegimeContext(
        utc_iso=now.isoformat(),
        utc_hour=hour,
        session_label=session,
        vol_regime=vol,
        chop_expansion=chop,
        spread_regime=str(m.pop("spread_regime", "unknown")),
        fee_burden_regime=str(m.pop("fee_burden_regime", "unknown")),
        liquidity_regime=str(m.pop("liquidity_regime", "unknown")),
        exhaustion_cooldown=str(m.pop("exhaustion_cooldown", "none")),
        rolling_range_state=str(m.pop("rolling_range_state", "unknown")),
        breakout_continuity=str(m.pop("breakout_continuity", "unknown")),
        meta=m,
    )
