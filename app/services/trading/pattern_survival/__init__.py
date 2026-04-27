"""Q2 Task K — pattern-survival meta-classifier package.

Three-phase rollout:

  Phase 1 (this commit) — feature collection only.
    * ``features.snapshot_pattern_features`` collects per-pattern
      point-in-time features and writes ``pattern_survival_features``.
    * Daily scheduler job (``run_pattern_survival_snapshot_job``) iterates
      over live + challenged patterns, snapshots each.
    * No model training, no decisions wired anywhere.

  Phase 2 (separate task) — train.
    * Backfill 30-day labels onto features that have aged out.
    * Train LightGBM (or scikit-learn baseline as fallback) on the
      labeled feature table.
    * Score new daily snapshots; write ``pattern_survival_predictions``.

  Phase 3 (separate task) — wire decisions.
    * When ``chili_pattern_survival_decisions_enabled`` flips ON, the
      demotion / sizing path consults the latest survival probability
      and de-risks pre-emptively for low-survival patterns.

Flag-gated by ``chili_pattern_survival_classifier_enabled`` (default OFF).
The reads on prediction tables work regardless; only writes are gated.
"""

from .features import (
    snapshot_pattern_features,
    run_pattern_survival_snapshot_job,
)

__all__ = [
    "snapshot_pattern_features",
    "run_pattern_survival_snapshot_job",
]
