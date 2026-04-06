"""Stable, explicit public surface for the trading services package.

Prefer ``from app.services.trading import public_api`` (or ``public_api``) for
these symbols instead of importing underscore-prefixed helpers from
``trading_service`` / ``app.services.trading``.

Legacy ``from app.services import trading_service as ts`` remains supported;
this module is additive and does not change router response shapes.
"""
from __future__ import annotations

from .journal import weekly_performance_review

__all__ = [
    "weekly_performance_review",
]
