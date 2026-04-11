"""Shared market-profile helpers for Autopilot symbols."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


_NY_TZ = ZoneInfo("America/New_York")


def asset_class_for_symbol(symbol: str | None) -> str:
    sym = (symbol or "").strip().upper()
    return "crypto" if sym.endswith("-USD") else "stock"


def is_coinbase_spot_symbol(symbol: str | None) -> bool:
    sym = (symbol or "").strip().upper()
    if not sym.endswith("-USD"):
        return False
    base = sym[:-4]
    return bool(base) and base.isalnum()


def market_open_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    if asset_class_for_symbol(symbol) == "crypto":
        return True
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    if local.weekday() >= 5:
        return False
    minute_of_day = local.hour * 60 + local.minute
    return 570 <= minute_of_day < 960
