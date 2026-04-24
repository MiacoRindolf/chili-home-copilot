"""Trading domain contracts (schemas shared across scanners and allocators)."""

from .signal import Horizon, Side, Signal

__all__ = ["Horizon", "Side", "Signal"]
