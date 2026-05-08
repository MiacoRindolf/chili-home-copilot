# NEXT_TASK: f-fastpath-maker-only-executor

STATUS: PENDING

**Follow-up to `f-fastpath-maker-only`** (DONE 2026-05-08 as foundation layer per the CC report). The original brief listed 11 sequenced steps; CC shipped 6 (foundation: migration + settings + gate dispatch + calibration table allowlist + 23 tests). **This brief is the deferred half** — the actual executor path that places `post_only` orders, the decay-miner writer that populates `fast_signal_decay_maker_filled`, and the status endpoint that exposes per-pair fill rate.

## Why now

The foundation layer is in place and deployed-ready (migration 232 + tables + four new settings + mode-dispatched cost gate + table-name allowlist). Until the executor brief ships, **`CHILI_FAST_PATH_EXECUTION_MODE=maker_only` cannot be flipped** — the gate would dispatch to `fast_signal_decay_maker_filled` (empty), reject everything, and even taker orders wouldn't get through. The executor brief is the unblock.

References:
- Original brief: `docs/STRATEGY/QUEUED/f-fastpath-maker-only.md` (steps 6/7/8/9 of its sequencing apply)
- Foundation CC report: `docs/STRATEGY/CC_REPORTS/2026-05-08_f-fastpath-maker-only.md`
- Foundation review: `docs/STRATEGY/COWORK_REVIEWS/2026-05-08_f-fastpath-maker-only.md`
- Alpha replay: `docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md`

## Goal

Wire up the executor path + decay writer + status surface that the foundation layer is waiting on:

1. **`executor.py` — `place_maker_only` path.**
   - When `execution_mode == 'maker_only'` (or `'maker_first_then_taker'`), place `post_only=true` limit orders inside the spread.
   - Limit price = `best_bid + 1 tick` (long entries) / `best_ask − 1 tick` (short entries).
   - Track in `fast_path_maker_attempts` (placed_at, broker_order_id, limit_price, spread_at_placement_bps).
   - Cancel-on-timeout: cancel + abandon if unfilled within `settings.maker_cancel_on_timeout_s` (default 10s).
   - **Hard cap: 1 outstanding maker order per (ticker, side).** Prevents stale-limit pile-up if signals fire faster than the cancel-on-timeout period.
   - On fill: record fill_price, filled_at, time_to_fill_ms, mid_drift_bps, spread_at_fill_bps. Update `fast_executions.quantity` for partial-fill bookkeeping.
   - On cancel: record cancelled_at, fill_outcome='cancelled', final_price=None.
   - Mirror short-entry logic.

2. **`maker_first_then_taker` mode** — try `post_only` for `settings.maker_first_taker_fallback_s` seconds (default 5s), then fall back to taker if unfilled. Records both attempts in `fast_path_maker_attempts` with `fill_outcome='replaced'` on the maker side.

3. **`decay_miner.py` writer.**
   - On observed maker-fill outcome (filled or cancelled), update `fast_signal_decay_maker_filled` Welford stats — same shape as the existing `fast_signal_decay` updates but writing to the new table.
   - When forward-return horizon ticks complete, compute mean_return for the (ticker, alert_type, score_bucket, horizon_s) cell and update.
   - **`fill_rate` column** populated as `N filled / N attempted` per cell (running ratio; cell-local).
   - Existing `fast_signal_decay` writes continue unchanged for taker-mode; the maker-filled table is sibling, not a replacement.

4. **Status surface.**
   - Extend `GET /api/trading/fast-path/universe` (or new sibling `GET /api/trading/fast-path/maker-stats`) with last-24h per-pair fill rate.
   - Pairs with fill_rate < 25% flagged with `advisory: "uneconomic for maker-only"`. Reads from `fast_path_maker_attempts` aggregated.

5. **Tests** (mirror foundation-layer pattern):
   - `tests/test_fastpath_maker_executor.py` — helper-level: `post_only` flag passed to broker stub; cancel-on-timeout path; 1-outstanding cap; partial-fill bookkeeping; mode-dispatch (`taker` vs `maker_only` vs hybrid). Use `unittest.mock` for the broker.
   - `tests/test_fastpath_maker_decay_writer.py` — Welford updates land in `fast_signal_decay_maker_filled`, not `fast_signal_decay`. Fill rate column updates monotonically.
   - `tests/test_fastpath_maker_status_endpoint.py` — 200 response shape includes `maker_stats` with per-pair fill rates + advisory hints.

## Acceptance criteria

1. `executor.py` handles all three values of `settings.execution_mode` correctly. Default `taker` is bit-identical to today.
2. `fast_path_maker_attempts` rows accumulate during a soak (operator-side; not testable in CC).
3. `fast_signal_decay_maker_filled` Welford rows accumulate (operator-side).
4. Status endpoint returns the new shape; helper tests pin it.
5. Helper-level tests pass (mirror foundation's 23/23 pattern); DB-bound tests deferred.
6. `executor.py` AST clean, `wc -l` matches expected change set, splice pattern used (NOT Edit tool).
7. CC report at `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-fastpath-maker-only-executor.md`.

## Brain integration (reuse, don't rewrite)

- **Foundation-layer settings + gate dispatch.** Already shipped; reuse via `settings.execution_mode`, `settings.cost_aware_maker_fee_bps`, `settings.maker_cancel_on_timeout_s`, `settings.maker_first_taker_fallback_s`.
- **`fast_path_maker_attempts` schema.** Already migrated; just write to it.
- **`fast_signal_decay_maker_filled` schema.** Already migrated; just write to it.
- **`_fetch_bucket_rows` table allowlist.** Already includes both decay tables; just call with `table='fast_signal_decay_maker_filled'`.
- **Existing `decay_miner.py` Welford-update functions.** Reuse the math; just dispatch on table name.
- **Coinbase Advanced Trade adapter.** Use whatever the executor already calls for limit orders; add `post_only=true` parameter rather than building a new client.
- **Existing `fast_executions.quantity` partial-fill column.** Already supports partials; verify the maker path reads it correctly.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. **`CHILI_FAST_PATH_MODE=paper` stays default.** Maker-only mode is paper-default.
- **Default `CHILI_FAST_PATH_EXECUTION_MODE=taker`.** Unchanged from foundation layer; preserves switchover bit-identity.
- **Edit-tool truncation discipline (HARD).** Memory `reference_2026_05_07_widespread_truncation.md`. Four rounds of post-CC truncation today, two with AST-broken files (migrations.py, gates.py). For any non-trivial edit:
  - **Mandatory Step 0**: truncation scan via the one-liner; restore via `git checkout HEAD -- <file>` if anything flags.
  - Splice pattern (`git show HEAD: | python str.replace + ast.parse + write`) for any file >100 lines getting non-trivial edits.
  - Verify post-edit with **(a) `wc -l` against HEAD, AND (b) `ast.parse()`**.
  - **Critical files this brief touches: `executor.py` (highest-risk; 702 lines), `decay_miner.py`.** Splice pattern only. Do NOT use the Edit tool for non-trivial edits.
- **No threshold tuning.** The four maker-related settings stay at their defaults from the foundation layer.
- **Migration ID**: 233 is the next free (231 = universe, 232 = maker foundation).
- **Tests use `_test`-suffixed DB.**
- **No magic numbers.** Tick-size adjustments and limit-offset must be settings-tunable.

## Out of scope

- Hyperliquid perps integration.
- Microstructure features (toxic flow, depth-decay, OFI).
- Adaptive limit-tick offset based on book depth.
- Queue-position estimation.
- Smart routing.

## Sequencing within this task

1. **Truncation scan** (mandatory).
2. **`decay_miner.py` writer first.** It's the simplest of the three; gets the table-write plumbing in place. Splice pattern.
3. **`executor.py` maker-only path.** HIGHEST-RISK file. Splice pattern from start. Post-edit grep for landmarks: `def execute_paper_fill`, `class ExecContext`, `place_paper_fill`, etc.
4. **`maker_first_then_taker` mode wiring** in executor.
5. **Status endpoint extension.**
6. **Tests** (3 new files).
7. **One commit per logical step.**
8. **CC report.**

## Operator-side after CC ships

Per the foundation review's section + this brief's additions:

1. `git pull` on the operator's box.
2. **Truncation scan** (mandatory).
3. If anything flags: `git checkout HEAD -- <file>` to restore.
4. `docker compose up -d --force-recreate chili scheduler-worker fast-data-worker`.
5. **Wait for rotator soak**: 24h+ of decay rows in `fast_signal_decay` on new shadow pairs.
6. **THEN consider flipping `CHILI_FAST_PATH_EXECUTION_MODE=maker_only`** in `.env` (re-up). Only flip when:
   - Coinbase volume tier confirmed (cost_aware_maker_fee_bps default = 40.0 = tier 1).
   - 24h+ of `fast_signal_decay_maker_filled` rows for at least 3 active pairs (this brief's writer populates them).
   - Fill rate ≥ 25% on those pairs.
   - At least one (ticker, alert_type, score_bucket) cell shows `mean_return > 2 × maker_round_trip` with `sample_count ≥ 30`.
7. After 48h of maker-only paper soak, evaluate per-pair fill rate; pairs below 25% get dropped from the universe.

## Rollback plan

`git revert` the commit. Setting `CHILI_FAST_PATH_EXECUTION_MODE=taker` (the default) restores prior behavior. The new `decay_miner.py` writer is purely additive — its removal restores prior write-only-to-`fast_signal_decay` behavior.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Operator's actual Coinbase volume tier.** The foundation brief flagged this; the executor brief inherits it. Surface confirmation status — once confirmed, the maker-fee default may need overriding.
2. **`maker_first_then_taker`'s value-add.** Foundation CC asked whether it's overengineered. If executor implementation suggests it adds significant complexity per economic upside, recommend dropping it from settings and trimming gate dispatch to two modes (`taker` / `maker_only`). Surface this in the CC report.
3. **Tick-size sourcing.** The brief proposes `best_bid + 1 tick` for limit price. Coinbase's product metadata exposes `quote_increment` per pair; reuse it. If unavailable for some pair, default to a small offset (e.g., `0.01%` of mid). Settings-tunable.
4. **Cancel-on-timeout implementation.** Background asyncio task vs per-tick check. Pick whatever fits the executor's existing pattern; surface trade-off if non-obvious.
