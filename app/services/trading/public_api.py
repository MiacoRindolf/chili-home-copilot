"""Stable, explicit public surface for the trading services package.

Prefer ``from app.services.trading import public_api`` (or ``public_api``) for
these symbols instead of importing underscore-prefixed helpers from
``trading_service`` / ``app.services.trading``.

**Listed here:** prediction helpers (``learning_predictions``), journal reviews,
and learning entrypoints that own SWR / scheduler wiring (``learning``).

Legacy ``from app.services import trading_service as ts`` remains supported;
this module is additive and does not change router response shapes.
"""
from __future__ import annotations

from .journal import weekly_performance_review
from .learning import get_current_predictions, refresh_promoted_prediction_cache
from .learning_predictions import compute_prediction, predict_confidence, predict_direction

__all__ = [
    "weekly_performance_review",
    "compute_prediction",
    "predict_direction",
    "predict_confidence",
    "get_current_predictions",
    "refresh_promoted_prediction_cache",
]
