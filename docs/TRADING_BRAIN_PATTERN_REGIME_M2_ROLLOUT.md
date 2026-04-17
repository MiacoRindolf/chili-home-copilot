# Phase M.2 — Pattern × Regime Authoritative Consumer Rollout

## Scope

Phase M.2 wires the M.1 ledger
(`trading_pattern_regime_performance_daily`) into three **independently-
gated** decision surfaces:

| Slice | Decision surface | Mode flag | Authoritative side-effect |
|-------|------------------|-----------|---------------------------|
| M.2.a | NetEdgeRanker sizing tilt (multiplier on proposed notional) | `BRAIN_PATTERN_REGIME_TILT_MODE` | Scales `proposed_notional` by `[min_mult, max_mult]` |
| M.2.b | Promotion gate (block auto-approve to live in adverse regimes) | `BRAIN_PATTERN_REGIME_PROMOTION_MODE` | Converts a baseline-allow into a block (never upgrades) |
| M.2.c | Kill-switch / auto-quarantine (pattern lifecycle decay) | `BRAIN_PATTERN_REGIME_KILLSWITCH_MODE` | Calls `lifecycle.transition_on_decay` to quarantine a live pattern |

Each slice has its **own** mode flag, its **own** append-only decision
log, its **own** release blocker, and its **own** staged cutover
(`off → shadow → compare → authoritative`). Nothing flips
simultaneously.

## Authority contract

- All three slices default to `off`. Shipping enables them for **shadow**
  observation only.
- `compare` mode: consumers compute a decision, write it to their
  decision-log table, but **do not** take effect. Baseline wins.
- `authoritative` mode: consumer decision is applied. **Gated by an
  unexpired row in `trading_governance_approvals` of the matching
  decision type** (`approval_live=true`). Absent approval, the service
  emits `event=*_refused_authoritative` and falls back to baseline.
- The promotion gate **never** upgrades a baseline-block to allow — it
  can only block an allow. This is a one-way gate by design.

## Ship list

1. **Migration 145** (`145_pattern_regime_m2_consumers`) — adds
   `expires_at` to `trading_governance_approvals`; adds
   `pattern_regime_tilt_multiplier` + `pattern_regime_tilt_reason` to
   `trading_position_sizer_log`; creates three append-only decision
   tables: `trading_pattern_regime_tilt_log`,
   `trading_pattern_regime_promotion_log`,
   `trading_pattern_regime_killswitch_log`.
2. **ORM models** `PatternRegimeTiltLog`, `PatternRegimePromotionLog`,
   `PatternRegimeKillSwitchLog` in `app/models/trading.py`, plus two
   new nullable columns on `PositionSizerLog`.
3. **Shared pure module**
   `app/services/trading/pattern_regime_ledger_lookup.py` —
   `LedgerCell`, `ResolvedContext`, `load_resolved_context`,
   `resolved_context_hash`, `summarise_context`. Single place that
   reads the M.1 ledger so all three slices see the same data.
4. **Pure models** (all three decision-free):
   - `pattern_regime_tilt_model.py` — `TiltConfig`, `TiltDecision`,
     `compute_tilt_multiplier`
   - `pattern_regime_promotion_model.py` — `PromotionConfig`,
     `PromotionDecision`, `evaluate_promotion`
   - `pattern_regime_killswitch_model.py` — `KillSwitchConfig`,
     `DailyExpectancyPoint`, `KillSwitchDecision`,
     `compute_consecutive_streak`, `evaluate_killswitch`
5. **Shared common service** `pattern_regime_m2_common.py` —
   `normalize_mode`, `mode_is_active`, `mode_is_authoritative`,
   `has_live_approval` (authoritative gate), `make_evaluation_id`
   (deterministic per-slice evaluation ids).
6. **Ops log** `pattern_regime_m2_ops_log.py` with three distinct
   prefixes (`[pattern_regime_tilt_ops]`,
   `[pattern_regime_promotion_ops]`, `[pattern_regime_killswitch_ops]`)
   so each slice has an isolated release blocker.
7. **Service layers** (one per slice, all additive):
   - `pattern_regime_tilt_service.py` — wired into
     `position_sizer_emitter.emit_shadow_proposal` after baseline
     proposal is written. Shadow: log only. Authoritative (with
     approval): scale `proposed_notional`.
   - `pattern_regime_promotion_service.py` — wired into
     `governance.request_pattern_to_live`. Never raises; in
     authoritative mode can convert a baseline-allow to a block and
     emits `pattern_regime_promotion` block reason.
   - `pattern_regime_killswitch_service.py` — daily sweep registered
     via APScheduler; calls `lifecycle.transition_on_decay` when all
     consecutive-day, confidence, and 30-day per-pattern circuit
     breaker gates pass.
8. **APScheduler job** `pattern_regime_killswitch_daily` at
   configurable cron (default **23:05**), gated by
   `BRAIN_PATTERN_REGIME_KILLSWITCH_MODE`.
9. **Diagnostics endpoints** (all frozen-shape):
   - `GET /api/trading/brain/pattern-regime-tilt/diagnostics`
   - `GET /api/trading/brain/pattern-regime-promotion/diagnostics`
   - `GET /api/trading/brain/pattern-regime-killswitch/diagnostics`
10. **Release blockers** (one per slice):
    - `scripts/check_pattern_regime_tilt_release_blocker.ps1`
    - `scripts/check_pattern_regime_promotion_release_blocker.ps1`
    - `scripts/check_pattern_regime_killswitch_release_blocker.ps1`
11. **Backfill script** `scripts/backfill_pattern_regime_ledger.py`
    (dry-run default; `--commit` to persist) — replays M.1
    `compute_and_persist` across a historical `as_of_date` range so
    M.2 consumers have a non-empty ledger to read.
12. **Docker soak** `scripts/phase_m2_soak.py` — 31 checks covering
    migration, tables, columns, settings, ops log, mode helpers,
    evaluation-id determinism, live approval gate, all three pure
    models, service-layer off-mode short-circuits, authoritative-
    refusal path, diagnostics shape, and M.1 additive-only checks.

## Frozen authority / ops contracts

### Release-blocking log patterns (per slice)

A log line that contains **all** of the following triggers release
block per slice:

**M.2.a tilt:**

```
[pattern_regime_tilt_ops] event=tilt_applied mode=authoritative approval_live=false
[pattern_regime_tilt_ops] event=tilt_refused_authoritative ...
```

**M.2.b promotion:**

```
[pattern_regime_promotion_ops] event=promotion_applied mode=authoritative approval_live=false
[pattern_regime_promotion_ops] event=promotion_refused_authoritative ...
```

**M.2.c kill-switch:**

```
[pattern_regime_killswitch_ops] event=killswitch_applied mode=authoritative approval_live=false
[pattern_regime_killswitch_ops] event=killswitch_refused_authoritative ...
```

The release blockers (PowerShell scripts) check both that no
`*_applied mode=authoritative approval_live=false` line exists, and
that the `*_refused_authoritative` event count is zero. Any match
returns exit code 1.

### Governance-approval contract

Each slice consults `trading_governance_approvals` with:

- `decision_type ∈ {pattern_regime_tilt, pattern_regime_promotion,
  pattern_regime_killswitch}`
- `approval_live = true`
- `expires_at IS NULL OR expires_at > now()`

Absent a live approval row, authoritative mode is refused (log +
fallback). This keeps the env flag and the human approval **separate**
gates — neither alone can activate the consumer.

## Rollout (per-slice, staged)

Order: **M.2.a (cheapest to reverse) → M.2.c (most protective) → M.2.b
(slowest feedback)**. Each slice completes its full cutover before the
next begins.

### Shared pre-flight (all slices)

1. Confirm M.1 ledger is populated: check
   `GET /api/trading/brain/pattern-regime-performance/diagnostics`
   returns `mode="shadow"`, `ledger_rows_total > 0`, and
   `confident_cells_total > 0` for the target dimensions.
2. If `ledger_rows_total = 0`, run the backfill:

   ```powershell
   conda activate chili-env
   python scripts\backfill_pattern_regime_ledger.py `
       --start 2026-01-01 --end 2026-04-15 --dry-run
   # verify the dry-run output, then:
   python scripts\backfill_pattern_regime_ledger.py `
       --start 2026-01-01 --end 2026-04-15 --commit
   ```

### Step 1 — Shadow (5 business days per slice)

Flip `.env`:

```
BRAIN_PATTERN_REGIME_TILT_MODE=shadow           # Day 1
BRAIN_PATTERN_REGIME_KILLSWITCH_MODE=shadow     # Day 1
BRAIN_PATTERN_REGIME_PROMOTION_MODE=shadow      # Day 1
```

Recreate services:

```powershell
docker compose up -d --force-recreate chili brain-worker brain-worker-heavy
```

Verify each slice diagnostics reports `mode=shadow` and the three
release blockers all PASS (exit 0). Let the shadow window run **5
business days**.

During shadow:
- Inspect `trading_pattern_regime_tilt_log` (grows with every
  `position_sizer_emitter.emit_shadow_proposal` for proposals tagged
  with `pattern_id`).
- Inspect `trading_pattern_regime_promotion_log` (grows with every
  `governance.request_pattern_to_live` call).
- Inspect `trading_pattern_regime_killswitch_log` (grows once per day
  when the APScheduler sweep runs).

Fallback rate sanity checks:
- Tilt: `would_apply_multiplier` distribution (expect most near 1.0
  in aggressive markets; left/right skew under stress).
- Promotion: `consumer_allow=false` ratio among baseline-allow
  evaluations.
- Kill-switch: number of `consumer_quarantine=true` would-fire events
  per day.

If any slice shows unexpectedly high would-fire rates (> 10% of
baseline promotions blocked, or > 5 kill-switch fires / day across
live patterns), **stop** and investigate before proceeding to
compare.

### Step 2 — Compare (5 business days per slice)

Only flip the slice whose shadow window passed. For example, after
M.2.a shadow is clean:

```
BRAIN_PATTERN_REGIME_TILT_MODE=compare
```

Recreate services. Compare mode writes the decision and a
`consumer_*` diff field so you can compute projected P&L impact
offline. Baseline still wins; no behaviour change.

Required gates before moving to authoritative:
- Decision-diff projected P&L ≥ 0 over the compare window.
- No `*_refused_authoritative` events (compare should never emit
  these).
- Release blocker PASS.
- Manual spot-check of 5 random decision rows (`reason_code`,
  `context_hash`, `ledger_run_id` all match an existing M.1 row).

### Step 3 — Authoritative (per-slice, only after compare passes)

Authoritative activation requires **two** things in the same
transaction — neither alone works:

1. Insert a row into `trading_governance_approvals`:

   ```sql
   INSERT INTO trading_governance_approvals (
       decision_type, decision_key, approval_live, expires_at,
       approved_by, approved_at, note
   ) VALUES (
       'pattern_regime_tilt',     -- or _promotion / _killswitch
       'global',
       true,
       NOW() + INTERVAL '30 days',
       'operator-name',
       NOW(),
       'Phase M.2.a authoritative cutover — approved after 5 BD compare'
   );
   ```

2. Flip the slice mode:

   ```
   BRAIN_PATTERN_REGIME_TILT_MODE=authoritative
   ```

   and `docker compose up -d --force-recreate chili brain-worker
   brain-worker-heavy`.

Verification after flip:
- Diagnostics endpoint reports `mode="authoritative"` and
  `approval_live=true`.
- Run the slice release blocker — must exit 0.
- Within 24 hours, at least one `event=*_applied mode=authoritative`
  line appears in `[pattern_regime_*_ops]`.

## Rollback

Rollback is symmetric per slice. To roll back authoritative:

1. Flip env flag back to `compare` (**not** straight to `off`):

   ```
   BRAIN_PATTERN_REGIME_TILT_MODE=compare
   ```

2. `docker compose up -d --force-recreate chili brain-worker
   brain-worker-heavy`.

3. Revoke the governance approval:

   ```sql
   UPDATE trading_governance_approvals
      SET approval_live = false, expires_at = NOW()
    WHERE decision_type = 'pattern_regime_tilt'
      AND approval_live = true;
   ```

4. Verify `mode="compare"` in diagnostics and release blocker PASS.

To roll back compare → shadow → off, repeat steps 1 + 2 with the next
lower mode. At each step verify diagnostics and release blocker before
stepping again.

For M.2.c (kill-switch), rolling back **does not** automatically
un-quarantine patterns that were already quarantined — that is
intentional (kill-switch actions should be re-reviewed manually via
the existing governance workflow for un-quarantine).

## Additive-only invariants

- `trading_pattern_regime_performance_daily` (M.1 table) is
  **read-only** for M.2 services. No M.2 code writes to it.
- Migration 145 is idempotent (`IF NOT EXISTS` guards), uses
  `schema_version` bump, and does not alter any existing column.
- `PositionSizerLog` adds only two **nullable** columns — nothing
  downstream depends on non-null values.
- `trading_governance_approvals.expires_at` is nullable and backwards
  compatible with existing rows (NULL = never-expires).
- `scan_status` frozen contract is untouched; regression 195/195 pass.

## Pre-flight for M.3

M.3 opens the door to **pattern × regime interaction** (2-D or
N-D slicing, e.g. `macro × vol_regime`), richer decision surfaces
(e.g. intraday trading window gating), and the first consumer of
kill-switch un-quarantine recovery. Do not open M.3 until:

1. All three M.2 slices are **authoritative** and have ≥ 10 BD of
   live data.
2. The kill-switch 30-day per-pattern circuit breaker has held
   (no pattern hit its cap).
3. Compare mode projected-P&L vs realised-P&L correlation is
   positive (so the metric is predictive, not random).
4. Governance approvals show a clean approve/revoke audit trail.
