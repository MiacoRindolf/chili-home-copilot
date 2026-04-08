"""Venue adapter protocol + normalized DTOs (execution layer; not neural logic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable


class VenueAdapterError(Exception):
    """Raised for venue-specific failures when strict mode is used."""

    def __init__(self, message: str, *, code: str | None = None, raw: Any = None):
        super().__init__(message)
        self.code = code
        self.raw = raw


@dataclass(frozen=True)
class FreshnessMeta:
    """Wall-clock metadata for a venue read (UTC)."""

    retrieved_at_utc: datetime
    provider_time_utc: Optional[datetime] = None
    max_age_seconds: float = 15.0

    def age_seconds(self, *, now: datetime | None = None) -> float:
        ref = now or datetime.now(timezone.utc)
        if self.retrieved_at_utc.tzinfo is None:
            ra = self.retrieved_at_utc.replace(tzinfo=timezone.utc)
        else:
            ra = self.retrieved_at_utc
        return max(0.0, (ref - ra).total_seconds())


def is_fresh_enough(meta: FreshnessMeta, *, now: datetime | None = None) -> bool:
    return meta.age_seconds(now=now) <= float(meta.max_age_seconds)


def require_fresh_or_raise(meta: FreshnessMeta, *, strict: bool = True) -> None:
    """Raise ``VenueAdapterError`` if ``strict`` and wall-clock age exceeds ``meta.max_age_seconds``."""
    if not strict:
        return
    if not is_fresh_enough(meta):
        raise VenueAdapterError(
            "market data older than max_age_seconds",
            code="stale",
            raw={"age_seconds": meta.age_seconds(), "max_age_seconds": meta.max_age_seconds},
        )


@dataclass(frozen=True)
class NormalizedProduct:
    """Spot product constraints (only fields we could map from Coinbase)."""

    product_id: str
    base_currency: str
    quote_currency: str
    status: str
    trading_disabled: bool
    cancel_only: bool
    limit_only: bool
    post_only: bool
    auction_mode: bool
    base_min_size: Optional[float] = None
    base_max_size: Optional[float] = None
    quote_min_size: Optional[float] = None
    quote_max_size: Optional[float] = None
    min_market_funds: Optional[float] = None
    max_market_funds: Optional[float] = None
    base_increment: Optional[float] = None
    quote_increment: Optional[float] = None
    price_increment: Optional[float] = None
    product_type: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def tradable_for_spot_momentum(self) -> bool:
        if self.trading_disabled:
            return False
        if self.status.lower() not in ("online", "active"):
            return False
        return not self.cancel_only


@dataclass(frozen=True)
class NormalizedTicker:
    """BBO + last trade summary."""

    product_id: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    spread_abs: Optional[float] = None
    spread_bps: Optional[float] = None
    last_price: Optional[float] = None
    last_size: Optional[float] = None
    base_volume_24h: Optional[float] = None
    quote_volume_24h: Optional[float] = None
    freshness: Optional[FreshnessMeta] = None


@dataclass(frozen=True)
class NormalizedOrder:
    order_id: str
    client_order_id: Optional[str]
    product_id: str
    side: str
    status: str
    order_type: str
    filled_size: float
    average_filled_price: Optional[float]
    created_time: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedFill:
    fill_id: Optional[str]
    order_id: Optional[str]
    product_id: str
    side: str
    size: float
    price: float
    fee: Optional[float] = None
    trade_time: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VenueAdapter(Protocol):
    """Internal execution venue surface (spot-first)."""

    def get_product(self, product_id: str) -> tuple[Optional[NormalizedProduct], FreshnessMeta]: ...
    def get_products(self) -> tuple[list[NormalizedProduct], FreshnessMeta]: ...
    def get_best_bid_ask(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]: ...
    def get_ticker(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]: ...
    def get_recent_trades(self, product_id: str, *, limit: int = 50) -> tuple[list[dict[str, Any]], FreshnessMeta]: ...
    def list_open_orders(self, *, product_id: Optional[str] = None, limit: int = 50) -> tuple[list[NormalizedOrder], FreshnessMeta]: ...
    def get_order(self, order_id: str) -> tuple[Optional[NormalizedOrder], FreshnessMeta]: ...
    def get_fills(
        self,
        *,
        product_id: Optional[str] = None,
        limit: int = 50,
    ) -> tuple[list[NormalizedFill], FreshnessMeta]: ...
    def place_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]: ...
    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]: ...
    def cancel_order(self, order_id: str) -> dict[str, Any]: ...
    def preview_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: Optional[str] = None,
        quote_size: Optional[str] = None,
    ) -> dict[str, Any]: ...
    def get_account_snapshot(self) -> dict[str, Any]: ...
    def is_enabled(self) -> bool: ...
