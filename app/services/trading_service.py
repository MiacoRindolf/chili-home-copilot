"""Backward-compatibility shim.

The trading module has been split into focused sub-modules under
``app/services/trading/``. This file re-exports everything so that
existing ``from ..services import trading_service as ts`` imports
continue to work.
"""
from .trading import *  # noqa: F401, F403
from .trading import signal_shutdown  # noqa: F811 — explicit re-export
from .trading.market_data import _clamp_period  # noqa: F401 — star-import omits leading _

# Private helper used by Smart Pick streaming endpoint.
# Explicitly re-exported so callers can use ts._build_smart_pick_context_strings(...)
from .trading.scanner import (  # type: ignore[F401]
    _build_smart_pick_context_strings,
)
