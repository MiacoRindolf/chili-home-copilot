"""Neural-mesh-native crypto momentum intelligence (not learning-cycle).

Owned by brain activation + BrainNodeState; see ``pipeline`` for tick entrypoints.
"""

from .pipeline import maybe_run_momentum_neural_tick, run_momentum_neural_tick

__all__ = [
    "maybe_run_momentum_neural_tick",
    "run_momentum_neural_tick",
]
