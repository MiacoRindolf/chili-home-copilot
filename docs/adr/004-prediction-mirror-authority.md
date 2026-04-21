# 004 — Prediction-mirror authority migration

**Status:** Accepted — hardened 2026-Q1, frozen through Phase 7+.
**Authors:** trading brain team.
**Supersedes:** none. **Superseded by:** none.

## Context

Before this work, `app/services/trading/learning.py::_get_current_predictions_impl` was the only source of prediction data. It held results in the in-memory `_learning_status` dict and served them directly to routers. Two problems pushed us off that floor:

1. **No durable record.** A crash mid-cycle erased predictions; the UI had no authoritative history it could replay, and the learning engine had no way to diff "what did we predict yesterday" against "what did we predict today" without re-running the entire cycle.
2. **No independent consumer surface.** Every downstream (autopilot, ranker, recert queue) read `_learning_status` directly, which meant any change to the return shape was a global change with no flag-level ramp.

We needed two things simultaneously:

- A durable mirror of predictions in the DB (append-only, fingerprintable).
- A progressive rollout path from "legacy is authoritative" → "mirror is authoritative" without rewriting every consumer.

## Decision

Adopt an **8-phase migration** with feature flags at each step and a **frozen authority contract** from Phase 7 onward. Phases are cumulative: each turns on new surface area that's consumed only when its own flag is enabled.

### The 8 phases

| Phase | Scope | Feature flag(s) |
|---|---|---|
| 1 | Models, ports, stage catalog (code presence only) | none |
| 2 | Shadow mirror of learning-cycle + stage-job rows during `run_learning_cycle` | `brain_cycle_shadow_write_enabled`, `brain_status_dual_read_enabled`, `brain_lease_shadow_write_enabled` |
| 3 | Single-flight lease admission via `brain_cycle_lease` (dedicated session) | `brain_cycle_lease_enforcement_enabled` |
| 4 | Append-only dual-write of predictions into `brain_prediction_snapshot` + `brain_prediction_line` | `brain_prediction_dual_write_enabled` |
| 5 | Read-side compare of legacy vs mirror + candidate-authoritative read when explicit tickers | `brain_prediction_read_compare_enabled`, `brain_prediction_read_authoritative_enabled`, `brain_prediction_read_max_age_seconds` |
| 6 | One-line INFO ops log per prediction path (`[chili_prediction_ops]`) | `brain_prediction_ops_log_enabled` |
| 7 | **Authority hardening** — reject implicit-universe reads from the authoritative path | (no new flag; logic gate) |
| 8 | Rollout playbook + release-blocking grep | none (docs + script) |

Flags default **OFF**; deployments enable progressively per `docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md`.

### The authority contract (frozen)

Three rules, enforced by the Phase 7 gate in `_get_current_predictions_impl`:

1. **Legacy is authoritative for the `tickers=None` / empty-list / implicit-universe paths.** The mirror is never the source of truth for a prediction the caller didn't explicitly ask for.
2. **Mirror is candidate-authoritative for non-empty explicit-ticker API requests**, when:
   - Phase 5 read flags are on,
   - The snapshot's `as_of_ts` is within `brain_prediction_read_max_age_seconds` (default 900s), AND
   - Full index-aligned parity against the legacy list passes.
3. **Every outcome emits `[chili_prediction_ops]`** with fields `dual_write`, `read`, `explicit_api_tickers`, `fp16`, `snapshot_id`, `line_count`. The exact byte format is frozen; alert rules + `scripts/check_chili_prediction_ops_release_blocker.ps1` pattern-match on it.

### The release blocker

A ship is **blocked** if any `[chili_prediction_ops]` log line observed during soak matches **both** `read=auth_mirror` AND `explicit_api_tickers=false`. That combination proves the authority contract is leaking — the mirror is being trusted for a request the caller never asked for in explicit form. Fix at the source; no retry, no override.

## Consequences

### Wins

- **Durable audit.** Every returned prediction line is a DB row with a fingerprint we can replay.
- **Progressive ramp.** The same codebase runs in every environment; the rollout is flag-driven, not branch-driven.
- **Provable safety.** The Phase-7 gate is a binary property: either the log line is clean or it isn't. No "the authority accidentally expanded over the last sprint" debate.
- **Rollback is fast.** `scripts/rollback-prediction-mirror.ps1` (Phase A tech-debt) flips all flags off + recreates containers in one command.

### Costs

- **Flag surface complexity.** Six flags cover phases 2-6; operators must read `TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md` before flipping.
- **Frozen log-line format.** Adding a new field to `[chili_prediction_ops]` now requires a new phase with soak. The 15-minute wall between "I want to add observability" and "I can ship it" is real.
- **Two sources of truth during phases 4-6.** The mirror is populated but legacy is authoritative; a subtle bug in the dual-write path only surfaces when compare-mode flags flip mismatches into WARNINGs. This is why parity tests are sacred (Phase-C risk watch-out).

### What this ADR does NOT do

- Does not retire legacy reads. Phase 5's authoritative mode falls back to legacy on any staleness / parity / implicit-universe case. Retirement is a future phase.
- Does not govern the non-prediction surfaces under `app/trading_brain/` (learning cycle, stage jobs, lease). Those have their own rollout discipline but are not frozen.
- Does not introduce ML model changes. The mirror captures the output of an unchanged legacy compute path; that's deliberate (compare-mode parity would otherwise be meaningless).

## References

- [`app/trading_brain/README.md`](../../app/trading_brain/README.md) — phase tracker + flag defaults
- [`docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md`](../TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md) — per-environment rollout procedure
- [`docs/PHASE_ROLLBACK_RUNBOOK.md`](../PHASE_ROLLBACK_RUNBOOK.md) — rollback incident playbook
- [`scripts/check_chili_prediction_ops_release_blocker.ps1`](../../scripts/check_chili_prediction_ops_release_blocker.ps1) — release-blocker grep
- [`scripts/rollback-prediction-mirror.ps1`](../../scripts/rollback-prediction-mirror.ps1) — automated rollback
- [`CLAUDE.md`](../../CLAUDE.md) — Hard Rule 5 ("prediction mirror authority is frozen")
- [`.cursor/plans/trading-brain-canonical-blueprint.md`](../../.cursor/plans/trading-brain-canonical-blueprint.md) — original blueprint

## How to modify this ADR

Don't edit; supersede. If a future phase changes authority (e.g. Phase 9 retires legacy reads), write ADR 005 that names this ADR in its `Supersedes` header, and link back from here under `Superseded by`.
