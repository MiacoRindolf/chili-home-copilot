"""Fast-path execution fee resolution.

Static fee settings are still the safe fallback, but the hot path can
use the broker's current fee tier when enabled so cost gates do not
depend on hand-entered Coinbase constants.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


MAKER_EXECUTION_MODES = frozenset({"maker_only", "maker_first_then_taker"})
DEFAULT_STATIC_MAKER_FEE_BPS = 40.0
DEFAULT_STATIC_TAKER_FEE_BPS = 60.0


@dataclass(frozen=True)
class FastPathFeeRates:
    maker_fee_bps: float
    taker_fee_bps: float
    source: str
    pricing_tier: str = ""
    error: str = ""

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {"fee_source": self.source}
        if self.pricing_tier:
            out["pricing_tier"] = self.pricing_tier
        if self.error:
            out["fee_error"] = self.error
        return out


def _settings_rates(settings: Any, *, source: str, error: str = "") -> FastPathFeeRates:
    return FastPathFeeRates(
        maker_fee_bps=_setting_fee_bps(
            settings,
            "cost_aware_maker_fee_bps",
            DEFAULT_STATIC_MAKER_FEE_BPS,
        ),
        taker_fee_bps=_setting_fee_bps(
            settings,
            "cost_aware_taker_fee_bps",
            DEFAULT_STATIC_TAKER_FEE_BPS,
        ),
        source=source,
        error=error,
    )


def _nonnegative_fee_bps(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        fee_bps = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(fee_bps) or fee_bps < 0.0:
        return None
    return fee_bps


def _setting_fee_bps(settings: Any, name: str, default: float) -> float:
    fee_bps = _nonnegative_fee_bps(getattr(settings, name, default))
    return default if fee_bps is None else fee_bps


def effective_fee_rates(settings: Any) -> FastPathFeeRates:
    if not bool(getattr(settings, "cost_aware_live_fee_enabled", False)):
        return _settings_rates(settings, source="settings")

    try:
        from app.services import coinbase_service

        live = coinbase_service.get_fee_rates_bps(prefer_env_credentials=True)
    except Exception as exc:
        return _settings_rates(
            settings, source="settings_fallback", error=str(exc)[:160],
        )

    if not live:
        return _settings_rates(
            settings, source="settings_fallback", error="live_fee_unavailable",
        )

    settings_fallback = _settings_rates(settings, source="settings")
    invalid_live_keys: list[str] = []
    maker_fee_bps = settings_fallback.maker_fee_bps
    taker_fee_bps = settings_fallback.taker_fee_bps
    if "maker_fee_bps" in live:
        live_maker_fee_bps = _nonnegative_fee_bps(live.get("maker_fee_bps"))
        if live_maker_fee_bps is None:
            invalid_live_keys.append("maker_fee_bps")
        else:
            maker_fee_bps = live_maker_fee_bps
    if "taker_fee_bps" in live:
        live_taker_fee_bps = _nonnegative_fee_bps(live.get("taker_fee_bps"))
        if live_taker_fee_bps is None:
            invalid_live_keys.append("taker_fee_bps")
        else:
            taker_fee_bps = live_taker_fee_bps
    if invalid_live_keys:
        return _settings_rates(
            settings,
            source="settings_fallback",
            error="live_fee_invalid:" + ",".join(invalid_live_keys),
        )
    return FastPathFeeRates(
        maker_fee_bps=maker_fee_bps,
        taker_fee_bps=taker_fee_bps,
        source="coinbase_live",
        pricing_tier=str(live.get("pricing_tier") or ""),
    )


def fee_bps_for_execution_mode(
    settings: Any,
    execution_mode: str,
) -> tuple[float, dict[str, Any]]:
    rates = effective_fee_rates(settings)
    exec_mode = str(execution_mode or "taker").strip().lower()
    if exec_mode in MAKER_EXECUTION_MODES:
        fee_bps = rates.maker_fee_bps
    else:
        fee_bps = rates.taker_fee_bps
    detail = rates.detail()
    detail["fee_bps"] = round(float(fee_bps), 4)
    return float(fee_bps), detail
