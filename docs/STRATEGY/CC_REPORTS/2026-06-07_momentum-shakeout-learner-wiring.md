# CC_REPORT: momentum-shakeout-learner-wiring

> Note: this task arrived as a **direct operator brief in the Claude Code session**,
> not via `NEXT_TASK.md` (which still holds the unrelated phase-5i soak, left
> untouched). Found via the trading audit (adversarially verified).

## What shipped

Commit `091c3e2` — *fix(momentum-lane): make the post-exit shake-out learner actually learn*.
One atomic commit (the two fixes are one capability: the labeler produces labels,
the aggregate consumes them — neither works without the other).

Files touched (6): `app/config.py`, `app/services/trading/momentum_neural/post_exit_excursion.py`,
`app/services/trading/momentum_neural/evolution.py`, `app/services/trading_scheduler.py`,
`tests/test_post_exit_excursion.py`, `scripts/reprocess_post_exit_markers.py` (new).
Migrations: none.

### Fix 1 — the labeler orphaned every marker

`run_post_exit_excursion_pass` selected sessions by `updated_at >= now - (horizon*4+3600)`
(~3h). A terminal session freezes `updated_at` at exit, but a marker can only be
labeled **after** its ~30min horizon elapses — so any gap (scheduler restart,
backlog) longer than the window orphaned the marker **permanently** (updated_at
never moves again).

- Now selects on the **durable marker state** in JSONB:
  `risk_snapshot_json->'momentum_live_execution'->'post_exit_excursion_pending'->>'state' = 'pending'`.
  A marker is re-selected until actually processed; processed markers drop out of
  the `pending` set, keeping the scan small (51 live sessions live — trivial).
- New outer bound `CHILI_MOMENTUM_POST_EXIT_MAX_AGE_SECONDS` (default 48h, measured
  from the **marker's own exit_time**, not the frozen `updated_at`). A marker older
  than this is retired as `expired` so it can't be rescanned forever. The existing
  attempts cap is unchanged.
- Scheduler now logs the summary whenever a marker is **touched / expired / errored**
  (`trading_scheduler.py`), not only on a successful label — so a silent stall
  (checked>0, labeled=0) is observable.
- `scripts/reprocess_post_exit_markers.py` forces the catch-up immediately (host or
  post-deploy) and prints a before/after census. `--dry-run` for census only.

### Fix 2 — the labels were write-only

`setup_quality` / `stop_too_tight` / `post_exit_label` were stamped onto the outcome
row and read by **nothing**. Two layers blocked them:

1. Every shake-out is recorded as a non-strategy `cancelled_in_trade`
   (`contributes_to_evolution=False`), and `aggregate_recent_outcomes_*` filtered
   `contributes_to_evolution.is_(True)` — so the labeled rows were **excluded from
   aggregation entirely**. The named fix (consume the label in the aggregate) would
   have been a no-op without addressing this.
2. The aggregate read raw `return_bps`/`realized_pnl_usd` and never the label.

Now `aggregate_recent_outcomes_for_variant` / `_for_symbol_variant`:
- Re-include rows carrying a `stop_too_tight` label alongside credited rows
  (`_contributes_or_shakeout_filter`).
- `_aggregate_rows` adds a **setup channel**: a shake-out's setup is credited with
  the favorable post-exit excursion it actually achieved, weighted by `setup_quality`
  (`eff_rb = raw*(1-sq) + mfe_bps*sq`), instead of scoring the realized loss as a
  thesis failure. New keys: `mean_setup_adjusted_return_bps`, `mean_setup_quality`,
  `setup_credited_count`, `shakeout_count`. Raw `mean_return_bps` is **preserved
  intact** (honest realized P&L).
- `_viability_delta_from_slices` consumes `mean_setup_adjusted_return_bps` (falls
  back to raw) — so a too-tight stop no longer degrades viability for a setup that
  worked, and a confirmed shake-out can nudge it slightly up.

## Verification

- **Unit:** `tests/test_post_exit_excursion.py` 13/13 pass (env python; `conda run`
  was crashing on a plugin fault — bypassed by calling `chili-env\python.exe`
  directly). New tests: shake-out credited despite negative PnL; thesis_invalidated
  stays a loss in both channels; unlabeled rows unchanged; ancient marker expires.
- **Generated SQL** verified against the live `chili` census (matches the durable
  cursor + shakeout-include filter I hand-ran during diagnosis).
- **Live diagnosis (before-state, read-only):** 51 live sessions; **11 pending
  markers, all `live_cancelled`, ages 9.5–17.5h — all out of the old window**; the
  durable label had been written exactly once across the whole table; 7 outcome rows
  carry a write-only `post_exit_label` (6 shake-out sq=1.0, 1 premature_stop sq=0.6;
  mfe +3% to +12.9%), **all `contributes_to_evolution=false`**.
- **PENDING — Docker recovery:** Docker Desktop's engine crashed mid-session (500 on
  all API routes, port 5433 closed — the known failure mode in
  `reference_docker_recovery.md`). The DB-backed momentum suite and the live one-shot
  reprocess of the 11 orphaned markers could not run. **The live CHILI trading stack
  is currently down with it** (postgres, scheduler, autotrader, etc.).

## Surprises / deviations

- The brief scoped Fix 2 to "aggregate_recent_outcomes_*". On live data that alone is
  a **no-op**: all labeled rows are `contributes_to_evolution=false` and were filtered
  out upstream. I extended the two aggregate queries to re-include `stop_too_tight`
  rows — necessary for the brief's stated outcome ("the brain actually learns the stop
  was too tight"). Flagging because it widens *which* rows the selection aggregate
  sees (still capped, tanh-saturated, ±0.06 viability cap — no runaway).
- `conda run` is crashing on this box (plugin fault); used the env interpreter
  directly. Unrelated to this change.

## Deferred

- **`maybe_pause_symbol_variant_after_losses` / `maybe_kill_underperforming_variant`**
  still count a shake-out as a raw-PnL loss (consecutive-loss pause; win-rate kill).
  Left them out to keep this change scoped to the named target and avoid touching
  variant-killing behavior without authorization. They are the next "penalizing good
  setups" mechanisms to make shake-out-aware.
- **Consumption cadence:** the setup-adjusted signal flows into viability only when a
  *contributes=True* outcome arrives to trigger ingest (or via the operator read
  model). Since the lane currently produces almost only cancels, a more direct path
  would be to trigger the viability feedback at the moment the label is stamped. See
  Open Questions.

## Open questions for Cowork

1. **Should a confirmed shake-out flip `contributes_to_evolution` true** (or trigger
   viability feedback directly when the label is stamped), so the deferred evidence is
   consumed promptly instead of only when an unrelated credited outcome happens by?
2. **Should the deeper outcome-classification bug be fixed** — a real entry that got
   stopped is recorded as `cancelled_in_trade` rather than `stop_loss`? That's the
   root reason these never get credit.
3. Extend shake-out awareness to the pause/kill gates (Deferred #1)?

## Rollback

Revert commit `091c3e2`. No migration, no schema change, no destructive DB op — the
labeler only flips marker states and stamps JSONB; the aggregate only adds read-side
fields and widens a read filter. Safe to revert with zero data cleanup.
