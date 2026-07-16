"""Fail-neutral short-mechanics adapter for momentum squeeze features."""

from __future__ import annotations

from typing import Any


def get_short_mechanics(symbol: str | None) -> dict[str, Any]:
    """Return borrow/short-interest mechanics when a provider is wired.

    Current fallback is intentionally empty: squeeze/Kelly callers treat missing
    mechanics as neutral, so this cannot size up or block a trade by itself.
    """
    _ = symbol
    return {}
