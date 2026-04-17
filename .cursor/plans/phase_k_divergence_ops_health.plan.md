---
status: completed_shadow_ready
title: Phase K - Divergence panel + ops health endpoint (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
---

## Objective

Introduce a **canonical divergence panel** that aggregates all paper-vs-live
and legacy-vs-canonical divergence signals persisted by Phases A/B/F/G/H,
plus a canonical **`/api/trading/brain/ops/health`** endpoint that gives
operators a single read-only view of the entire trading-brain substrate
(Phases A through J) alongside scheduler / governance state.

Problem this solves:

1. Phase A (`trading_ledger_parity_log`), Phase B (`trading_exit_parity_log`),
   Phase F (`trading_venue_truth_log`), Phase G
   (`trading_bracket_reconciliation_log`), and Phase H
   (`trading_position_sizer_log.divergence_bps`) each expose their own
   divergence fields in isolation. There is no single aggregation that
   tells an operator "pattern X is consistently diverging across multiple
   substrate layers and should be re-certified or quarantined".
2. There is no single endpoint that answers "is the trading brain
   healthy?". Operators currently have to pull 14 different
   `/api/trading/brain/*/diagnostics` endpoints to build a picture.
3. The master plan bullet "paper-vs-live divergence auto-quarantine"
   cannot be trusted without first proving the aggregation layer
   classifies divergence correctly against real substrate data. K.1
   ships the observability; K.2 opens the authoritative cutover to
   lifecycle mutation.

Phase K ships:

1. **Canonical divergence scorer** (`divergence_model.py` + service).
   A pure module that, given the recent rows of the five divergence-
   bearing tables for one `scan_pattern_id`, produces a
   `DivergenceScore` with:
     - per-layer severity (`ledger`, `exit`, `venue`, `bracket`, `sizer`)
     - an overall severity bucket `{green, yellow, red}` with hysteresis
       on minimum sample size across layers
     - structured reason codes for the top divergent layers
     - a deterministic `divergence_id` for `(pattern, as_of_key)`
   A daily APScheduler sweep appends one row per eligible pattern into
   a new `trading_pattern_divergence_log`. Purely observational - no
   lifecycle mutation, no `scan_patterns` writes.
2. **Ops health endpoint** (`/api/trading/brain/ops/health`).
   A single read-only route that returns a canonical frozen-shape
   aggregation of:
     - per-phase substrate status (A, B, F, G, H, I, J) re-using each
       phase's existing `*_summary` function
     - scheduler job list + running flag (via existing
       `get_scheduler_info`)
     - governance state (`get_governance_dashboard` minus approval
       lists, which stay on their own endpoint)
     - divergence panel summary
     - a top-level `overall_health_severity` derived from the union of
       per-phase `mode` and latest severity counts - never fails closed
       to `ok=false`; the `ok` contract mirrors existing diagnostics.
3. **Release-blocker PowerShell scripts.** Mirror Phase I/J conventions:
   - `check_divergence_release_blocker.ps1` blocks authoritative
     divergence emissions until K.2.
   - `check_ops_health_release_blocker.ps1` validates the JSON frozen
     shape of `/ops/health` (required keys present, per-phase sub-keys
     match the frozen contract).
4. **Docker soak** (`phase_k_soak.py`) exercising the full chain
   inside the running `chili` container.
5. **Docs** `docs/TRADING_BRAIN_DIVERGENCE_OPS_HEALTH_ROLLOUT.md`.

## Scope (K.1, strictly shadow-only)

- **Allowed writes:** `trading_pattern_divergence_log` only.
- **Allowed reads:** All existing Phase A-J summary functions and the
  five divergence-bearing tables listed above. The
  `get_scheduler_info()` and `get_governance_dashboard()` helpers.
  `scan_patterns` is read-only for `lifecycle_stage`, `active`,
  `win_rate`, `scan_pattern_id`.
- **New endpoints (additive):**
  1. `GET /api/trading/brain/divergence/diagnostics`
  2. `GET /api/trading/brain/ops/health`
- **Mode gating:** `BRAIN_DIVERGENCE_SCORER_MODE` with the same four
  `off|shadow|compare|authoritative` states as prior phases.
  `authoritative` is **blocked** with an explicit `RuntimeError` until
  K.2. `brain_ops_health_mode` is not a gate - the endpoint is always
  safe to hit. It does expose a `brain_ops_health_enabled` toggle for
  ops emergencies.

## Forbidden changes (K.1)

- **No writes** to `scan_patterns`, `trading_trades`,
  `trading_paper_trades`, or any existing Phase A-J log table. The
  divergence service is **append-only** to its own new table.
- **No changes** to `/api/trading/scan/status` payload, `brain_runtime`
  shape, or any existing `*_summary` dict shape. K.1 consumes summaries
  read-only.
- **No lifecycle FSM transitions** from the divergence score. K.1
  observes; K.2 (not opened) will wire divergence severity into the
  Phase J re-cert queue and/or the lifecycle FSM.
- **No flipping** `BRAIN_DIVERGENCE_SCORER_MODE=authoritative` in any
  environment before K.2 is explicitly opened.
- **No hot-path emissions.** The divergence scorer is only called by
  the scheduled sweep and the diagnostics endpoint; no scanner /
  alerts / paper-trading / stop-engine path imports it.

## What lands (dependency order)

1. **Migration `137_divergence_panel`** - creates
   `trading_pattern_divergence_log` with: `id`, `divergence_id`,
   `scan_pattern_id`, `pattern_name`, `as_of_date`, per-layer
   severities (`ledger_severity`, `exit_severity`, `venue_severity`,
   `bracket_severity`, `sizer_severity`), overall `severity`,
   aggregated `score` (0.0-1.0), `layers_sampled` (int),
   `layers_agreed` (int), `layers_total` (int), `payload_json`,
   `mode`, `sweep_at`, `observed_at`. Indexes: `(scan_pattern_id,
   sweep_at)`, `(severity, sweep_at)`, `(divergence_id)`.
2. **ORM** `PatternDivergenceLog` appended to
   `app/models/trading.py` mirroring the migration.
3. **Pure model** `app/services/trading/divergence_model.py`:
   - `DivergenceConfig` (severity thresholds, min-sample gate,
     per-layer weight map).
   - `LayerSignal(layer, severity, reason_code, observed_at, score)`
     dataclass.
   - `DivergenceInput(scan_pattern_id, pattern_name, as_of_key,
     signals: Sequence[LayerSignal])`.
   - `DivergenceOutput` (all fields of the log table).
   - `compute_divergence_id(scan_pattern_id, as_of_key) -> str`.
   - `compute_divergence(inputs, *, config) -> DivergenceOutput` -
     classifies by `max(weighted_layer_score)` hysteresised against
     `min_layers_sampled`.
4. **Pure model** `app/services/trading/ops_health_model.py`:
   - `OpsHealthSnapshotInput` (per-phase summary dicts + scheduler +
     governance + divergence summary).
   - `OpsHealthSnapshot` dataclass with a frozen-shape `to_dict()`.
   - `compute_overall_severity(snapshot) -> str` with clear rules:
     * `red` if any authoritative-blocked mode observed or any
       severity breach above `red_threshold`;
     * `yellow` if any shadow-mode substrate is emitting red counts
       >0 in the lookback;
     * `green` otherwise.
5. **Pure unit tests**
   `tests/test_divergence_model.py` and
   `tests/test_ops_health_model.py`.
6. **Config flags + ops-log modules** -
   `brain_divergence_*` and `brain_ops_health_*` added to
   `app/config.py`; `divergence_ops_log.py` created. Ops health does
   not need its own log module in K.1 (the endpoint is read-only).
7. **DB service** `divergence_service.py`:
   - `evaluate_pattern(db, *, bundle, as_of_key, ...)` - mirrors
     Phase J writer pattern.
   - `run_sweep(db, *, bundles, as_of_date, ...)`.
   - `divergence_summary(db, *, lookback_days)` returning the frozen
     shape used by the diagnostics endpoint.
   - Mode gating (`_effective_mode` / `mode_is_active` /
     `mode_is_authoritative`) matching Phase J.
   - Authoritative refusal via `RuntimeError` + ops log warning.
8. **DB service** `ops_health_service.py`:
   - `build_health_snapshot(db, *, lookback_days)` - calls each of
     `ledger_parity_summary`, `exit_parity_summary`,
     `venue_truth_summary`, `bracket_reconciliation_summary`,
     `position_sizer_proposals_summary`, `risk_dial_summary`,
     `capital_reweight_summary`, `drift_summary`, `recert_summary`,
     `divergence_summary`, and the scheduler / governance helpers.
   - Returns the frozen-shape dict.
9. **DB tests** `test_divergence_service.py`,
   `test_ops_health_service.py` (authored; validated via Docker
   soak per the Phase I/J pattern to avoid local pytest hang on live
   Postgres contention).
10. **APScheduler registration** in `trading_scheduler.py` of
    `divergence_sweep_daily` (cron default 06:15), gated by
    `BRAIN_DIVERGENCE_SCORER_MODE not in (off, authoritative)`. The
    job iterates patterns with any recent divergence signal from the
    five source tables and calls `divergence_service.run_sweep`.
11. **Diagnostics endpoints** in `app/routers/trading_sub/ai.py`:
    - `GET /api/trading/brain/divergence/diagnostics`
      returning `{"ok": true, "divergence": <summary>}`.
    - `GET /api/trading/brain/ops/health` returning
      `{"ok": true, "ops_health": <snapshot>}`.
12. **API smoke tests** `tests/test_phase_k_diagnostics.py` asserting
    frozen top-level and nested key sets.
13. **Release-blocker PowerShell scripts**
    `scripts/check_divergence_release_blocker.ps1` (log pattern grep
    + optional diagnostics JSON gate) and
    `scripts/check_ops_health_release_blocker.ps1` (pure JSON gate:
    required keys present + `ok=true`).
14. **Docker soak** `scripts/phase_k_soak.py` verifying:
    - Migration 137 applied.
    - Settings exist.
    - `evaluate_pattern` no-op in `off`, writes in `shadow`, refuses
      `authoritative`.
    - `run_sweep` produces one row per bundle.
    - `divergence_id` deterministic.
    - `divergence_summary` frozen shape.
    - `/ops/health` returns all required phase keys and the
      `overall_health_severity` field.
15. **.env** flipped to `BRAIN_DIVERGENCE_SCORER_MODE=shadow` and
    `BRAIN_OPS_HEALTH_ENABLED=true`.
16. **Regression** `scan_status` frozen contract + all Phase K pure
    tests.
17. **Docs**
    `docs/TRADING_BRAIN_DIVERGENCE_OPS_HEALTH_ROLLOUT.md`.
18. **Closeout** - plan YAML flipped to `completed_shadow_ready` with
    closeout + self-critique.

## Verification gates

- Pure unit tests pass 100% for both new models.
- Docker soak `phase_k_soak.py` reports `ALL CHECKS PASSED`.
- Both release blockers exit 0 against a 10-minute live log window.
- `/api/trading/brain/ops/health` returns `ok=true` with all
  expected top-level and per-phase keys on a live service.
- `/api/trading/scan/status` regression tests still pass (frozen
  contract untouched).
- No lint regressions on files touched.

## Rollback

At any time during shadow:

1. Set `BRAIN_DIVERGENCE_SCORER_MODE=off` in `.env`.
2. Set `BRAIN_OPS_HEALTH_ENABLED=false` if the endpoint must be
   disabled (otherwise it stays up - it is read-only).
3. Recreate `chili`, `brain-worker`, `scheduler-worker`.
4. `evaluate_pattern` / `run_sweep` become no-ops.
5. Scheduler `divergence_sweep_daily` is not registered.
6. Existing rows in `trading_pattern_divergence_log` are left intact
   for post-mortem.

## Non-goals

- Automated pattern quarantine or lifecycle transitions from
  divergence severity. Deferred to K.2.
- Re-cert queue integration. Phase J's queue is not fed by
  divergence in K.1; K.2 can wire it.
- UI surfaces for the panel. Deferred to K.2.
- Modifying any existing Phase A-J diagnostics summary shape. All
  consumption is read-only.
- Adding a sixth divergence-bearing table (e.g. a new "execution
  fill vs paper expected" log). The five existing layers are the
  sources of truth.
- Covariance-based pattern clustering. Out of scope.

## Definition of done (K.1)

Shadow substrate runs in all three services. Divergence sweep fires
daily and appends rows to `trading_pattern_divergence_log`. The
`/api/trading/brain/ops/health` endpoint returns the frozen shape
with `mode: "shadow"` on the divergence sub-object. All tests green,
release blockers clean against live logs, and the plan closeout
documents the K.2 checklist (authoritative consumption of divergence
severity by re-cert queue / lifecycle FSM, UI panel).

## Closeout (K.1)

Shipped end-to-end:

- Migration `137_divergence_panel` and ORM model
  `PatternDivergenceLog`.
- Pure models `divergence_model.py` and `ops_health_model.py`
  (**35/35** unit tests pass).
- Config flags (`BRAIN_DIVERGENCE_SCORER_*`, `BRAIN_OPS_HEALTH_*`).
- Ops-log module `divergence_ops_log.py` with the
  `[divergence_ops]` prefix and one-line format consistent with
  other phases.
- DB services `divergence_service.py` (signal gathering for all five
  layers, mode gating, append-only writes, authoritative refusal,
  active-pattern discovery) and `ops_health_service.py` (defensive
  aggregation of every Phase A - K summary plus scheduler and
  governance state).
- APScheduler job `divergence_sweep_daily` (06:15 server time,
  gated by `BRAIN_DIVERGENCE_SCORER_MODE`).
- Diagnostics endpoints:
  - `GET /api/trading/brain/divergence/diagnostics`
  - `GET /api/trading/brain/ops/health`
- Release-blocker PowerShell scripts (both smoke-tested **5/5**):
  - `scripts/check_divergence_release_blocker.ps1`
  - `scripts/check_ops_health_release_blocker.ps1`
- Docker soak `scripts/phase_k_soak.py` (**45/45** checks pass inside
  the `chili` container against live Postgres).
- Env flipped to `BRAIN_DIVERGENCE_SCORER_MODE=shadow` +
  `BRAIN_OPS_HEALTH_ENABLED=true`; `chili`, `brain-worker`,
  `scheduler-worker` recreated. Live diagnostics return
  `mode=shadow`; scheduler registered
  `Divergence panel daily (06:15; mode=shadow)`.
- Docs: `docs/TRADING_BRAIN_DIVERGENCE_OPS_HEALTH_ROLLOUT.md`.
- `/api/trading/scan/status` frozen contract verified live and
  unchanged.

Self-critique (what to watch in shadow):

1. **Signal coverage is best-effort.** `gather_signals_for_pattern`
   pulls only the most-recent row per layer in the lookback. If a
   layer happens to have had one benign green row right after a
   string of reds, the panel will reflect the benign one. K.2
   should consider an N-of-M aggregation before moving to
   `authoritative`.
2. **`overall_severity` reflects the substrate, not individual
   patterns.** A single red pattern does not bubble up to the
   overall snapshot severity unless that phase's summary exposes a
   `red_count > 0`. This is intentional for K.1 (operators still see
   per-phase red counts) but should be revisited if we wire the
   snapshot into a pager.
3. **Defensive tolerance can hide bugs.** `_safe_call` swallows
   exceptions inside `ops_health_service`. A substrate that starts
   silently failing its summary will appear `present=False`, which
   is a yellow-ish signal rather than red. The warning log is the
   only contemporary signal; K.2 should convert repeated `present=False`
   into a structured phase health note.
4. **No dedupe.** Append-only writes mean repeated sweeps on the same
   pattern/as_of_key each add a row. This is intentional for K.1
   (simpler invariants) but means storage grows linearly with the
   cron cadence. Defer compaction to K.2.
5. **Live pytest against Docker Postgres still hangs** (pool
   contention). As in Phases H/I/J we validated the DB paths via
   the in-container soak script rather than local pytest; full test
   matrix is documented in the rollout doc.

K.2 checklist (deferred, requires a new approved plan):

- Wire `red` divergence severity into `recert_queue_service`
  (`source=divergence`).
- Auto-quarantine: move `active=false` + `lifecycle_stage=challenged`
  after N consecutive red sweeps (explicit FSM transition).
- UI panel surfacing divergence rows and the ops-health snapshot.
- Flip `BRAIN_DIVERGENCE_SCORER_MODE=authoritative` in staging first,
  then production, with dual-read / compare safeguards.
- Retention / compaction job for `trading_pattern_divergence_log`.

