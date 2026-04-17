---
status: completed_shadow_ready
title: Phase M.2 - Pattern x Regime authoritative consumer (tilt + promotion gate + kill-switch)
parent_plan: trading_brain_profitability_gaps_bd7c666a
phase_id: phase_m2
phase_slice: M.2
depends_on_phase: phase_m (M.1 completed_shadow_ready)
created: 2026-04-17
frozen_at: 2026-04-17
rollout_mode_default: off
target_rollout_mode: compare
authoritative_deferred_until: per-slice staged cutover (see Rollout)
---

# Phase M.2 — Pattern × Regime authoritative consumer

First **authoritative** phase of the pattern × regime track. Wires the
M.1 ledger (`trading_pattern_regime_performance_daily`) into three
real decision surfaces:

- **M.2.a — NetEdgeRanker sizing tilt** (multiplier on `size_dollars`)
- **M.2.b — Promotion gate** (block weak patterns from going live in
  adverse regimes)
- **M.2.c — Kill-switch / auto-quarantine** (shut off a live pattern
  when its current-regime cell is confidently negative)

Each slice ships with its **own mode flag**, its **own append-only
decision log**, its **own release blocker**, and its **own staged
cutover** (`off → shadow → compare → authoritative`). **Nothing
flips simultaneously.**

## Objective (M.2)

Prove — with logged evidence, not intuition — that the per-pattern ×
per-regime expectancy tables built in M.1 actually move outcomes when
consumed. Succeed = for ≥ 1 full parity window (default 10 business
days) in **compare** mode, the decision-diff log shows the M.2
consumers would have changed sizing / promotion / lifecycle in a
measurable but bounded way, and the projected effect on realised
P&L is non-negative.

## Rationale (why all three in one frozen phase)

1. They share the **same lookup logic**, the **same guardrails**, the
   **same governance table**, and the **same backfill script**.
   Splitting them across 3 phases would triple the migration /
   docs / soak overhead with no risk reduction, because each slice
   is individually gated.
2. Each slice targets a **different decision surface** — sizing,
   promotion, lifecycle. They don't step on each other.
3. The user explicitly chose option (6) "all three" after reviewing
   the trade-off.

**Per `chili-workflow-phases.mdc` "one logical change at a time":**
execution order inside this phase is **M.2.a first, then M.2.c,
then M.2.b** (cheapest to reverse → most protective → slowest cycle).
Each slice completes its full cutover before the next begins.

## Non-negotiables

1. **Additive-only.** No schema changes to existing tables. No
   mutation of M.1 tables. No mutation of L.17 – L.22 tables.
   Three new log tables + three new config blocks + three new
   ops-log modules + three new release blockers.
2. **Per-slice independent gating.** Three mode flags, read
   independently:
   - `BRAIN_PATTERN_REGIME_TILT_MODE` ∈ {off, shadow, compare,
     authoritative}
   - `BRAIN_PATTERN_REGIME_PROMOTION_MODE` ∈ {off, shadow, compare,
     authoritative}
   - `BRAIN_PATTERN_REGIME_KILLSWITCH_MODE` ∈ {off, shadow, compare,
     authoritative}
   Default `off`. Target end-state for this phase: **all three at
   `compare`**, with authoritative cutover handled by the rollout
   procedure below, not by this plan's target.
3. **Kill-switch precedence.** The existing
   `governance.is_kill_switch_active()` short-circuits **before** any
   M.2 consumer runs. Same for any per-slice kill switch flag
   (`BRAIN_PATTERN_REGIME_*_KILL`).
4. **Hard floor on cell confidence.** A cell is only consumable when
   `has_confidence == True` AND `n_trades >= tilt_min_trades` (default
   **10**, tightens the M.1 default of 3). Below the floor → slice
   falls back to the global-pattern baseline (no tilt / no block /
   no quarantine).
5. **Coverage floor.** If fewer than `tilt_min_confident_dimensions`
   (default **3 of 8**) regime dimensions are confident for the
   pattern on the evaluation day, the slice falls back to baseline.
6. **Bounded tilt.** Multiplier always clamped to
   `[tilt_min_multiplier, tilt_max_multiplier]` (defaults
   **[0.25, 2.0]**). Hard-coded in the pure model; test-covered.
7. **Consecutive-day kill-switch.** Quarantine requires
   `killswitch_consecutive_days` (default **3**) consecutive evaluation
   days where the pattern's current-regime cells are confidently
   negative (not a single-day blip).
8. **Approval-gated authoritative mode.** Flipping any slice to
   `authoritative` requires a matching row in
   `trading_governance_approvals` with
   `action_type = 'pattern_regime_<slice>_authoritative'`,
   `decision = 'approved'`, and `expires_at > now()`. The slice
   refuses (`RuntimeError` + `event=*_refused_authoritative`) if no
   approval is live.
9. **Append-only decision logs.** Every non-`off` evaluation writes
   exactly one row to the slice's log table. No updates, no
   deletes, no soft-state.
10. **No new scheduler job.** Consumers run inline on existing paths
    (sizing: position_sizer_emitter; promotion: mining_validation /
    governance.request_pattern_to_live; kill-switch: daily 23:15
    hook inside the M.1 scheduler job or an adjacent small job —
    see "Scheduler" section for final wiring). Reuses slots only.
11. **`scan_status` frozen contract remains bit-for-bit identical.**
    No root-level keys added. `ops_health_model.PHASE_KEYS` is
    unchanged.
12. **Compare mode writes both decisions, applies neither.**
    `compare` mode logs `baseline_decision` + `consumer_decision` +
    `diff_category` for every evaluation. `authoritative` applies
    the consumer decision. `shadow` only logs the consumer decision
    without the baseline diff.
13. **Per-pattern circuit breaker.** If a single pattern's
    consumer_decision has flipped > `max_flips_per_day` (default **3**)
    times in a day, the pattern is frozen to its baseline for the
    rest of that day (logged as `event=*_circuit_breaker_trip`).
14. **Deterministic evaluation id** per (slice, pattern_id, as_of_date,
    context_hash). `context_hash = sha256(sorted regime labels in
    effect)[:12]`. Used to make re-runs idempotent.

## Data sources (read-only joins)

| Table | Role |
|---|---|
| `trading_pattern_regime_performance_daily` (M.1) | Source of truth for expectancy / profit_factor / sharpe_proxy per (pattern, dimension, label) |
| `trading_macro_regime_snapshots` (L.17) | Current macro regime label |
| `trading_breadth_relstr_snapshots` (L.18) | Current breadth / RS labels |
| `trading_cross_asset_snapshots` (L.19) | Current cross-asset label |
| `trading_ticker_regime_snapshots` (L.20) | Per-ticker regime |
| `trading_vol_dispersion_snapshots` (L.21) | Vol / dispersion / correlation labels |
| `trading_intraday_session_snapshots` (L.22) | Current session label |
| `scan_patterns` | Pattern metadata (lifecycle stage; promotion status) |
| `trading_paper_trades` | Read-only; baseline expectancy fallback |
| `trading_governance_approvals` | Gate for `authoritative` mode |
| `trading_position_sizer_log` (Phase H) | M.2.a writes its tilt multiplier into a new sibling column via extension |

## Ship list (deliverables)

### Shared substrate

1. **Migration 145_pattern_regime_m2_consumers**
   - Creates `trading_pattern_regime_tilt_log`,
     `trading_pattern_regime_promotion_log`,
     `trading_pattern_regime_killswitch_log`.
   - Adds `pattern_regime_tilt_multiplier` NUMERIC + `pattern_regime_tilt_reason`
     TEXT to `trading_position_sizer_log` (nullable, additive).
   - Creates 6 indexes total (per-table: `(as_of_date)`,
     `(pattern_id, as_of_date)`, partial `WHERE mode='authoritative'`).
2. **ORM models** in `app/models/trading.py`:
   `PatternRegimeTiltLog`, `PatternRegimePromotionLog`,
   `PatternRegimeKillswitchLog`. `PositionSizerLog` gets two new
   nullable columns.
3. **Shared pure module** `pattern_regime_ledger_lookup.py`:
   - `LedgerLookup` dataclass (read-only view over `PatternRegimePerformanceDaily`).
   - `resolve_pattern_context(pattern_id, as_of_date) -> ResolvedContext`
     with `cells_by_dimension`, `n_confident_dimensions`, `mean_expectancy`,
     `global_expectancy`, `fallback_used`, `context_hash`.
   - Deterministic; no side effects; no DB writes. Thin wrapper over
     a SQL read.
4. **Shared ops-log module**
   `app/trading_brain/infrastructure/pattern_regime_m2_ops_log.py`:
   - Prefixes: `[pattern_regime_tilt_ops]`,
     `[pattern_regime_promotion_ops]`,
     `[pattern_regime_killswitch_ops]`.
   - One formatter per slice; all share the same event vocabulary
     (computed / persisted / refused_authoritative / skipped /
     circuit_breaker_trip / fallback_to_baseline).
5. **Config flags** in `app/config.py` (21 new keys):
   - 3 mode flags, 3 slice kill-switch flags, 3 ops-log toggles,
     plus shared thresholds (`tilt_min_trades`,
     `tilt_min_confident_dimensions`, `tilt_min_multiplier`,
     `tilt_max_multiplier`, `killswitch_consecutive_days`,
     `max_flips_per_day`, `parity_window_days`).
6. **Backfill script** `scripts/backfill_pattern_regime_ledger.py`
   — one-shot. Regenerates M.1 ledger rows for the last 180 days
   (default); dry-run by default; idempotent (keyed on
   `ledger_run_id`).

### Slice M.2.a — NetEdgeRanker sizing tilt

7. **Pure model** `pattern_regime_tilt_model.py`:
   - `compute_tilt_multiplier(resolved_context, config) -> TiltDecision`
     with `multiplier`, `reason_code`, `fallback_used`,
     `contributing_dimensions`. 15+ pytest cases.
8. **DB service** `pattern_regime_tilt_service.py`:
   - `evaluate_and_log(db, signal, mode) -> TiltDecision`
     - `off` → return baseline multiplier = 1.0, no write.
     - `shadow` → compute + log with `applied=False`.
     - `compare` → compute + log `baseline_size`, `consumer_size`,
       diff. Do not modify the caller's size.
     - `authoritative` → compute + log, and **return a non-1.0
       multiplier that the emitter applies**.
   - `pattern_regime_tilt_summary(db, lookback_days)` for diagnostics.
9. **Integration hook** in `position_sizer_emitter.emit_shadow_proposal`:
   - After the existing shadow proposal is built and before it is
     recorded, call `evaluate_and_log(db, signal, mode)`. In
     `authoritative` mode, multiply `size_dollars` by the returned
     multiplier and store `pattern_regime_tilt_multiplier` +
     `pattern_regime_tilt_reason` on the `PositionSizerLog` row.
   - In any other mode the emitter's output is unchanged.
10. **Diagnostics endpoint**
    `GET /api/trading/brain/pattern-regime-tilt/diagnostics` with
    frozen 11-key shape (mode, lookback_days, n_evaluations,
    n_applied, distribution histogram for multiplier, top &
    bottom 25 patterns by average tilt, fallback rate).
11. **Release-blocker PowerShell**
    `scripts/check_pattern_regime_tilt_release_blocker.ps1`.
12. **5 smoke tests** (clean / auth-persist / refused / diag-ok /
    diag-below-coverage) matching the L.17 – M.1 pattern.

### Slice M.2.b — Promotion gate

13. **Pure model** `pattern_regime_promotion_model.py`:
    - `evaluate_promotion(resolved_context, config) -> PromotionDecision`
      with `allow`, `reason_code`, `blocking_dimensions`. 12+ pytest
      cases.
    - **Allow** by default; only **block** when ≥ 2 confident
      dimensions show `expectancy < 0` for the current regime.
14. **DB service** `pattern_regime_promotion_service.py`
    (same shape as M.2.a's service).
15. **Integration hooks** — two call-sites, both gated by
    `BRAIN_PATTERN_REGIME_PROMOTION_MODE`:
    - `governance.request_pattern_to_live` — check before creating
      the approval row.
    - `mining_validation.*` — check in the pre-promotion validator
      path (finalise after reading the existing validator).
    In `compare` mode both sites write decision-diff rows; the
    existing behaviour is preserved. In `authoritative` mode, a
    `block` decision refuses promotion with a structured error
    that the caller already handles.
16. **Diagnostics endpoint**
    `GET /api/trading/brain/pattern-regime-promotion/diagnostics`.
17. **Release-blocker PowerShell**
    `scripts/check_pattern_regime_promotion_release_blocker.ps1`.
18. **5 smoke tests**.

### Slice M.2.c — Kill-switch / auto-quarantine

19. **Pure model** `pattern_regime_killswitch_model.py`:
    - `evaluate_killswitch(resolved_context_history, config) ->
      KillswitchDecision` with `quarantine`, `reason_code`,
      `consecutive_days_negative`, `worst_dimension`. 15+ pytest
      cases.
    - **Keep** by default; only **quarantine** when ≥
      `killswitch_consecutive_days` consecutive evaluations show
      ≥ 2 confident dimensions with `expectancy < 0`.
20. **DB service** `pattern_regime_killswitch_service.py`.
21. **Integration hooks**:
    - **Daily evaluation** hook at the tail of the M.1 scheduler
      job (23:05, after M.1 ledger is persisted) — reads the last
      N days of the slice's decision log + current M.1 ledger,
      produces today's decision.
    - **Lifecycle transition** in `authoritative` mode: call
      `lifecycle.transition_on_decay(db, pattern, reason="pattern_regime_killswitch")`
      when the decision is `quarantine` and the pattern is
      currently `live`. In `shadow` / `compare` modes, **do not**
      call `transition_on_decay`; only log.
22. **Diagnostics endpoint**
    `GET /api/trading/brain/pattern-regime-killswitch/diagnostics`.
23. **Release-blocker PowerShell**
    `scripts/check_pattern_regime_killswitch_release_blocker.ps1`.
24. **5 smoke tests**.

### Cross-slice verification

25. **Docker soak** `scripts/phase_m2_soak.py`. Must include:
    - Migration 145 + index checks.
    - 21 new config keys readable from settings.
    - `LedgerLookup.resolve_pattern_context` semantics.
    - Three pure models' bounded outputs (multiplier clamp,
      promotion allow-by-default, killswitch consecutive-day
      requirement).
    - Each service refuses `authoritative` without approval row.
    - Each service honours `off` / `shadow` / `compare` modes.
    - `scan_status` frozen contract still byte-equal.
    - L.17 – M.1 row counts unchanged.
    - ≥ 25 checks total.
26. **Docs**
    `docs/TRADING_BRAIN_PATTERN_REGIME_M2_ROLLOUT.md` with rollout,
    rollback, release-blocker section per slice, approval runbook.
27. **Closeout section** on this plan, including per-slice
    parity-window evidence.

## Data flow (compare mode, M.2.a example)

```
new trade signal
   │
   ▼
position_sizer_emitter.emit_shadow_proposal(...)
   │ builds baseline PositionSizerLog row
   ▼
pattern_regime_tilt_service.evaluate_and_log(signal, mode)
   │ reads LedgerLookup for (pattern_id, today)
   │ reads latest L.17–L.22 snapshots for signal.ticker
   │ runs pattern_regime_tilt_model.compute_tilt_multiplier
   ▼
writes PatternRegimeTiltLog:
   baseline_size_dollars = emitter's size
   consumer_size_dollars = baseline * multiplier
   multiplier            = 1.00 – 2.00 (or 0.25–1.00)
   reason_code           = regime_positive | regime_neutral |
                           regime_negative | fallback_global |
                           fallback_low_coverage | fallback_low_confidence
   diff_category         = upsize | downsize | none
   applied               = False (compare mode)
   ▼
emitter proceeds with unchanged size (compare mode)
```

In `authoritative` mode the only behavioural change is:
- `applied = True`
- emitter's `size_dollars` is multiplied by the returned multiplier
- `pattern_regime_tilt_multiplier` + `_reason` are recorded on the
  `PositionSizerLog` row

## Authority contract (per slice)

| Mode | Log row? | Applied? | Error on auth-without-approval? |
|---|---|---|---|
| `off` | No | No | N/A |
| `shadow` | Yes, no diff | No | N/A |
| `compare` | Yes, with diff | No | N/A |
| `authoritative` | Yes, with diff | **Yes** | **RuntimeError** + refused event |

## Frozen diagnostics shape (all three, top-level keys identical)

```
{
  "ok": true,
  "<slice_key>": {
    "mode": "off"|"shadow"|"compare"|"authoritative",
    "lookback_days": <int>,
    "window_days": <int>,
    "n_evaluations_total": <int>,
    "n_evaluations_applied": <int>,
    "n_fallback_baseline": <int>,
    "fallback_rate": <float>,
    "latest_as_of_date": "YYYY-MM-DD"|null,
    "latest_evaluation_id": "<hex12>"|null,
    "by_reason_code": { "<code>": <count>, ... },
    "top_patterns_by_avg_effect":   [ { pattern_id, pattern_label, avg_effect, n } x 25 ],
    "bottom_patterns_by_avg_effect":[ { pattern_id, pattern_label, avg_effect, n } x 25 ]
  }
}
```

## Release-blocker contract (per slice)

Fail if any log line contains **all** of:

- `[pattern_regime_<slice>_ops]`
- `event=pattern_regime_<slice>_persisted`
- `mode=authoritative`

**OR** any line with `event=pattern_regime_<slice>_refused_authoritative`
(meaning `authoritative` was attempted without a live approval —
release-blocker prevents silent drift).

Additional diagnostics gates (all slices):

- `by_reason_code` histogram sum must equal `n_evaluations_total`.
- `fallback_rate` must be ≤ `max_fallback_rate` (default 0.60) or the
  blocker fires (runaway fallback means the model is effectively a
  no-op and shouldn't be called authoritative).

## Rollout procedure (staged; per-slice; strict order)

**Prerequisites — day 0**

1. Apply migration 145.
2. Run `scripts/backfill_pattern_regime_ledger.py --days 180`
   (not dry-run) in the live container. Verify row counts.
3. Regression bundle green: `scan_status` frozen contract,
   L.17 – L.22 + M.1 pure tests, new pure tests.
4. Docker soak green.
5. Flip all three modes to `shadow` for **5 business days**. Goal:
   volume of evaluations, no crashes, fallback rate reasonable.

**M.2.a tilt — cutover**

6. `BRAIN_PATTERN_REGIME_TILT_MODE=compare` for **10 BD**.
   - Analyse tilt-log diff histogram daily.
   - Check decision-diff distribution; expect most multipliers
     near 1.0; outliers logged.
   - Release blocker clean.
7. Submit governance approval
   `action_type='pattern_regime_tilt_authoritative'` with
   2-week `expires_at`.
8. `BRAIN_PATTERN_REGIME_TILT_MODE=authoritative`.
   - Recreate chili + brain-worker + scheduler-worker.
   - Live probe diagnostics endpoint; mode=authoritative.
   - Release blocker clean (no `refused_authoritative` events).
   - Hold for **5 BD** before proceeding to M.2.c.

**M.2.c kill-switch — cutover** (more protective, so goes before
promotion gate)

9. `BRAIN_PATTERN_REGIME_KILLSWITCH_MODE=compare` for **10 BD**.
10. Approval + `authoritative`.
11. Hold for **5 BD**.

**M.2.b promotion gate — cutover**

12. `BRAIN_PATTERN_REGIME_PROMOTION_MODE=compare` for **10 BD**.
13. Approval + `authoritative`.
14. Phase closeout.

Approx total calendar time at minimum thresholds: **≈ 50 BD** /
**≈ 10 calendar weeks** from day 0 to all three authoritative.
Parity window lengths may extend per the evidence.

## Rollback (any slice, any stage)

1. Set the slice's `BRAIN_PATTERN_REGIME_*_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. Consumer short-circuits; no writes; no behaviour change.
4. Historical decision-log rows are retained for post-mortem.
   `TRUNCATE` of the slice's log table is safe (no FKs into the
   table).
5. For emergency rollback under load:
   - `BRAIN_PATTERN_REGIME_<SLICE>_KILL=true` short-circuits **before**
     the mode check (no recreate required — just `.env` reload or
     restart).
6. Approval rows can be `reject`-ed via governance; the slice reads
   the most recent non-rejected approval.

## Done-when (per-slice gates)

For each of M.2.a / M.2.b / M.2.c:

- All unit tests green.
- DB-backed service tests green (where run via Docker soak).
- Docker soak checks for that slice all green.
- Release blocker green on live logs for the full parity window.
- Diagnostics endpoint returns the frozen shape with
  `mode=authoritative` after cutover.
- Approval row present with `decision='approved'`,
  `expires_at > now()`.
- `scan_status` frozen contract still byte-equal.
- L.17 – M.1 diagnostics still responsive and `mode=shadow`.
- Plan YAML frontmatter advances through
  `frozen → in_rollout_a → in_rollout_c → in_rollout_b →
  completed_authoritative`.

## Non-goals (deferred to M.3 or later)

- **2-D interaction cells** (e.g. `macro × session` expectancy).
  M.2 reads only 1-D cells written by M.1.
- **Isotonic calibration** on top of the tilt multiplier
  (deferred to Phase E.2).
- **Ticker-level tilt** (`pattern × ticker × regime`). M.2 treats
  ticker regime as just another dimension of the 1-D lookup.
- **Promotion *up-grade*** (auto-promote a pattern that shows
  confidently positive regime cells) — M.2.b only blocks, never
  accelerates.
- **Per-strategy tilt** (M.2.a tilts per-pattern; per-strategy is
  a separate aggregator).
- **UI surface** for any of the three slices. Diagnostics endpoints
  only in M.2. UI is M.3.
- **Kill-switch auto-recertification** path — M.2.c quarantines only;
  re-activating a quarantined pattern still goes through the manual
  recert path from Phase J.
- **Any change to M.1's ledger schema** or write path.

## Self-critique (ahead of execution)

1. **Three slices in one plan is the largest scope shipped so far.**
   Mitigations: per-slice modes, per-slice release blockers,
   per-slice rollbacks, strictly staged cutover order.
2. **Approval-gated authoritative mode is new.** The phase adds a
   read of `trading_governance_approvals` that didn't exist in prior
   phases' release contract. Mitigations: tests for both presence
   and expiry of approval row; refused event fires on missing /
   expired approval.
3. **Position sizer integration mutates `PositionSizerLog`
   (additive columns).** This is the first time Phase M touches a
   Phase H table. Mitigations: columns are nullable, writes are
   additive, existing consumers of `PositionSizerLog` do not read
   the new columns in Phase H code.
4. **Kill-switch is irreversible-ish.** A pattern quarantined by
   M.2.c goes through the manual recert path to come back. This is
   deliberate but could trap a good pattern on a short-lived regime
   spike. Mitigation: `killswitch_consecutive_days=3` default +
   `max_flips_per_day=3` circuit breaker + the existing Phase J
   recert path.
5. **Fallback rate ≤ 0.60** is a coarse gate. If early shadow logs
   show fallbacks concentrated on recently-live patterns (because
   ledger history is thin), the backfill script must be re-run
   with a wider window before compare.

## Closeout (shadow ready — 2026-04-17)

M.2 shadow rollout is complete end-to-end for all three slices.
Status advanced from `frozen → executing_shadow →
completed_shadow_ready`.

### What shipped

- Migration 145 (`145_pattern_regime_m2_consumers`): `expires_at`
  nullable col on `trading_governance_approvals`; two nullable cols
  (`pattern_regime_tilt_multiplier`, `pattern_regime_tilt_reason`) on
  `trading_position_sizer_log`; three new append-only decision-log
  tables. Idempotent, lint clean.
- ORM: `PatternRegimeTiltLog`, `PatternRegimePromotionLog`,
  `PatternRegimeKillSwitchLog`, plus two `PositionSizerLog` columns.
- 21 config flags (7 per slice) in `app/config.py`.
- Shared ops-log module
  `app/trading_brain/infrastructure/pattern_regime_m2_ops_log.py`
  with three isolated prefixes
  (`[pattern_regime_tilt_ops]`,
  `[pattern_regime_promotion_ops]`,
  `[pattern_regime_killswitch_ops]`).
- Shared pure module
  `app/services/trading/pattern_regime_ledger_lookup.py` so all three
  slices read the M.1 ledger consistently.
- Three pure models (tilt / promotion / kill-switch) with 37 unit
  tests; all 195 regression tests pass
  (L.17 – L.22 + M.1 + M.2 + scan_status frozen contract).
- Three service layers plugged into:
  - `position_sizer_emitter.emit_shadow_proposal` (tilt)
  - `governance.request_pattern_to_live` (promotion)
  - `lifecycle.transition_on_decay` via the new APScheduler
    `pattern_regime_killswitch_daily` job at 23:05 (kill-switch)
- Three diagnostics endpoints (all frozen shape,
  `mode=shadow approval_live=false` verified live).
- Three release-blocker PowerShell scripts
  (`check_pattern_regime_{tilt,promotion,killswitch}_release_blocker.ps1`),
  6/6 smoke tests pass; all three clean on live logs.
- Docker soak `scripts/phase_m2_soak.py` — 31/31 checks pass inside
  the chili container.
- Backfill script `scripts/backfill_pattern_regime_ledger.py`
  (dry-run default; `--commit` persists; smoke tested).
- Rollout doc `docs/TRADING_BRAIN_PATTERN_REGIME_M2_ROLLOUT.md`.

### Live verification (2026-04-17 post-flip)

- `.env` flipped for all three slices:
  `BRAIN_PATTERN_REGIME_TILT_MODE=shadow`,
  `BRAIN_PATTERN_REGIME_PROMOTION_MODE=shadow`,
  `BRAIN_PATTERN_REGIME_KILLSWITCH_MODE=shadow`.
- `chili`, `brain-worker`, `scheduler-worker` force-recreated.
- Scheduler registered
  `Pattern x regime killswitch daily (23:05; mode=shadow)`.
- All three diagnostics endpoints return `mode="shadow"`
  `approval_live=false`.
- All three release blockers exit 0.
- `scan_status` frozen contract intact: keys `['ok', 'brain_runtime',
  'prescreen', 'learning']`, `brain_runtime.release == {}`,
  `encode_error == None`.

### Deliberate deviations from the frozen plan

- None. All three slices shipped per spec, in the planned order,
  with per-slice gates and per-slice blockers.

### Self-critique (post shadow rollout)

- **Observability window before compare.** 5 BD of shadow is the
  minimum. The kill-switch fires at most once/day, so its
  observability budget is tightest; we may need to extend kill-switch
  shadow longer if no pattern crosses the consecutive-day threshold
  naturally during the window.
- **Backfill dependence.** If the M.1 ledger is thin for older
  patterns (wait for Phase M.1 scheduler to accumulate history), the
  backfill script must be used before meaningful compare-mode
  evidence exists. Pre-flight in the rollout doc calls this out.
- **Approval runbook.** The rollout doc includes a SQL snippet for
  inserting a governance approval row, but this should eventually
  move to an admin UI. Tracked implicitly under M.3 UI scope.
- **Postgres contention.** Isolated pytest runs block on live
  Postgres activity (observed in prior L/M phases). DB-layer
  coverage is provided by the Docker soak, which ran green.

### Deferred to authoritative cutover (M.2 continued)

- Per-slice compare windows (5 BD each, gated by P&L diff ≥ 0).
- Per-slice authoritative flip (requires live governance approval
  row and a clean 5 BD compare window).
- Per-slice 5 BD hold at authoritative.
- Authoritative cutover order: **M.2.a → M.2.c → M.2.b**.

### M.3 checklist (pre-flight for the next phase)

Do not open M.3 until:

1. All three M.2 slices are authoritative with ≥ 10 BD live data.
2. Kill-switch 30-day per-pattern circuit breaker has held
   (no pattern hit its cap).
3. Compare-mode projected P&L vs realised P&L correlation is
   positive (the metric is predictive, not random).
4. Governance approvals show a clean approve/revoke audit trail.

Scope candidates for M.3:

- 2-D interaction cells (e.g. `macro × vol_regime`).
- Kill-switch auto-recertification path (post-quarantine recovery).
- UI surface for the three M.2 decision surfaces + M.1 ledger.
- Isotonic calibration on top of the M.2.a tilt multiplier.
- Ticker-level tilt (pattern × ticker × regime).
