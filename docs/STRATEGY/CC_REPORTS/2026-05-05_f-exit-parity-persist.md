# CC_REPORT: f-exit-parity-persist

## What shipped

One commit covering all six steps in the brief.

**Files touched (4):**

- `app/migrations.py` — `+_migration_225_exit_parity_strict_agree` (idempotent ADD COLUMN + CREATE INDEX) and registry entry.
- `app/models/trading.py` — `ExitParityLog.agree_strict_bool: Optional[bool]` + matching `ix_exit_parity_strict_agree_created` index in `__table_args__`.
- `app/services/backtest_service.py` — three changes:
  1. `_run_dynamic_pattern_slice` now injects `_ticker`, `_scan_pattern_id`, `_parity_sink: []` into the `type()` call. Without this the sink branch in `_phase_b_bt_shadow_parity` was a silent no-op (this is the actual reason 0 backtest rows landed; not the GC explanation in the brief — see Surprises).
  2. `_phase_b_bt_shadow_parity` now also writes `agree_strict_bool` (strict label equality) into the sink dict alongside the existing loose `agree_bool`.
  3. New `_drain_backtest_parity_sink(strat_cls, ticker)` helper, called immediately after `_bt_run_budget(bt, ...)` returns. Opens a fresh `SessionLocal`, computes `pnl_diff_pct` per row when both exit prices are present, `bulk_save_objects` + `commit`. Failures log + continue.
- `app/services/trading/live_exit_engine.py` — `_phase_b_shadow_parity` rewritten to:
  - Compute `pnl_diff_pct` inline from `legacy_exit_price` + `canonical_exit_price` (long-only sign).
  - Set `agree_strict_bool` (mirrors `agree_bool` for live, since live's existing `agree` was already strict).
  - Replace `db.add(row); db.flush()` with a fresh `SessionLocal` write so the parity insert commits independently of the caller's transaction (see Step 2 reasoning below).
  - Ops-log line now reads `position_id` from the kwargs dict instead of holding a transient ORM object.

**Migrations added: 1** (`225_exit_parity_strict_agree`).

## Migration ID confirmation

`.\scripts\verify-migration-ids.ps1` → `OK: 225 migrations, 0 retired; no ID collisions.`

Migration applied to `chili_test`; verified `agree_strict_bool BOOLEAN NULL` column + `ix_exit_parity_strict_agree_created` index exist.

## Verification

### Tests

```
pytest tests/test_exit_evaluator.py tests/test_exit_evaluator_parity.py -p no:asyncio
> 248 passed in 1.18s

pytest tests/test_backtest_metrics.py tests/test_backtest_param_sets.py tests/test_backtest_asset_scope.py -p no:asyncio
> 27 passed in 266.40s
```

(`-p no:asyncio` works around an unrelated `pytest-asyncio` plugin collection bug at `Package.obj` — not introduced by this task.)

### End-to-end write-path smoke

Wrote a temporary script that:
1. Synthesized a strategy class with a populated `_parity_sink` containing one agreement row (legacy 100.0 → canonical 101.0) and one disagreement row (legacy NULL → canonical 99.5), called `_drain_backtest_parity_sink`.
2. Wrote one live-shaped row through the same fresh-`SessionLocal` pattern the live hook uses.

Result:
```
{'source': 'backtest', 'ticker': 'SMOKE_BT', 'agree_bool': True,  'agree_strict_bool': True,  'pnl_diff_pct': 1.0,                'legacy_action': 'exit_trail', 'canonical_action': 'exit_trail'}
{'source': 'backtest', 'ticker': 'SMOKE_BT', 'agree_bool': False, 'agree_strict_bool': False, 'pnl_diff_pct': None,               'legacy_action': 'hold',       'canonical_action': 'exit_bos'}
{'source': 'live',     'ticker': 'SMOKE_LIVE','agree_bool': True,  'agree_strict_bool': True, 'pnl_diff_pct': 1.1904761904761905, 'legacy_action': 'exit_stop',  'canonical_action': 'exit_stop'}
OK -- 3 smoke rows written + cleaned
```

Smoke confirms: rows persist, `pnl_diff_pct` computes when both prices present and is correctly NULL when legacy_xp is None, `agree_strict_bool` round-trips on both sources. Smoke rows cleaned up.

### What still needs the operator post-deploy

Post-deploy success criteria #2/3/6 (real backtest + live-engine row counts, `dispatch-exit-parity-verdict.ps1` non-empty) require the brain-worker to actually run a FractionalBacktest cycle and the scheduler to evaluate at least one live position. Those are environment-side; this task ships the plumbing.

## Step-by-step mapping to brief

| Brief step | Where it landed |
|---|---|
| 1. Drain `_parity_sink` to DB | `_drain_backtest_parity_sink` + injection of `_parity_sink: []` / `_ticker` / `_scan_pattern_id` into `type()` |
| 2. Replace `db.flush()` with commit | Used the **fresh `SessionLocal`** branch (the brief's caveat path), not `db.commit()` — see reasoning below |
| 3. Add `agree_strict_bool` column | mig 225 + ORM column + populated on both paths |
| 4. Compute `pnl_diff_pct` at row creation | Inline in both `_phase_b_shadow_parity` (live) and `_drain_backtest_parity_sink` (backtest) |
| 5. Smoke verification | Tests + manual end-to-end script (cleaned up) |
| 6. CC report + audit summary | This file |

## Surprises / deviations

### 1. The brief's "_parity_sink is GC'd" theory is incomplete

The brief explained the 0-backtest-rows result as: "the strategy instance is garbage-collected and the list with it." That's not what was happening. The actual cause: **`_parity_sink`, `_ticker`, and `_scan_pattern_id` are accessed via `getattr(strategy, ..., None)` but were never set anywhere in the codebase** (`grep -n "_parity_sink\s*=" -r` returned zero matches outside the brief itself). So `getattr` always returned `None`, the `if sink is not None:` branch was dead, and nothing ever appended to anything. GC of an empty list isn't lossy.

This task fixes the actual root cause by injecting all three attributes via the `type()` call at the strat_cls construction site. The drain logic the brief asked for is still correct and necessary — it just wouldn't have helped without the attribute injection.

### 2. Live path: chose fresh `SessionLocal` over `db.commit()`

The brief's Step 2 caveat was the right call. `run_exit_engine(db)` is invoked from `_run_paper_trade_check_job` in `trading_scheduler.py:288-313`, which first calls `check_paper_exits(db)` — that path writes paper-trade closures. A `db.commit()` inside `_phase_b_shadow_parity` would prematurely flush those (and potentially partial state from any earlier pending work in the session), which is exactly the kind of side-channel commit semantic that bites later.

The fresh-`SessionLocal` write is invariant to caller transaction scope: the parity row commits whether or not the caller commits, and the caller's transaction is untouched. Mirror of what the backtest drain does. Trade-off: slightly more connection-pool pressure (one extra checkout per live evaluation), which is negligible at the live evaluation cadence.

### 3. Backtest sink-drain location: `_run_dynamic_pattern_slice`, not `FractionalBacktest.run`

The brief mentioned "drain at the end of `FractionalBacktest.run`" but `FractionalBacktest` is third-party (`backtesting.lib`) — we can't add a hook there. The next-best place is the call site immediately after `_bt_run_budget(bt, ...)` returns inside `_run_dynamic_pattern_slice`, which has both the `strat_cls` (with the populated sink as a class attribute) and proximity to the budget-exception path. That's where the drain landed. The named-strategy backtest at `run_named_strategy_backtest` doesn't use `DynamicPatternStrategy` so the parity hook never fires there — no drain needed.

### 4. Smoke script encountered a pytest plugin bug

`pytest-asyncio` 0.23.3 against `pytest` 9.0.2 raises `AttributeError: 'Package' object has no attribute 'obj'` at collection. Worked around with `-p no:asyncio`. Unrelated to this task; flagged as a maintenance item if `pytest` 9 vs `pytest-asyncio` 0.23 mismatch becomes painful in CI.

## Audit summary (rule-by-rule, per brief Step 6)

These are the substantive parity findings from the conversation that fed this brief. They are **not fixed by this task**; they are surfaced here so the cutover decision has the context.

### A. `trail_monotonic = False` in `build_config_backtest` is a deliberate parity choice

Legacy `DynamicPatternStrategy` does not enforce trailing-stop monotonicity in the backtest path (the trailing stop can move in any direction with the highest-since-entry / ATR product). Canonical `build_config_backtest` mirrors this with `trail_monotonic=False` so backtest parity holds.

**Cutover question for Cowork:** when `brain_exit_engine_mode=authoritative` is flipped, does monotonicity (`trail_monotonic=True`) turn on at the same time, or only later? Turning it on at the same time changes behavior on the cutover bar — defensible, but should be an explicit operator call.

Suggested default: leave `trail_monotonic=False` at cutover (preserve legacy semantics exactly), then promote to `True` in a follow-up phase with its own soak.

### B. `_resolve_trailing_atr_mult` brain-learner currently writes to a parameter that feeds nothing live

`live_exit_engine._resolve_trailing_atr_mult` adapts the ATR trailing multiple via `StrategyParameter`. It is **read** when computing `result["trailing_stop"]` for **reporting only** (`compute_live_exit_levels` populates `result["trailing_stop"]` but never sets `action="exit_trail"` on a trailing breach in the live path — only `exit_stop`/`exit_target`/`exit_time_decay`/`exit_bos` are reachable). And canonical `build_config_live` sets `trail_atr_mult=None`, so the canonical evaluator's trailing branch also ignores it.

So the learner is currently writing into a black hole. Not harmful, just dead.

**Cutover prep:** if cutover ALSO enables trail-close in live (`trail_atr_mult` non-None in `build_config_live`), the resolver should be wired into the cfg construction. Otherwise leave alone — wiring it on without enabling live trail-close is meaningless.

This is a follow-up brief, not part of this task.

### C. Time-decay unit mismatch (legacy vs canonical)

`compute_live_exit_levels` (legacy live) computes `days_held = (datetime.utcnow() - trade.entry_date).days` and compares to `max_bars`. Canonical `evaluate_bar` checks `state.bars_held >= max_bars`. The live shadow adapter passes `bars_held = days` into the canonical state. So both legacies are interpreting the same `max_bars` config value as "days" at intraday timeframes. Not a parity bug between legacy and canonical (they agree because the adapter erases the unit). It IS a real bug at intraday — but it's pre-existing in legacy, not introduced by canonical.

**No action this task.** Surface as a watch item: if the post-deploy verdict-query data shows time-decay exits firing at unexpected times (e.g., at market open instead of at bar N), this is the cause. Treat as a separate brief if it shows up in the data.

### D. `partial_profit_eligible` informational flag is dead

`compute_live_exit_levels` sets `result["partial_profit_eligible"] = True` when `r_move >= 1.0` and `partial_at_1r` is configured. **No code consumes that flag** (`grep` confirmed). Canonical evaluator does not set or read an analogous field. Not a parity concern; not a migration risk; just dead informational.

### E. Dual `agree_bool` definitions, fixed by `agree_strict_bool`

Live's `agree_bool` is computed as `legacy_action == canonical_action` — strict label equality.

Backtest's `agree_bool` is computed as `(legacy_action == canonical_action) or (legacy_action != "hold" and canonical_action != "hold")` — looser "both engines agreed to close" semantic that allows label mismatch.

Mixing definitions in one column makes any aggregate over `agree_bool` methodologically unsound (a live `agree_bool=True` rate is comparable to itself across time but not to a backtest `agree_bool=True` rate).

**This task adds `agree_strict_bool`** populated as `legacy_action == canonical_action` on both paths. Verdict queries that need a consistent definition across sources should filter `WHERE agree_strict_bool IS NOT NULL` and use that column.

The original `agree_bool` is preserved on both paths so prior analysis remains valid.

## Deferred (explicitly not in this task)

- **Flipping `brain_exit_engine_mode` to `authoritative`.** Per the brief, separate operator decision once 24-48h of verdict data exists.
- **Time-decay unit fix (audit point C).** Pre-existing; not a parity concern.
- **Wiring `_resolve_trailing_atr_mult` into `build_config_live` (audit point B).** Only relevant if cutover also enables live trail-close.
- **`trail_monotonic` cutover decision (audit point A).** Surfaced for cutover-time decision, not implemented now.
- **Cleaning up `partial_profit_eligible` dead flag (audit point D).** Cosmetic; not load-bearing.

## Open questions for Cowork

1. **`trail_monotonic` at cutover — same flip or staged?** Recommend staged: keep `False` at cutover to preserve legacy parity, promote to `True` in a follow-up phase. (See audit point A.)

2. **Verdict thresholds for cutover.** Brief suggested defaults (`agree_strict_pct >= 99.0%`, `pnl_diff_pct` mean within ±0.5%, `pnl_diff_pct` stddev < 1.0, ≥1k live + ≥100k backtest rows). Once 24-48h of post-deploy data exists, do these need adjusting based on what we actually see, or are they fine as-is?

3. **Stale uncommitted work in working tree.** Pre-existing (not from this task) at session start: `app/models/trading.py` carries an in-progress `_trade_phantom_close_guard` event listener; `.env.example` has new `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*` flags; `data/ticker_cache/crypto_top.json` is one-byte different. Plus a sizeable backlog of `.commit_msg_*.txt` and `docs/AUDITS/*.md` files. None of it is mine; I left it untouched. Cowork may want to know it's there before queuing the next task.
