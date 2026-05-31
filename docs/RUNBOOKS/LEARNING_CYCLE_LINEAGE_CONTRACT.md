# Learning-Cycle Lineage Contract

This runbook defines the MLOps contract for CHILI brain-worker learning-cycle
diagnostics. It is intentionally read-only and does not authorize schema
changes, model promotion, worker restarts, broker actions, breaker changes, or
live-trading behavior changes.

## Problem

Brain-worker heartbeat and event-first activity can be current while durable
learning-cycle lineage is absent. In that state, `brain_worker_control` proves a
process is alive, but it does not prove that a reproducible learning-cycle run,
stage, input artifact, output artifact, or model evidence chain exists.

`learning_live_json` is therefore a volatile UI/progress signal unless it points
to persisted lineage.

## Required Classification

Diagnostics should distinguish these states:

- `WORKER_ACTIVITY_NOT_OBSERVED`: no worker heartbeat or recent worker-event
  activity was observed by the diagnostic.
- `WORKER_ACTIVITY_WITHOUT_DURABLE_LEARNING_CYCLE_LINEAGE`: worker heartbeat or
  event activity is current, but no recent persisted `learning_cycle` ledger row
  and no `brain_learning_cycle_run` lineage are observed.
- `WORKER_ACTIVITY_WITH_LINEAGE_PARTIAL_OR_PRESENT`: worker heartbeat or recent
  worker-event activity is observed and
  at least one durable lineage surface is populated; reviewers must still inspect
  whether stage/artifact coverage is complete.

## Durable Lineage Requirement

Every visible learning cycle must do one of the following:

- Persist a `brain_learning_cycle_run` row with a stable correlation/run ID.
- Persist `brain_stage_job` rows for each major stage with input and output
  artifact references where artifacts exist.
- Link `brain_worker_control.learning_live_json` to the durable run ID.

If the visible progress state is intentionally not persisted, it must explicitly
mark itself as volatile/unpersisted so dashboards and downstream evidence
consumers cannot confuse liveness with reproducibility.

## Read-Only Diagnostic Surfaces

The diagnostic should report:

- `brain_worker_control` heartbeat age, wake/stop state, and safe prefixes of
  live/digest JSON.
- Legacy `brain_batch_jobs` `job_type='learning_cycle'` totals, recent count,
  running count, and latest start/end times.
- Recent brain/pattern/neural job activity as separate liveness evidence.
- Durable lineage table presence and freshness:
  - `brain_learning_cycle_run`
  - `brain_stage_job`
  - `brain_cycle_lease`
  - `brain_prediction_snapshot`
  - `brain_prediction_line`

## Pattern-Survival Freshness SLO Proposal

Pattern-survival evidence should not be treated as current ML evidence just
because decision logs exist. DS should review and approve an SLO derived from
observable pipeline cadence:

- Freshness should be measured separately for feature snapshots, predictions,
  decision logs, and model artifact metadata.
- The allowed age should derive from the configured refresh cadence and the
  observed successful refresh cadence, not from an unexplained constant.
- Survival predictions should be classified as stale if the newest prediction
  predates the newest feature cohort it is expected to score.
- Survival features should be classified as stale if they predate the latest
  material pattern/backtest cohort used by promotion or recert consumers.
- Any consumer that uses survival output for promotion, sizing, gating, or
  reporting should surface the freshness classification and fail closed or
  downgrade confidence until DS signs off on the SLO.

## Safety Boundary

- Production probes must be read-only and rollback-clean.
- Tests that mutate DB state must use a `_test` suffixed database or mocks.
- This contract does not authorize production DB mutation, migration execution,
  worker restart, broker interaction, breaker reset, kill-switch change,
  feature-flag flip, model promotion, deployment, capital allocation, or
  live-trading behavior change.
