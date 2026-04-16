"""Trading Brain: Postgres-backed event-driven neural mesh (topology + activations)."""

from __future__ import annotations

from .schema import (
    DEFAULT_DOMAIN,
    DEFAULT_GRAPH_VERSION,
    LOG_PREFIX,
    effective_graph_mode,
)
from .activation_runner import run_activation_batch
from .projection import build_neural_graph_projection

# Register built-in fire handlers on first import.
try:
    from .action_handlers import register_builtin_handlers as _register
    _register()
except Exception:
    import logging
    logging.getLogger(__name__).debug("built-in handler registration deferred", exc_info=True)

__all__ = [
    "DEFAULT_DOMAIN",
    "DEFAULT_GRAPH_VERSION",
    "LOG_PREFIX",
    "effective_graph_mode",
    "run_activation_batch",
    "build_neural_graph_projection",
]
