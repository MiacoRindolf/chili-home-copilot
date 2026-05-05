# CC_REPORT: f-partial-profit-wire-up

## What shipped

One commit covering all nine implementation steps in the brief.

**Files touched (6):**

- `app/migrations.py` — `+_migration_226_partial_taken_columns` and registry entry. Adds the four `partial_taken_*` columns to BOTH `trading_trades` and `trading_paper_trades`, sparse partial indexes on `WHERE partial_taken = TRUE`, and widens `trading_paper_trades.quantity` from `INTEGER` to `DOUBLE PRECISION` so a fractional partial is representable on the typical `quantity=1` paper trade.
- `app/models/trading.py` — four new ORM columns on both `Trade` (live) and `PaperTrade` (paper); `PaperTrade.quantity` widened `Integer → Float`. `Trade.quantity` was already `Float`.
- `app/services/trading/live_exit_engine.py` — three changes:
  1. `_load_exit_config` defaults dict gains `partial_close_fraction: 0.5`. Per-pattern override via the existing `ScanPattern.exit_config` JSONB. No new schema.
  2. The dead `partial_profit_eligible` block in `compute_live_exit_levels` (Grep: 1 producer, 0 readers) is replaced with an active `result["action"] = "partial"` emission gated on `partial_at_1r=True ∧ not trade.partial_taken ∧ result["action"] == "hold"` (priority discipline preserves stop > target > BOS > time_decay > partial). Block was also moved AFTER BOS so it correctly sees terminal exits before deciding.
  3. `run_exit_engine` now returns separate `actions` (terminal) and `partial_actions` (non-terminal) buckets. The `actions` key keeps its legacy meaning so existing consumers don't change behaviour.
- `app/services/trading/paper_trading.py` — new `place_partial_close(db, trade, fraction, *, current_price=None)`. Reduces `trade.quantity` by the fraction, populates the four `partial_taken_*` columns, applies paper slippage on the fill price (mirror of full closes), commits. Refuses `live_partial_not_yet_supported` for `Trade` (live) instances, `already_partialed` if the bit is set, and `invalid_fraction:N` outside `(0, 1)`. Lives in `paper_trading.py` not `broker_service.py` because the brief constrains this task to paper-only — see Surprises.
- `app/services/trading_scheduler.py` — `_run_paper_trade_check_job` (the only consumer of `run_exit_engine` in the repo) now iterates `partial_actions`, calls `place_partial_close`, and emits a `[partial_profit_ops]` log line on success (plus a WARNING on failure). Terminal-action handling stays unchanged.
- `tests/test_partial_profit_wire_up.py` — 10 cases from the brief (4 of which expand to 4 parametric sub-cases under #8, total **13 test executions**).

**Migrations added: 1** (`226_partial_taken_columns`).

## Migration ID confirmation

`.\scripts\verify-migration-ids.ps1` → `OK: 226 migrations, 0 retired; no ID collisions.`

Migration applied to `chili_test`. Schema check post-apply:

```
('trading_paper_trades', 'partial_taken',       'boolean')
('trading_paper_trades', 'partial_taken_at',    'timestamp without time zone')
('trading_paper_trades', 'partial_taken_price', 'double precision')
('trading_paper_trades', 'partial_taken_qty',   'double precision')
('trading_paper_trades', 'quantity',            'double precision')   # widened from integer
('trading_trades',       'partial_taken',       'boolean')
('trading_trades',       'partial_taken_at',    'timestamp without time zone')
('trading_trades',       'partial_taken_price', 'double precision')
('trading_trades',       'partial_taken_qty',   'double precision')
('trading_trades',       'quantity',            'double precision')
indexes: ['ix_trading_trades_partial_taken', 'ix_trading_paper_trades_partial_taken']
```

## Verification

### Tests

```
pytest tests/test_partial_profit_wire_up.py -p no:asyncio
> 13 passed in 1157.96s   (truncate-per-test on a large schema; not a regression)

pytest tests/test_exit_evaluator.py tests/test_exit_evaluator_parity.py -p no:asyncio
> 248 passed in 1.46s
```

The 13 cases cover:

1. ✅ `compute_live_exit_levels` emits `action="partial"` at 1R when configured + not yet partialed + no terminal pending.
2. ✅ `compute_live_exit_levels` returns `hold` when `partial_at_1r=False`.
3. ✅ `compute_live_exit_levels` returns the **terminal** action (here `exit_target` at 110.0) when both partial AND terminal would fire — priority discipline preserved.
4. ✅ `compute_live_exit_levels` returns `hold` (no re-fire) when `trade.partial_taken=True`.
5. ✅ `run_exit_engine` separates a 1R-hit ticker into `partial_actions` and a stop-blown ticker into `actions`. No cross-contamination.
6. ✅ `place_partial_close` happy path: `partial_taken=True`, four bookkeeping fields populated, `quantity` reduced.
7. ✅ `place_partial_close` returns `{"error": "already_partialed"}` on second call.
8. ✅ `place_partial_close` returns `{"error": "invalid_fraction:..."}` for `0.0`, `1.0`, `-0.1`, `1.5` (parametric × 4).
9. ✅ End-to-end: `run_exit_engine → partial_actions → place_partial_close` with a synthetic 1R-hit paper trade. Trade ends at `partial_taken=True`, `quantity=5.0` (was 10.0), `status='open'`. Re-running `run_exit_engine` does NOT re-fire.
10. ✅ `partial_profit_eligible` is no longer set on the result dict. (Sanity guard against the dead flag drifting back.)

### Smoke setup (deferred to deploy; environment-dependent)

Per brief Step 9, a real partial fire requires a live paper position to reach 1R **after** deploy. That's environment-side; cannot be guaranteed in this session. The setup query is documented inline for the operator:

```sql
UPDATE scan_patterns
   SET exit_config = jsonb_set(
       coalesce(exit_config, '{}'::jsonb),
       '{partial_at_1r}', 'true'::jsonb
   )
   WHERE id = <chosen_pattern_id>;
```

After deploy, watch for `[partial_profit_ops] position_id=... fraction=0.50 r_multiple=...` log lines and verify with:

```sql
SELECT id, ticker, quantity, partial_taken, partial_taken_qty,
       partial_taken_price, partial_taken_at
FROM trading_paper_trades WHERE partial_taken = TRUE LIMIT 5;
```

Note: brief's example used table name `paper_trades`; actual is `trading_paper_trades` (see Surprise #1).

## Surprises / deviations

### 1. Brief table name mismatch: `paper_trades` → `trading_paper_trades`

The brief's Step 1 SQL targeted `paper_trades`. Actual table name is `trading_paper_trades` (`PaperTrade.__tablename__`, `app/models/trading.py:1025`). Migration uses the correct name. No-op for downstream consumers; flagging because Cowork-authored briefs may keep using the wrong name in future SQL snippets.

### 2. `PaperTrade.quantity` was Integer; widened to Float

The brief assumed quantity was already float-capable ("reduces position quantity by the fraction"). It wasn't — `PaperTrade.quantity = Column(Integer, default=1)`. With `quantity=1` (the default) and `fraction=0.5`, integer math produces `0`, which is meaningless.

Migration 226 widens to `DOUBLE PRECISION`. Verified via Grep that `PaperTrade.quantity` is only used multiplicatively (P&L, commission); no `range()`, no int-specific ops. Existing rows promote losslessly (`1 → 1.0`).

`Trade.quantity` (live) was already Float — no change there.

### 3. No paper-balance ledger to credit

The brief said "credits the partial proceeds to the paper-balance ledger." Research confirmed there is **no separate paper-balance / paper-cash ledger** in the system (Grep for `paper_balance`, `PaperAccount`, `paper_ledger` — zero hits). Closing a paper trade goes through `_paper_close_ledger` → `on_paper_trade_closed` (an audit hook for the neural-mesh feedback loop), and `pnl` is computed/stored directly on the `PaperTrade` row at close time.

For the partial fill, I followed the same pattern: mutate the row in-place (reduce `quantity`, set the four `partial_taken_*` columns), apply paper slippage on the fill price, commit. **No ledger to credit.** The `partial_taken_*` columns are the audit trail; the eventual full close on the remaining quantity will compute its own `pnl` against the smaller `quantity` correctly.

If a paper-balance ledger is added later (e.g., for cash accounting), the partial-close path will need an entry there too. Surfacing for Cowork's awareness; not a blocker now.

### 4. `place_partial_close` lives in `paper_trading.py`, not `broker_service.py`

The brief Step 6 suggested `broker_service.place_partial_close`. Research showed `broker_service.py` is exclusively live-broker primitives (Robinhood / Coinbase API calls). Putting paper-mode logic there would mix concerns and violate the existing module split.

Decision: paper-mode `place_partial_close` lives in `app/services/trading/paper_trading.py` next to `_close_paper_trade` and `check_paper_exits`. When live-mode partials get wired (separate brief, behind the fast-path safety belts), a `place_partial_close` live wrapper can be added to `broker_service.py` that delegates to the existing `place_sell_order(ticker, partial_qty)` (which already accepts a quantity parameter — no new broker primitive needed; verified by Grep).

For now, calling `place_partial_close` with a `Trade` instance (live) returns `{"ok": False, "error": "live_partial_not_yet_supported"}`. This is intentional and tested.

### 5. Partial emission moved AFTER BOS in `compute_live_exit_levels`

The brief's pseudocode put the partial block at the same position the legacy `partial_profit_eligible` block lived (after target check, before time_decay/BOS). But the gate `result["action"] == "hold"` then runs before time_decay and BOS would have set their actions, so a position that hits 1R AND breaks structure on the same bar would emit `partial` instead of the (correct) `exit_bos`.

Fix: moved the partial emission to AFTER the BOS block (line ~129 area), still BEFORE the parity hook. Now `result["action"] == "hold"` is evaluated against ALL terminal rules, and partial only fires when truly nothing terminal would. Test #3 (`test_compute_live_exit_levels_terminal_preempts_partial`) validates this.

### 6. `run_exit_engine` filter for terminal vs partial

The legacy filter `r.get("action") != "hold"` would treat `"partial"` as terminal. New filter splits explicitly:

```python
terminal_actions = [r for r in results if r.get("action") not in ("hold", "partial")]
partial_actions = [r for r in results if r.get("action") == "partial"]
```

Backward-compat preserved: `actions` still means terminal closes only; `partial_actions` is a new key.

## Audit summary

- **`partial_profit_eligible` is gone.** Producer at `live_exit_engine.py:99-103` removed; replaced with active `action="partial"` emission. Grep across the repo before deletion confirmed zero readers (only producer + docs/COMMIT_EDITMSG mentions). Test #10 guards against drift.
- **`PositionState.partial_taken`** in `exit_evaluator.py:116` already existed and the canonical emission at `:358-361` already gates on it (`if config.partial_at_1r and not state.partial_taken`). Brief was correct that this side was already wired; this task only added the consumer.
- **No new magic numbers.** `partial_close_fraction=0.5` is a config default, override-able per pattern via `exit_config`. `r_move >= 1.0` is the existing 1R definition (also literal in legacy line 101 of the pre-task file). No new behavioural numbers introduced.

## Deferred (explicitly not in this task)

- **Live-mode partial closes.** `place_partial_close` returns `live_partial_not_yet_supported` for `Trade` instances. Live wiring needs the fast-path safety-belt review (PROTOCOL Hard Rule 1) and a separate brief.
- **Multiple partials per trade** (e.g., 33% at 1R then 33% at 2R). Schema is single-flag (`partial_taken: bool`); a counter or a separate `trading_partial_fills` table would be needed. Out of scope per brief.
- **Brain-learner pattern selection for `partial_at_1r`.** Once realized partial-vs-full data accumulates, a separate brief can wire pattern-level adaptive selection.
- **Enabling `partial_at_1r=True` on any pattern.** Per brief, opt-in stays per-pattern via operator decision.
- **Paper-balance ledger** — see Surprise #3.

## Open questions for Cowork

1. **Brief assumed `paper_trades` table name** — wrong; actual is `trading_paper_trades`. Worth noting in the cookbook for future briefs.
2. **`PaperTrade.quantity` widened from Integer to Float.** Surfaced as Surprise #2. Existing data promotes losslessly; only multiplicative consumers; no behavioural risk identified. But it IS a schema type change and worth Cowork awareness in case some external script (out-of-repo) reads the column with a strict integer assumption.
3. **`place_partial_close` location**: I picked `paper_trading.py` because the brief's `broker_service.py` would mix paper logic into a live-only module. If Cowork wants a unified entry point that dispatches paper vs live, a thin wrapper in `broker_service.py` could be added later — flagged here so it's an explicit decision.
4. **No paper-balance ledger** — see Surprise #3. The audit trail lives in `partial_taken_*` columns + the eventual close's `pnl` field. If a paper cash ledger gets introduced separately, this code path will need an entry there too.
5. **Live-mode safety review for partials.** My read matches the brief's expectation: a partial is a SELL on an open position, allowed under existing fast-path belts. But live-mode is explicitly deferred and the helper refuses Trade instances; surfacing for explicit confirmation when the live brief is queued.
6. **Single partial per trade enforcement.** `partial_taken: bool` (not a counter) means a trade can partial exactly once. If a future pattern wants stacked partials, this needs schema rework. Surfacing per brief Open Question #5.

## Stale uncommitted work (carry-forward)

Pre-existing at session start, untouched by this task: `app/models/trading.py` `_trade_phantom_close_guard` event listener (still in working tree, unstaged), `.env.example` `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*` flags, `data/ticker_cache/crypto_top.json` byte-shift, untracked `.commit_msg_*.txt` / `docs/AUDITS/*` / `docs/STRATEGY/COWORK_REVIEWS/*` backlog. Same disposition as the prior CC report: left exactly as found.
