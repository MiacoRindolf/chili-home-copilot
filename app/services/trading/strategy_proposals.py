"""Strategy Proposals: predictions, position sizing, confidence scoring.

Re-exports proposal/prediction functions from learning.py to establish a
cleaner module boundary for the strategy proposal subsystem.
"""
from __future__ import annotations

from .learning import (
    get_current_predictions,
    backfill_predicted_scores,
    tune_position_sizing,
    learn_exit_optimization,
)
from .learning_predictions import compute_prediction, predict_confidence, predict_direction

__all__ = [
    "compute_prediction",
    "predict_direction",
    "predict_confidence",
    "get_current_predictions",
    "backfill_predicted_scores",
    "tune_position_sizing",
    "learn_exit_optimization",
]
