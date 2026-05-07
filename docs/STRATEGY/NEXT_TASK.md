# NEXT_TASK: f-fastpath-maker-only

STATUS: PENDING

**Promoted 2026-05-07 evening, in parallel with the universe-rotation 48h soak.** The maker-only execution mode is the structural prerequisite for any live activation on Coinbase — the alpha replay confirmed no pair clears the 120 bps round-trip taker cost, but ICP-USD already clears the maker round-trip, and four other mid-tier pairs (RENDER/ARB/INJ/TAO) sit close. Code can ship in parallel with the soak; the soak's outcome only changes the activation decision.

## Why now

From `docs/STRATEGY/RESEARCH/2026-05-07_fastpath-universe-alpha-replay.md` and the universe-rotation review:

| Pair | 5m edge (bps) | rt_taker | net_taker | rt_maker | net_maker |
|---|---|---|---|---|---|
| **ICP-USD** | +6.13 | 123.4 | −117.2 | 3.4 | **+2.76** |
| RENDER-USD | +6.55 | 130.2 | −123.7 | 10.2 | −3.7 |
| ARB-USD | +4.17 | 127.9 | −123.7 | 7.9 | −3.7 |
| INJ-USD | +4.12 | 127.8 | −123.6 | 7.8 | −3.6 |
| TAO-USD | +2.55 | 122.6 | −120.1 | 2.6 | −0.07 |

Even on the best pair, taker round-trip costs ~117 bps per trip. Maker-only makes one pair tradeable today and brings four more to within 4 bps of break-even — and once any of those signals get a 1.3× edge improvement (e.g., from the planned `f-fastpath-microstructure-features-v2`), they all clear maker round-trip. **This is the critical leg between "structural loss" and "first positive-EV configuration."**

Brief: `docs/STRATEGY/QUEUED/f-fastpath-maker-only.md`. Earlier work landed today: `f-fastpath-universe-rotation` (commits `22cb7bd`, `d83ff03`, `a096651`, `107c349`); fee-default fix (commit `3f91cdc`).

## Goal

Add a maker-only execution mode to `app/services/trading/fast_path/executor.py`. Place `post_only=true` limit orders inside the spread instead of crossing it. Track fills + cancels in a new `fast_path_maker_attempts` table. Add a separate calibration table `fast_signal_decay_maker_filled` for adverse-selection-aware decay stats. Ship in **paper mode by default** (Hard Rule 1); operator's own decision when to flip live.

## Acceptance criteria

1. **Migration 232** (after universe rotation's 231) creates `fast_path_maker_attempts` and `fast_signal_decay_maker_filled` tables. Idempotent. Additive only — no destructive ALTERs on existing tables.
2. **Three execution-mode flag values** in `settings.py`:
   - `taker` — current behavior, default, retained as benchmark.
   - `maker_only` — `post_only=true` limit orders only; cancel + abandon if unfilled within timeout.
   - `maker_first_then_taker` — try maker, fall back to taker after `T` seconds (operator-tunable).
3. **Three new settings** with explicit doc comments and per-tier reference values:
   - `cost_aware_maker_fee_bps: float = 40.0` — Coinbase Advanced Trade retail tier 1 maker fee per-side. Same defect class as the fee-fix that just shipped — the docstring MUST be honest about the per-side framing and reference https://docs.cdp.coinbase.com/exchange/docs/fees with per-tier reference values.
   - `maker_cancel_on_timeout_s: int = 10` — cancel + abandon if unfilled.
   - `maker_first_taker_fallback_s: int = 5` — only used in `maker_first_then_taker` mode.
4. **`place_maker_only` path in `executor.py`**:
   - Limit price = `best_bid + 1 tick` for long entries (sit at the bid+ε to be filled by aggressive sellers).
   - Coinbase Advanced Trade flag `post_only=true`.
   - Cancel-on-timeout via async task; persist fill / cancel outcome to `fast_path_maker_attempts`.
   - Hard cap of **1 outstanding maker order per (ticker, side)** — prevents stale-limit pile-up.
   - Mirror logic for short entries.
5. **`fast_signal_decay_maker_filled` schema and writer**:
   ```sql
   CREATE TABLE fast_signal_decay_maker_filled (
     ticker varchar,
     alert_type varchar,
     score_bucket varchar,
     horizon_s integer,
     sample_count bigint,
     mean_return double precision,
     m2_return double precision,
     fill_rate double precision,            -- N filled / N attempted in this cell
     last_updated timestamp,
     PRIMARY KEY (ticker, alert_type, score_bucket, horizon_s)
   );
   ```
   Decay miner (or a peer module — operator/CC's call which) writes here when an attempted maker fill is observed (filled or cancelled). The "filled" subset's forward returns are biased by adverse selection; the table captures that empirically rather than modeling it.
6. **`gate_cost_aware_admission` reads from `fast_signal_decay_maker_filled`** when `execution_mode == 'maker_only'`. Falls back to `fast_signal_decay` when the maker-filled table has no row for the cell. Cold-start carve-out: same `no_data` allows-through pattern as universe rotation.
7. **Status surface**: extend `GET /api/trading/fast-path/universe` (or new endpoint `GET /api/trading/fast-path/maker-stats`) to expose per-pair fill rate over the last 24 h. Pairs with fill rate < 25% are flagged in the response with an `advisory: "uneconomic for maker-only"` hint.
8. **Tests**:
   - `tests/test_fastpath_maker_only.py` — helper-level: `post_only` flag passed to broker stub; cancel-on-timeout path; 1-outstanding-per-(ticker,side) cap; partial-fill bookkeeping path; mode-flag dispatch (`taker` vs `maker_only` vs `maker_first_then_taker`).
   - `tests/test_fastpath_maker_settings_validation.py` — same plausible-range pattern as `test_fastpath_settings_validation.py` for the new fee + timeout settings. **Critical**: `test_cost_aware_maker_fee_bps_default_is_retail_tier_1` asserting `40.0`. The brief's whole reason for existing today is that the prior brief shipped with a wrong fee default; CC's review SHOULD catch a repeat by running this test.
9. **No regression** in existing tests:
   - `test_fastpath_cost_aware_gate.py` (7 tests) — should still pass; the gate's behavior in `taker` mode is unchanged.
   - `test_fastpath_universe_rotator.py` (helper-level 7 tests) — orthogonal; should still pass.
   - `test_fastpath_settings_validation.py` (5 tests, just-shipped) — should still pass; the new maker setting must not regress the existing `cost_aware_taker_fee_bps == 60.0` assertion.
10. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-07_f-fastpath-maker-only.md` per PROTOCOL format.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/fast_path/executor.py` — the existing place-paper-fill path is the model. Add the maker-only path as a sibling, not a rewrite.
- `app/services/trading/fast_path/gates.py:gate_cost_aware_admission` — the cell-lookup pattern is established. The maker-mode read just changes which table it reads from; the verdict logic is unchanged.
- `app/services/trading/fast_path/decay_miner.py` — already does Welford updates on cells. Reuse the helpers; don't duplicate.
- `app/services/trading/fast_path/settings.py` — same `_env_*` helpers + dataclass pattern. The just-shipped fee-fix is the template.
- `app/services/trading/fast_path/calibration.py` — `_fetch_bucket_rows` and `_best_sharpe_row` should accept a table-name parameter (or a sibling function) so the cost-aware gate can choose which table to read from.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. Default `CHILI_FAST_PATH_MODE=paper`. Default `CHILI_FAST_PATH_EXECUTION_MODE=taker` (preserves current behavior at switchover).
- **No magic numbers.** The maker-fee default and the two timeouts MUST be settings-tunable per the no-magic-numbers rule. Per-knob doc comments.
- **Must default to `taker` execution mode.** This brief introduces the new mode but does NOT flip it on by default.
- **No removal or modification of existing taker-mode behavior.** The taker code path stays bit-identical.
- **No changes to `fast_signal_decay`.** The new table is sibling, not derived.
- **Migration 232 must check `_migration_NNN_` registry.** Same constraint that caught CC on the universe-rotation brief — they shipped 231 because 230 was taken. Now 232 is the next free ID.
- **Tests use `_test`-suffixed DB.**
- **Edit-tool truncation hazard.** Today this hit twice in the same session. **Always verify Python file edits with `wc -l` against HEAD AND `ast.parse()` immediately post-Edit.** If editing more than a single-line literal, use the `git show HEAD: | python str.replace + ast.parse + write` splice pattern. This is a hard requirement for this brief — losing 100+ lines of `executor.py` mid-Edit is much more dangerous than losing 100+ lines of `gates.py`.

## Out of scope

- Hyperliquid perps integration (`f-fastpath-hyperliquid-perps`). Different venue.
- Toxic-flow / depth-decay / OFI features (`f-fastpath-microstructure-features-v2`). Sequence after this brief.
- Order book queue position estimation. Out of scope; queued separately.
- Adaptive limit-tick offset based on book depth. Out of scope; queued separately.
- Smart order routing across venues. Out of scope.
- Backfilling `fast_signal_decay_maker_filled` from historical data. Cold-start shadow window pattern from the universe-rotation brief is the right way; backfill could leak data from windows where adverse-selection events were observed retroactively.
- Removing the legacy 5-pair pullback allowlist in `gates.py:280`. Separate brief.
- Changes to the universe rotator. The rotator already populates the active set; the maker-only path consumes that set without changes.

## Sequencing within this task

1. **Migration 232 + tables.** Idempotent. Verify with `.\scripts\verify-migration-ids.ps1` before merge.
2. **`settings.py` additions** — three new fields + env loaders. **Verify the file with `wc -l + ast.parse` immediately post-Edit.**
3. **`gates.py` adaptation** — `gate_cost_aware_admission` reads the right table based on execution mode. **Verify the file with `wc -l + ast.parse` immediately post-Edit.**
4. **`calibration.py` parameterization** — make the table-name optionally injectable; existing callers pass the legacy table.
5. **`decay_miner.py` writer** — when a maker fill outcome is observed, update the new table. The legacy table continues to be updated as today (taker assumption).
6. **`executor.py` maker-only path** — branch on `execution_mode` flag; place `post_only=true`; track in `fast_path_maker_attempts`; cancel on timeout. **HIGH RISK FILE for Edit truncation.** Use the splice pattern from the start; verify with `wc -l + ast.parse + grep` for known landmarks (e.g., `def execute_paper_fill`, `class ExecContext`).
7. **Status endpoint extension** — per-pair fill rate over last 24h.
8. **Tests** — both new test files; run them in chili container in a SEPARATE dispatch from the commit (lesson from today: don't bundle long pytest with commit dispatches).
9. **CC report**.

## Risks / hazards (carried from QUEUED brief)

1. **Adverse selection** on maker fills (a fill happens when someone aggressive ate through the resting limit). Realized post-fill returns typically 30–60% worse than no-friction backtest. Mitigated by the new `fast_signal_decay_maker_filled` table — it captures the bias empirically.
2. **Cancel storms** — the 1-outstanding-per-(ticker,side) cap is the structural fix. Verify in tests.
3. **Coinbase post-only rejection rate** — track in `fast_executions.reject_reason='post_only_would_cross'`. If >10% of attempts reject, the limit-tick offset is too aggressive. Surface in CC report.
4. **Fill latency vs signal half-life** — many imbalance signals decay in 100–500 ms. A 10-second timeout may catch a stale signal. Post-fill calibration table will surface this empirically.
5. **Partial fills** — `fast_executions.quantity` already supports them; verify the `exit_manager.py` reads partials correctly. Test #4 in the test plan above covers this.

## Acceptance criteria for live activation (NOT this brief)

This brief ships the code; it does NOT activate live trading on the new mode. Acceptance criteria for a future "flip the live switch" brief:

- 48h+ of decay rows in `fast_signal_decay_maker_filled` for at least 3 active pairs.
- Fill rate ≥ 25% on those 3 pairs.
- At least one (ticker, alert_type, score_bucket) cell shows `mean_return > 2 × maker_round_trip` with `sample_count ≥ 30`.
- Operator explicit "yes" via Cowork session.

## Push & deploy

- Multiple commits in a tight series (one per logical step, per CLAUDE.md). Don't bundle.
- After commit + push, restart `chili` + `fast-data-worker` to pick up the new executor path. Bind-mount means no rebuild.
- Do NOT restart `autotrader-worker` for this — different lane.
- Verify settings via `docker exec ... python -c "from app.services.trading.fast_path.settings import load; print(load())"` post-restart.

## Rollback plan

`git revert` the commits. Migration 232 is purely additive (no destructive ALTERs). Setting `CHILI_FAST_PATH_EXECUTION_MODE=taker` (the default) restores bit-identical pre-this-brief behavior. The new `fast_path_maker_attempts` table sits empty.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Limit-tick offset calibration.** Default is `best_bid + 1 tick` for long. If observed `post_only_would_cross` rejection rate is high (>10%), the offset should widen. Surface the rate and propose a per-pair adaptive widening if it's a real issue.
2. **`maker_cancel_on_timeout_s` default of 10s.** This is a guess based on imbalance signal half-life of 100–500 ms × 20× safety. If observed fill latency cluster is bimodal (some <2s, others 8–10s), the default should be tighter for short signals. Surface fill-latency histogram in CC report.
3. **Should `maker_first_then_taker` mode log a "would have been taker" line for the would-be-fallback trades that DID get filled by maker?** Helps the operator understand the cost trade-off without an A/B. Bookkeeping cost is small; surface if implementation isn't clean.
4. **`cost_aware_maker_fee_bps` default — please double-check the Coinbase fee schedule URL on the day of implementation.** Coinbase periodically updates the schedule; if the retail tier 1 maker is no longer 40 bps, the default needs to match the live schedule. Cite the URL fetched in the CC report.
