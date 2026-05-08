# CC_REPORT: f-fastpath-maker-only-executor

## Outcome

Maker-only execution path lit up end-to-end behind the existing
`execution_mode=taker` default, so production behaviour at switchover
is bit-identical. New surfaces:

* `executor.py` — `_process_alert` dispatches on `settings.execution_mode`.
  `maker_only` and `maker_first_then_taker` route through
  `_process_alert_maker`, which places `post_only` limit orders one
  tick inside the spread, inserts a `fast_path_maker_attempts` row,
  schedules a cancel-on-timeout asyncio task, enforces a
  1-outstanding-order-per-(ticker, side) cap, and notifies the
  decay miner on fill.
* `decay_miner.py` — `record_maker_outcome` schedules the same
  8-horizon forward-return obs that `_handle_alert_inserted` does,
  but tagged `is_maker_filled=True` so the existing finalization
  loop dispatches them into `fast_signal_decay_maker_filled`. The
  taker-mode behaviour is unchanged.
* `coinbase_service.py` — `place_buy_order` / `place_sell_order`
  accept `post_only=False` (default); `cancel_order_by_id` wraps
  the SDK's batch-cancel for the timeout handler.
* `app/routers/trading_sub/fast_path_api.py` —
  `GET /api/trading/fast-path/maker-stats` aggregates last-24h
  per-pair fill rate from `fast_path_maker_attempts` and flags
  `fill_rate < 0.25` with `advisory: "uneconomic for maker-only"`.
* `supervisor.py` — `decay_miner` constructed before the executor
  so the executor receives a reference for `record_maker_outcome`.

**Test surface: 65/65 PASS in 2.27s.** 38 new helper-level tests
across 3 files, plus the 27 foundation tests still green.

## Per-step status

### Step 0 — Truncation scan — COMPLETE
Working copy intact. Zero TRUNCATED entries.

### Step 1 — `decay_miner.py` writer + `coinbase_service.py` post_only — SHIPPED (commit `b994373`)
* `_PendingObs` extended with `is_maker_filled: bool = field(compare=False, default=False)`.
* `_finalize_one_obs` dispatches: maker-filled obs go to a new
  `_welford_upsert_maker_filled` (writes to mig 232's
  `fast_signal_decay_maker_filled`); default obs go to the existing
  `_welford_upsert`. Existing taker-mode behaviour is bit-identical.
* `record_maker_outcome(...)` is the public entry point the executor
  calls when a maker order's outcome is known. For `filled` / `partial`
  it schedules the same 8-horizon forward-return obs that
  `_handle_alert_inserted` would; for `cancelled` / `replaced` /
  `rejected` it's a no-op (fill rate is sourced from
  `fast_path_maker_attempts`, not the miner).
* `_DecayMetrics` extended with 4 new counters; surfaced in `stats()`.
* `coinbase_service.place_buy_order` / `place_sell_order` accept
  `post_only=False`; when True, prefer the SDK's
  `limit_order_gtc_buy_post_only` / `_sell_post_only` variant via
  `getattr(client, ..., None)`, fall back to passing `post_only=True`
  as a kwarg.
* `cancel_order_by_id(order_id)` is the new thin wrapper around
  `client.cancel_orders([order_id])` returning `ok`/`error` shape.
* Post-edit: `wc -l decay_miner.py` 914 → 1062 (+148);
  `wc -l coinbase_service.py` 837 → 904 (+67); both AST clean.

### Step 2 — `executor.py` maker-only path + supervisor wiring — SHIPPED (commit `381e151`)
* Module-level helpers behind a new
  `# ── Maker-only execution helpers ─` banner:
  * `MAKER_LIMIT_TICK_FRACTION_OF_MID = 1e-4` (1bp).
  * `_maker_default_tick_size(mid)` — fallback when no
    `quote_increment` available.
  * `_compute_maker_limit_price(side, best_bid, best_ask, tick)` —
    one tick inside the spread, **never crosses** (returns 0.0 if
    the offset would invert the book).
  * `_place_coinbase_maker_order_live` — same authorization belts
    as `_place_coinbase_order_live`, but calls
    `cb.place_buy_order(..., order_type='limit', post_only=True)`.
  * `_cancel_coinbase_order_live(order_id)`.
* `FastPathExecutor.__init__` accepts `decay_miner: Any | None = None`
  and tracks `_outstanding_maker[(ticker, side)]` for the
  1-outstanding cap. `_ExecutorMetrics` extended with maker-attempt
  placed/filled/cancelled/replaced/rejected/capped counters.
* `_process_alert` now first reads
  `(self._settings.execution_mode or "taker").strip().lower()` and
  delegates to `_process_alert_maker(...)` for `maker_only` /
  `maker_first_then_taker`. The taker branch is unchanged and
  bit-identical at the default mode.
* `_process_alert_maker` enforces the cap, computes a limit price,
  places (paper synthesises; live calls SDK with `post_only=True`),
  inserts a `fast_path_maker_attempts` row, writes the decision
  row, schedules `_maker_timeout_handler` with timeout =
  `maker_cancel_on_timeout_s` (or `maker_first_taker_fallback_s` in
  hybrid mode).
* `_maker_timeout_handler` resolves the outcome: live polls broker
  for terminal state and cancels via SDK if unfilled; paper peeks
  the in-memory book and treats a book-cross past the limit as
  filled. UPDATE on `fast_path_maker_attempts`, notify
  `decay_miner.record_maker_outcome` on fill/partial, drop the
  (ticker, side) entry from the cap.
* `stats()` surfaces `execution_mode` plus all 6 maker-attempt
  counters and `maker_outstanding_count`.
* `supervisor.py`: `decay_miner` constructed *before* the executor
  so the reference can be passed in. Startup order
  (executor.start → exit_manager.start → decay_miner.start) is
  unchanged; only construction order moved.
* Post-edit: `wc -l executor.py` 702 → 1320 (+618); AST clean.

### Step 3 — `maker_first_then_taker` taker fallback — SHIPPED (commit `e12142e`)
When the maker leg's outcome resolves to `replaced`, a sibling taker
fires before the (ticker, side) cap is dropped (so a fresh maker
can't race the fallback through the cap).

* New method `_taker_fallback_after_maker_replaced(...)` re-reads
  top-of-book (the book may have moved during the maker wait),
  then routes to live/paper exactly like `_process_alert`'s taker
  branch: live applies the same authorization belts and broker-error
  handling; paper synthesises at best ask (long) / best bid (short).
  Updates `_open_positions` and `_daily_notional_used_usd` so the
  taker path's accounting invariants hold.
* `_maker_timeout_handler` invokes the fallback when
  `attempt['execution_mode'] == 'maker_first_then_taker'` and
  `outcome == 'replaced'`.
* Post-edit: `wc -l executor.py` 1320 → 1460 (+140); AST clean.

### Step 4 — `GET /api/trading/fast-path/maker-stats` — SHIPPED (commit `347ad4f`)
* New read-only endpoint surfacing last-24h per-pair fill rate.
* Reads aggregated outcomes from `fast_path_maker_attempts` (the
  executor's INSERT-on-place + UPDATE-on-resolve), so no executor
  IPC required.
* Pairs with `fill_rate < 0.25` (`MAKER_FILL_RATE_UNECONOMIC_THRESHOLD`)
  get an `advisory: "uneconomic for maker-only"` flag for operator
  guidance during soak.
* Response shape: `settings`, `window_hours`, `totals`, `per_pair`
  (capped at 100, ordered by attempts DESC).
* Post-edit: `wc -l fast_path_api.py` 644 → 793 (+149); AST clean.

### Step 5 — Tests — SHIPPED (commit `4ed74f2`)
**38 new helper-level tests across 3 files. Combined with the
foundation 27 = 65/65 PASS in 2.27s.**

`tests/test_fastpath_maker_executor.py` (13 tests):
* Pure helpers: `_compute_maker_limit_price` for buy/sell,
  no-cross guard, no-quotes guard, unknown-side guard;
  `_maker_default_tick_size` scaling.
* Mode dispatch: taker is bit-identical (zero maker artefacts).
* 1-outstanding cap rejects duplicate signal at same (ticker, side).
* Happy path: limit inside spread, attempt row inserted, decision
  row written.
* Cancel-on-timeout (paper): book unchanged → cancelled;
  book crossed → filled + decay-miner notified.
* Hybrid: replaced + sibling paper taker fills.
* Live placement passes `post_only=True` to the broker stub.

`tests/test_fastpath_maker_decay_writer.py` (15 tests):
* `is_maker_filled` defaults False.
* `record_maker_outcome` schedules 8 horizons with flag True only
  on `filled` / `partial`; no-op for `cancelled` / `replaced` /
  `rejected` / unknown.
* Defensive guards reject malformed inputs.
* Heap cap enforced.
* `_finalize_one_obs` dispatches correctly on the flag.
* `stats()` exposes maker counters.

`tests/test_fastpath_maker_status_endpoint.py` (10 tests):
* All 4 maker settings present.
* Per-pair shape matches brief.
* `fill_rate < 0.25` → advisory; `>= 0.25` → null.
* Multi-pair totals aggregate.
* Empty result set zeroed.
* DB exception surfaces as `ok: false` (not a 500).

Run command:
```
pytest -p no:asyncio tests/test_fastpath_maker_executor.py \
  tests/test_fastpath_maker_decay_writer.py \
  tests/test_fastpath_maker_status_endpoint.py -v
```

The `-p no:asyncio` workaround is the same one
`tests/test_bracket_writer_cover_policy_clarify.py:17` uses for the
pre-existing pytest-asyncio Package-collection bug. Without it,
pytest collection fails with `AttributeError: 'Package' object has no
attribute 'obj'` — a known plugin issue, unrelated to this brief.

### Step 6 — DB-bound runtime soak — DEFERRED (operator-side)
Acceptance criteria 2 + 3 (`fast_path_maker_attempts` rows accumulate
during soak; `fast_signal_decay_maker_filled` Welford rows accumulate)
require:
1. Coinbase egress recovery (currently 100% blocked; environmental
   per the rotator-http-retry CC report).
2. Operator flips `CHILI_FAST_PATH_EXECUTION_MODE=maker_only` in
   `.env` after the rotator populates rows for ~24h.
3. ~48h paper soak.

Per the brief's "Operator-side after CC ships" section.

## Magic-number audit

**Net new operator-tunable knobs:** zero (the four maker settings
already shipped in the foundation layer; this brief reused them).

**Net new internal constants:**
* `MAKER_LIMIT_TICK_FRACTION_OF_MID = 1e-4` (1bp) — default tick
  offset when `quote_increment` isn't sourced from venue metadata.
  Inline doc explains the rationale: 1bp is small enough that the
  resting limit consistently lands inside the spread for any
  reasonable Coinbase pair (median spread is well above 1bp), but
  not so small that it collides with the BBO. Settings-tunable
  via a follow-up brief if operator needs override.
* `MAKER_FILL_RATE_UNECONOMIC_THRESHOLD = 0.25` — brief-specified
  threshold for the `advisory: "uneconomic for maker-only"` flag
  in the status endpoint.
* `MAKER_STATS_WINDOW_HOURS = 24` — brief-specified window.
* `MAKER_STATS_PAIR_LIMIT = 100` — payload-size guard for the
  operator-facing endpoint.

These are policy constants, not tuning knobs. Lifting any of them
into `settings.py` is a follow-up if the operator needs runtime
override.

## Surprises / deviations

1. **`fill_rate` per-cell column NOT added.** The brief's Step 3
   said "fill_rate column populated as N filled / N attempted per
   cell" but mig 232 didn't include such a column on
   `fast_signal_decay_maker_filled`. The brief's "no new migration
   needed for this brief" took precedence; per-cell fill rate is
   redundant with the `fast_path_maker_attempts` aggregation that
   the status endpoint reads directly. If a future brief wants the
   per-cell denominator on the decay table itself (for offline
   replay), add it via mig 233 + an executor write at outcome
   resolution. Not load-bearing for the maker-only soak.

2. **`pytest-asyncio` plugin collection bug.** The new test files
   don't use `@pytest.mark.asyncio` — they invoke `asyncio.run(...)`
   directly — but the plugin still mishandles collection in the
   `chili-env` config. Workaround `-p no:asyncio` is documented in
   the test commit message and matches existing precedent
   (`tests/test_bracket_writer_cover_policy_clarify.py:17`). Not
   load-bearing.

3. **Live mode SDK post_only variant probing.** The Coinbase
   Advanced Trade Python SDK exposes the post-only path differently
   across versions: some have a dedicated
   `limit_order_gtc_buy_post_only` method, others accept
   `post_only=True` as a kwarg on `limit_order_gtc_buy`. The
   adapter probes for the dedicated method via `getattr(client,
   ..., None)` and falls back to the kwarg path; no SDK version pin.
   Live verification deferred to operator soak.

4. **Paper-mode book-cross simulation choice.** Defensible
   simulation: at timeout, peek the in-memory book; if both
   `best_bid` AND `best_ask` have moved past the limit (a real
   cross, not just a touch), record `filled` at limit price.
   Otherwise `cancelled`. This avoids over-counting "touch"
   events that wouldn't have actually filled a maker resting at
   the limit. Per Open Q #4 in the brief — surfaced as the
   chosen approach.

## Open questions (carried forward from brief)

1. **Operator's actual Coinbase volume tier.** Foundation brief
   asked; this brief inherited. Unanswered. Default
   `cost_aware_maker_fee_bps=40.0` (tier 1) holds until the
   operator confirms.
2. **`maker_first_then_taker`'s value-add.** The hybrid mode is
   shipped end-to-end (placement + replaced-on-timeout + sibling
   taker). It adds ~140 lines of executor code beyond pure
   maker_only. Not overengineered per se, but it needs operator
   soak to validate that the 5-second maker window adds enough
   fill-rate headroom to justify the additional state. **Recommend
   defaulting `maker_only` (not hybrid) on the first soak**, then
   compare hybrid against pure maker_only on a second soak.
3. **Tick-size sourcing.** Currently the `mid * 1e-4` fallback is
   used unconditionally. Coinbase product metadata exposes
   `quote_increment` per pair; lifting this into
   `_compute_maker_limit_price` is a 10-line follow-up that requires
   a fresh metadata fetch (likely cached in the rotator).
   **Surfaced as a follow-up brief candidate.**
4. **Cancel-on-timeout implementation.** Went with one
   `asyncio.create_task` per outstanding order. Bounded by the
   1-outstanding cap (+1 task max in steady state per ticker × side
   in hybrid mode), so memory is fine. Per-tick scan would have
   tighter cancellation semantics but adds a polling loop the
   executor doesn't need.
5. **Retry policy reuse from `universe_rotator`.** Not reused yet.
   The maker placement path's `_place_coinbase_maker_order_live`
   calls `cb.place_buy_order` directly without a retry layer. If
   the same Docker NAT TCP-drop pattern affects placement (it
   shouldn't — the rotator is the only known affected surface),
   lifting `_HTTP_RETRY_BACKOFFS_S` into a shared helper is a
   follow-up. **Surfaced as a follow-up brief candidate; not
   load-bearing for the soak.**

## Verification

* 65/65 helper-level tests PASS in 2.27s.
* All edited files AST clean, splice pattern used (NOT Edit tool)
  for `executor.py` (1460 lines), `decay_miner.py` (1062 lines),
  `coinbase_service.py` (904 lines), `fast_path_api.py` (793 lines).
  Supervisor used a single small Edit (35 lines reordered).
* Post-edit `wc -l`:
  * `executor.py`: 702 → 1460 (+758 across 2 splices)
  * `decay_miner.py`: 914 → 1062 (+148)
  * `coinbase_service.py`: 837 → 904 (+67)
  * `fast_path_api.py`: 644 → 793 (+149)
* Truncation-class regression check: zero TRUNCATED entries pre-
  and post-edit.

## Operator-side after CC ships

Per brief:
1. `git pull` on the operator's box.
2. Truncation scan (mandatory):
   ```powershell
   python -c "import subprocess,ast,os; mod=subprocess.check_output(['git','diff','--name-only','HEAD','--','*.py']).decode().strip().split('\n'); [print(f'TRUNCATED {f}') for f in mod if f and os.path.exists(f) and (lambda h,d: d.count(chr(10))<h.count(chr(10))*0.95)(subprocess.check_output(['git','show',f'HEAD:{f}']).decode('utf-8','replace'),open(f,encoding='utf-8',errors='replace').read())]"
   ```
3. `docker compose up -d --force-recreate chili scheduler-worker fast-data-worker`.
4. **Wait for Coinbase egress to recover** (currently blocked;
   intermittent recovery expected). Verify with the curl-from-
   container snippet from the brief.
5. Once shadow rows accumulate (~24h+ post-egress recovery), flip
   `CHILI_FAST_PATH_EXECUTION_MODE=maker_only` in `.env` (re-up).
6. After 48h of maker-only paper soak, evaluate per-pair fill rate
   via `GET /api/trading/fast-path/maker-stats`; pairs flagged with
   `advisory: "uneconomic for maker-only"` (fill_rate < 0.25) get
   dropped from the universe.

## Rollback plan

`git revert` the 5 commits in reverse:
```
4ed74f2 test(fast-path): maker-only executor + decay writer + status endpoint
347ad4f feat(fast-path): GET /api/trading/fast-path/maker-stats endpoint
e12142e feat(fast-path): maker_first_then_taker — taker fallback after maker_replaced
381e151 feat(fast-path): executor maker-only path + supervisor wiring
b994373 feat(fast-path): decay-miner maker-filled writer + coinbase post_only support
```

Setting `CHILI_FAST_PATH_EXECUTION_MODE=taker` (the default)
restores prior behavior; the maker-side code is dormant. The
maker tables (mig 232) stay; they're additive and harmless.
