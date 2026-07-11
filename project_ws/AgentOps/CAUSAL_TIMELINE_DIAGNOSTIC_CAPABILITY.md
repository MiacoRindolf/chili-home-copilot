# CHILI Causal Timeline Diagnostic Capability

Date: 2026-07-11

## Purpose

Flat evidence ranking can mistake a late timeout or queue spike for the root cause. CHILI now accepts structured event and state metadata, reconstructs event-time order, identifies the earliest explicit invariant break, and marks descendants as downstream symptoms.

## Structured Evidence

Diagnostic observations may carry:

- `observed_at` and `sequence`
- `entity_id` and `event_type`
- `expected_state`, `actual_state`, `transition_from`, and `transition_to`
- `causal_parent_ids`
- `source_revision` and `runtime_revision`

The normalizer bounds every field and keeps existing evidence valid when these fields are absent.

## Deterministic Gate

`reconstruct_causal_timeline`:

1. Orders shuffled evidence by UTC event time and sequence.
2. Tracks the last state for each entity.
3. Detects expected/actual and transition-from violations.
4. Detects source/runtime revision mismatch.
5. Finds the earliest structured break.
6. Computes the transitive downstream evidence set from causal parent edges.

The evaluator prevents downstream-only evidence from confirming a root cause, blocks code attribution while source/runtime parity is unresolved, and prefers a grounded hypothesis attached to the earliest break. If this supersedes the model's requested conclusion, the result remains provisional unless typed evidence independently confirms it.

## Runtime Wiring

Every bounded typed probe now emits UTC observation time, sequence, probe entity, event type, and terminal state. This provides ordered evidence without adding shell access, runtime mutation, or premium calls.

## Validation

- Shuffled source, runtime, queue, and dependency events reconstruct in event-time order.
- A stale runtime revision is selected as the earliest break; queue saturation and provider timeout are marked downstream.
- Code attribution is blocked while source and runtime revisions differ.
- Illegal entity transitions are detected against the prior state.
- Existing diagnostic and probe focused suite: **46 passed**.
- Broad routing, evidence, repair, identity, and autonomy suite: **299 passed**.
- Disclosed eight-case heuristic regression remains **100/100**.

## Remaining Boundary

The graph currently consumes explicit metadata. Automatic correlation-id extraction from logs/traces, metrics-backend queries, container/process inspection, and cross-service lineage are not yet implemented. This capability therefore improves structured diagnosis but does not establish broad Fable 5 parity.
