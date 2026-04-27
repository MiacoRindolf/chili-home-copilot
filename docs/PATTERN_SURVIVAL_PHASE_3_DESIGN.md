# K Phase 3 design — wire `survival_probability` into decisions

**Status:** Design — no code in this doc. Spec for follow-up work.

## Background

Phase 1 (PR #49) ships feature collection. Phase 2 (PR #56) ships training. Both produce a `pattern_survival_predictions.survival_probability` per (pattern, day) under a versioned model. Today nothing reads that number. Phase 3 is the consumer side: how the score affects what the brain actually does.

## Why Phase 3 matters

Today's lifecycle is reactive — a pattern degrades, the operator sees realized loss, demotes. The classifier says "this pattern looks like it'll degrade soon" before the loss happens. Pre-emptive de-risking is the whole point of building Phase 1 + 2; without Phase 3 the predictions are just dashboard art.

## Three consumers

The brain has three places where a survival score is load-bearing. Each gets its own flag and its own decision-log entry. Operator can flip them on / off independently.

### Consumer A — entry sizing multiplier

`auto_trader._size_position` (or wherever the per-trade notional is computed after HRP) gets a final multiplier:

```
multiplier = clamp(SURVIVAL_FLOOR + (1 - SURVIVAL_FLOOR) * survival_probability, 0.25, 1.0)
sized_notional *= multiplier
```

With `SURVIVAL_FLOOR = 0.25`, a pattern at `survival_probability=0.5` gets a `multiplier = 0.625`. A pattern at `0.9` gets `0.925`. A pattern at `0.0` floors at `0.25`.

Why a multiplier rather than a hard gate: small sizing on a low-survival pattern still gives the brain feedback. Hard zero would silence the pattern and starve the next training pass of data on that pattern's regime.

**Flag:** `chili_pattern_survival_sizing_enabled` (default OFF).

**Compose with:** kill switch (Hard Rule 1, multiplier doesn't bypass), drawdown breaker (Hard Rule 2, multiplier composes after the breaker check), HRP weighting (Q1.T5, multiplier is applied to the HRP-allocated notional, not before).

**Decision-log fields:** `consumer='sizing', input_notional, output_notional, multiplier, p, threshold=null, model_version`.

### Consumer B — pre-emptive demotion gate

A scheduled pass (daily, after the snapshot job) iterates over `lifecycle in ('live','challenged')` patterns. If their latest prediction's `survival_probability < DEMOTE_THRESHOLD`, action depends on current lifecycle:

| current | next | reason |
| --- | --- | --- |
| `live`, p < 0.30 | `challenged` | first-stage warning |
| `challenged`, p < 0.20 (and consecutive 3 days) | `demoted` | sustained low survival |
| `live`, 0.30 ≤ p < 0.50 | (no change) | log as `at_risk` for operator visibility |

The `consecutive 3 days` clause prevents single-day blips (e.g. one bad regime tag) from demoting a pattern. Stored as a counter on the pattern itself or computed by querying the last 3 prediction rows.

**Flag:** `chili_pattern_survival_demote_enabled` (default OFF).

**Hard rule alignment:** demoting changes lifecycle but never blocks operator manual promotion / re-promotion. Per CLAUDE.md "Flag conflicts in frozen scopes, don't veto" — operator override always wins.

**Decision-log fields:** `consumer='demote', from_lifecycle, to_lifecycle, p, threshold, consecutive_days, model_version`.

### Consumer C — promotion gate (advisory)

Patterns moving `candidate → promoted` already pass through the CPCV promotion gate (Hard Rule 5 territory; the prediction-mirror authority is frozen, but CPCV gate is separate). The new check is **advisory**: a candidate whose first prediction has `survival_probability < PROMOTE_THRESHOLD` (e.g. 0.40) does not auto-promote even if CPCV passes; instead it lands in a "review" state that the operator clears.

A first prediction requires the pattern to have at least one feature row, which means it has been `lifecycle='candidate'` for at least one snapshot pass (~24h). That cold-start hold is acceptable.

**Flag:** `chili_pattern_survival_promote_gate_enabled` (default OFF).

**Compose with:** CPCV gate (Hard Rule 5 frozen scope; this gate runs AFTER CPCV passes and only blocks, never overrides the existing CPCV verdict). If CPCV says reject, this gate is moot. If CPCV says promote AND survival says no, the pattern goes to a new `lifecycle='review'` state.

**Decision-log fields:** `consumer='promote_gate', cpcv_verdict, p, threshold, promotion_held, model_version`.

> **`lifecycle='review'` is a new state.** The state machine today is `candidate → promoted/live → challenged → demoted`. Phase 3 adds `review` between `candidate` and `promoted`. Migrating the schema means a CHECK-constraint update and updating the lifecycle enum if there is one. **Cost:** moderate — touches every consumer that switches on lifecycle. Mitigation: ship Phase 3.A and 3.B first; defer 3.C until the model has a real track record.

## New table: `pattern_survival_decision_log`

```sql
CREATE TABLE pattern_survival_decision_log (
  id BIGSERIAL PRIMARY KEY,
  scan_pattern_id INTEGER NOT NULL,
  consumer TEXT NOT NULL,                  -- 'sizing' | 'demote' | 'promote_gate'
  predicted_survival DOUBLE PRECISION,
  threshold_used DOUBLE PRECISION,
  decision TEXT NOT NULL,                  -- 'apply' | 'no_op' | 'manual_override'
  details JSONB,                           -- consumer-specific fields above
  model_version TEXT,
  decided_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX ON pattern_survival_decision_log
  (scan_pattern_id, decided_at DESC);
CREATE INDEX ON pattern_survival_decision_log
  (consumer, decided_at DESC);
```

Migration 187 ships the table empty. First INSERT happens when consumer A's flag flips on.

## Flag map

| flag | default | role | Phase 3 sub-step |
| --- | --- | --- | --- |
| `chili_pattern_survival_decisions_enabled` | OFF | parent flag (already exists) — leave OFF until at least one sub-flag is ON | — |
| `chili_pattern_survival_sizing_enabled` | OFF | enables consumer A | 3.A |
| `chili_pattern_survival_demote_enabled` | OFF | enables consumer B | 3.B |
| `chili_pattern_survival_promote_gate_enabled` | OFF | enables consumer C | 3.C |

Why split: operator can ship sizing first (lowest blast radius — tweaks notional, doesn't change lifecycle), watch the decision-log for a week, then enable demote, then enable promote_gate. Failure mode of any one is bounded by its flag.

The parent `chili_pattern_survival_decisions_enabled` becomes redundant once sub-flags exist; keep it as a kill-switch for the whole Phase 3 surface (any sub-flag effective only if both itself AND parent are True). Flipping parent off is a one-line revert.

## Risk + rollback

### Sizing (A) — lowest risk
A bad model that predicts low survival on actually-good patterns just sizes them smaller. PnL impact is bounded by the multiplier floor (0.25x). Rollback: flip `chili_pattern_survival_sizing_enabled = False`. Sizing reverts to HRP-only on next tick. No data to roll back.

### Demote (B) — medium risk
A bad model could demote a healthy live pattern, missing trades. The `consecutive 3 days` clause smooths single-day blips. Rollback:
1. Flip `chili_pattern_survival_demote_enabled = False`
2. Manually re-promote any pattern incorrectly demoted by the gate. The `pattern_survival_decision_log` table has the audit trail of what was demoted by the gate vs. the operator vs. the existing drift_monitor.

### Promote gate (C) — highest risk
A bad model could block a CPCV-passed candidate from going live. New `lifecycle='review'` patterns sit in limbo. Rollback:
1. Flip `chili_pattern_survival_promote_gate_enabled = False`
2. Migration to map any in-flight `review` patterns back to `promoted`. (Or: leave them in `review`; the operator clears them manually, since they DID pass CPCV.)
3. Rolling back C is more involved than A or B because it adds a state. Recommend not enabling until A and B have run for at least 30 days clean.

## What gets logged where

- `[ps_decisions] consumer=sizing pattern_id=X p=0.42 mult=0.625 in=$2000 out=$1250` (INFO)
- `[ps_decisions] consumer=demote pattern_id=X from=live to=challenged p=0.28 days=3` (WARNING)
- `[ps_decisions] consumer=promote_gate pattern_id=X cpcv=pass p=0.31 held=True` (WARNING)

Plus the `pattern_survival_decision_log` row for every `apply` and every `no_op` (so the operator can audit what the gate considered, not just what it acted on).

## Activation order

Recommended once a model has been training for ≥4 weeks (so the train pass has run weekly with at least some real labels):

1. Watch the decision log in shadow mode (existing column `pattern_survival_predictions.predicted_label` is shadow data — no consumer reads it).
2. Compare predicted_label against actual_survived for the past 30 days; lift over random > 1.5x (~precision 0.6 vs base rate 0.4) is the green light.
3. Flip 3.A (sizing) only. Watch for one week. If sizing reduces realized PnL by more than 10% AND the gate's multiplier is the dominant cause (vs market regime, autotrader gating, etc), flip 3.A back off.
4. Flip 3.B (demote). Watch for two weeks. Confirm `pattern_survival_decision_log` shows `consumer='demote'` rows aligning with realized degradation (not random demotions of healthy patterns).
5. Defer 3.C (promote gate) until 3.A + 3.B have at least 30 days of clean history. The new `lifecycle='review'` state is invasive enough to want operator confidence in the model first.

## What is NOT in scope for Phase 3

- **Reinforcement-learning loop** — the model is supervised on 30d-ahead survival labels; no online updates.
- **Per-regime models** — single global model. Per-regime is a Phase 4 conversation.
- **Ensemble** — single sklearn HGB classifier. Ensembling with the existing CPCV evidence is Phase 4.
- **Calibration** — `survival_probability` from HGB is uncalibrated. Phase 3 thresholds are conservative, but a Platt-scaling step is queued for after the model has produced > 1000 labeled rows.
- **Operator UI** — decision_log table is queryable via SQL; no widget. The KPI strip already shows `learning.live_but_inactive`; adding a `learning.survival_at_risk_count` is a one-liner on the existing endpoint.

## Sequencing for the implementation work

Each row is one PR-sized chunk:

| step | what | gates | LOC est. |
| --- | --- | --- | --- |
| S.1 | Migration 187 — `pattern_survival_decision_log` table | none | ~50 |
| S.2 | Add `chili_pattern_survival_*_enabled` flags to config.py | none | ~10 |
| S.3 | `pattern_survival/decisions.py` — single `compute_decision(db, scan_pattern_id, consumer)` returning the action + recording the log row | requires S.1, S.2 | ~150 |
| S.4 | Wire consumer A (sizing) into `auto_trader.py`. Multiplier applied AFTER HRP, BEFORE final notional rounds. | requires S.3 | ~50 |
| S.5 | Daily demote-pass scheduler hook + `consumer B` logic. Uses the consecutive-day counter via querying recent prediction rows. | requires S.3 | ~80 |
| S.6 | KPI endpoint adds `learning.survival_at_risk_count` (patterns where p < 0.30) | requires S.4 or S.5 done | ~20 |
| S.7 | Migration 188 — `lifecycle='review'` state added to the lifecycle CHECK constraint (if one exists; otherwise no-op). Defer until S.4+S.5 have 30 days clean. | none | ~20 |
| S.8 | Wire consumer C (promote_gate) into the CPCV-promotion path. | requires S.7 | ~80 |

Roughly 8 small PRs, totaling ~450 LOC. Each ships independently; failure of any one only loses that consumer.

## Hard-rule alignment

- **Hard Rule 1 (kill switch)** — All three consumers respect the kill switch. The sizing multiplier is applied AFTER the kill-switch check; demote/promote are scheduled jobs that the kill switch doesn't gate (they don't trade).
- **Hard Rule 2 (drawdown breaker)** — Sizing multiplier composes with the breaker; if the breaker is tripped, no trade fires regardless.
- **Hard Rule 3 (data-first)** — `pattern_survival_decision_log` is the data-side audit; consumers compose with existing data, never replace it.
- **Hard Rule 4 (test DB)** — N/A; no test changes in this phase.
- **Hard Rule 5 (prediction mirror authority frozen)** — Phase 3 writes to NEW tables. The prediction mirror is untouched.
- **Hard Rule 6 (migrations sequential + idempotent)** — 187 + 188 follow the pattern. Both are ID-checked before merge.

## Open questions — resolved (Task T)

### Q1 — lifecycle CHECK constraint  ✅ RESOLVED

**Found:** `chk_sp_lifecycle` exists. Allowed values:

```
candidate, backtested, validated, challenged, promoted, live, decayed, retired
```

**Decision:** Phase 3 uses the existing vocabulary. NO new state.

- Consumer B (demote) goes `live → challenged → decayed` (`decayed` is the existing post-demote terminal). No CHECK constraint update needed.
- Consumer C (promote gate) does NOT use a new `lifecycle='review'` state. Instead, the candidate stays `lifecycle='candidate'` and a flag in a separate review-queue table holds it. Migration 188 (originally proposed for the lifecycle CHECK update) becomes:
  - `pattern_survival_promote_review_queue (scan_pattern_id PK, queued_at, predicted_p, cpcv_passed_at, review_decision NULL/approve/reject, review_decided_at, decided_by)`

This is a significantly cleaner design: no state-machine surgery, no risk of breaking lifecycle-stage consumers in other code paths. **Migration 188 LOC drops from ~20 to ~30 (one new table, no CHECK update).**

### Q2 — drift_monitor sequencing  ✅ RESOLVED

**Found:** `drift_monitor_service.py` and `drift_escalation_watchdog.py` produce drift severity (yellow/red) and write to `trading_pattern_drift_log`. They do NOT modify `scan_patterns.lifecycle_stage` directly (verified — no UPDATE statements touching that column anywhere in the drift-monitor module).

**Decision:** Phase 3.B can run **additively**, not sequenced. Both write to their own log tables. Operator reconciles by joining `pattern_survival_decision_log` against `trading_pattern_drift_log`. No risk of double-demotion since drift_monitor doesn't demote.

If a future operator decides drift_monitor SHOULD demote (separate PR), Phase 3.B's "consecutive 3 days" guard still smooths anything weird.

Drift_monitor flag (`brain_drift_monitor_mode`) is independent of `chili_pattern_survival_demote_enabled`. Both can be ON at the same time; their effects compose without interfering.

### Q3 — multiplier floor (0.25)  ⏸️ DEFERRED

Not actionable without ~30 days of live K Phase 1 features post-flag-flip. Ship with `chili_pattern_survival_sizing_floor=0.25` env default; tune in a follow-up after observing realized-PnL distribution.

### Q4 — streak storage  ✅ RESOLVED

**Found:** No existing `streak`/`risk` columns on `scan_patterns`.

**Decision:** Add `survival_at_risk_streak_days INTEGER NOT NULL DEFAULT 0` column to `scan_patterns` in migration 187 (alongside the decision log table). Updated by the daily demote pass: increment when today's prediction is below threshold, reset to 0 otherwise. Persisted streak is clearer for operator queries than recomputing from prediction history every check.

## Updated implementation sequencing (post-Task-T)

| step | what | gates | LOC est. |
| --- | --- | --- | --- |
| S.1 | Migration 187 — `pattern_survival_decision_log` table + `scan_patterns.survival_at_risk_streak_days` column | none | ~60 |
| S.2 | Add `chili_pattern_survival_*_enabled` flags to config.py | none | ~10 |
| S.3 | `pattern_survival/decisions.py` — single `compute_decision(db, scan_pattern_id, consumer)` returning the action + recording the log row | requires S.1, S.2 | ~150 |
| S.4 | Wire consumer A (sizing) into `auto_trader.py`. Multiplier applied AFTER HRP. | requires S.3 | ~50 |
| S.5 | Daily demote-pass scheduler hook + `consumer B` logic. Uses `survival_at_risk_streak_days` for the consecutive-day guard. Demotes `live → challenged` and `challenged → decayed`. | requires S.3, S.1 | ~100 |
| S.6 | KPI endpoint adds `learning.survival_at_risk_count` (patterns where p < 0.30) and `learning.in_promote_review_queue` count | requires S.4 or S.5 done | ~20 |
| S.7 | Migration 188 — `pattern_survival_promote_review_queue` table | none | ~30 |
| S.8 | Wire consumer C (promote_gate) into the CPCV-promotion path. Holds candidates in the review queue when CPCV passes but predicted survival < 0.40. | requires S.7 | ~80 |

Total ~500 LOC across 8 PRs (was ~450; the streak column adds a bit, the simplified S.7 saves a bit, net wash).

**Bigger win: no CHECK-constraint surgery.** The original S.7 was the riskiest step in the original plan — extending an enum CHECK on a heavily-referenced column. The new S.7 is a fresh table; nothing to break.
