"""Backward-compatibility shim.

The trading module has been split into focused sub-modules under
``app/services/trading/``. This file re-exports everything so that
existing ``from ..services import trading_service as ts`` imports
continue to work.
"""
from .trading import *  # noqa: F401, F403
from .trading import signal_shutdown  # noqa: F811 — explicit re-export
