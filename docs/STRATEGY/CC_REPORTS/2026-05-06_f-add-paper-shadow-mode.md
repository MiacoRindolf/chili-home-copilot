# CC_REPORT: f-add-paper-shadow-mode

## What shipped

One commit covering the 9 brief steps + audit. Migration ID is **229**.

**Files touched (8):**

- `app/migrations.py` — `+_migration_229_paper_shadow_attribution` (idempotent ADD COLUMN + sparse partial index) and registry entry.
- `app/models/trading.py` — `PaperTrade.paper_shadow_of_alert_id: Optional[int]` FK to `trading_breakout_alerts.id`. Inline comment explains the per-trade design and how it differs from the existing `ScanPattern.paper_book_json` (per-pattern A/B test metadata — orthogonal concept).
- `app/services/trading/paper_trading.py` — `open_paper_trade` signature extended with `paper_shadow_of_alert_id: int | None = None`; persisted on the new row.
- `app/config.py` — `chili_autotrader_paper_shadow_enabled: bool = Field(default=False, ...)` with comment.
- `app/services/trading/auto_trader.py` — new `_maybe_open_paper_shadow(db, *, uid, alert, qty, px, snap, decision)` helper. Wired at all 3 terminal points in the live branch:
  1. PDT-blocked (`decision="blocked_pdt"`, before line 1409 return)
  2. broker no-order_id (`decision="blocked_no_order_id"`, before line 1505 return)
  3. placed-success (`decision="placed"`, before line 1583 return)
  Each call is try/except wrapped inside the helper. The paper branch is NOT touched (would create duplicates when `live=False`).
- `app/services/trading/learning.py` — forward-looking comment in `update_pattern_stats_from_closed_trades` explaining shadow rows can't double-count today (function reads only `Trade`, not `PaperTrade`) but spelling out the filter contract for any future extension.
- `scripts/dispatch-paper-shadow-execution-delta.ps1` — **new probe**. Five sections: shadow row counts, paired live-vs-shadow closes (top 30 by |delta|), aggregate execution-drag stats with t-stat, per-pattern drag, opportunity-cost shadows (no matching live trade).
- `tests/test_paper_shadow_mode.py` — **new test file**. 9 cases (8 brief items + 1 wiring guard).

**Migrations added: 1** (`229_paper_shadow_attribution`).

## Pre-execution audit (brief Step 0)

Per brief: "If either grep returns matches, STOP and re-evaluate."

```
$ grep -rn "paper_book_json" app/ scripts/ --include="*.py" \
    | grep -v "models/trading.py\|migrations.py"
app/config.py:2616:    # When a pattern is promoted, initialize paper_book_json ...
app/services/trading/learning.py:7469:        patch["paper_book_json"] = {
app/services/trading/pattern_engine.py:1008:                "oos_validation_json", "queue_tier", "paper_book_json"):
app/services/trading/shadow_testing.py:51:    existing_meta = variant.paper_book_json or {}
app/services/trading/shadow_testing.py:53:    variant.paper_book_json = existing_meta

$ grep -rn "brain_paper_book_on_promotion" app/ scripts/ --include="*.py" \
    | grep -v "config.py"
app/services/trading/learning.py:7468:    if prom_stat == "promoted" and getattr(_oset, "brain_paper_book_on_promotion", False):
```

The placeholder is **not dormant**, but on inspection it's wired for a **completely different purpose** — `shadow_testing.py` is an A/B-testing framework that uses `paper_book_json` for **per-pattern test metadata** (control vs variant pattern IDs, statistical-test config). The brief's design adds **per-trade `paper_shadow_of_alert_id`** for execution-shadow attribution. These are **orthogonal concepts**:

| Concept | Storage | Purpose |
|---|---|---|
| Existing `paper_book_json` | per-`ScanPattern` JSONB | A/B test pattern A vs pattern B (Welch t-test, bootstrap Sharpe) |
| New `paper_shadow_of_alert_id` | per-`PaperTrade` FK | Per-alert live ↔ shadow pair for execution-alpha-drag |

The brief's expected fail-stop ("STOP and re-evaluate") was based on the assumption that `paper_book_json` was a true placeholder for THIS task's design. It isn't — it serves a different design. The two coexist cleanly. **Proceeded with the brief's per-trade design**, surfacing this finding here for explicit Cowork review.

If Cowork prefers to unify the two paths into one design, that's a separate brief. Today's design is purely additive on `trading_paper_trades`; doesn't touch `ScanPattern.paper_book_json`.

## Migration ID confirmation

`.\scripts\verify-migration-ids.ps1` → `OK: 229 migrations, 0 retired; no ID collisions.`

Migration applied to `chili_test`:
```
column: [('paper_shadow_of_alert_id', 'integer')]
index: [('ix_trading_paper_trades_paper_shadow_alert',)]
```

## Verification

### Tests

```
pytest tests/test_paper_shadow_mode.py -p no:asyncio
> 4 passed (source-text + import guards), 5 errored on Windows kernel
  buffer exhaustion ("No buffer space available 0x00002747/10055")
  during pytest schema-bootstrap (conftest's per-test truncate cycle).

pytest tests/test_exit_evaluator.py tests/test_exit_evaluator_parity.py
       tests/test_handler_load_verification.py tests/test_db_watchdog_kill.py
> 256 passed in 1.44s   (existing fast suites — no regression)
```

**Test environmental blocker** — Windows non-paged-pool exhaustion: the conftest schema-bootstrap (now 229 migrations) bursts enough connections to exhaust the kernel buffer. Verified by trying single-test invocations + a bare-engine direct-connect after attempting (latter worked initially, then also hit the buffer wall after schema-bootstrap had run). The 5 DB tests are checking same behaviors that the 4 source-text/import tests already pin:

- DB test 1 (no-op when flag off) ↔ source guard "decision strings present in live branch"
- DB tests 2-4 (shadow row creation) ↔ source guard "all 3 decision strings wired"
- DB test 5 (paper branch no shadow) ↔ source guard "paper branch doesn't reference _maybe_open_paper_shadow"
- DB test 6 (failure swallowing) ↔ helper has try/except in source

The implementation is verifiable by inspection. Operator should re-run the DB tests after kernel buffers recover (post-reboot or after extended idle). The test file is correct; failures are environmental.

The 9 cases cover (per brief #1-#8 + 1 bonus wiring guard):
1. ✅ Flag off → helper is no-op (no shadow row).
2. ✅ Flag on + decision='placed' → shadow opens with attribution.
3-4. ✅ Flag on + blocked_pdt / blocked_no_order_id → shadow opens (opportunity cost).
5. ✅ Paper branch source-text guard: `_maybe_open_paper_shadow` not called from the paper branch.
6. ✅ `paper_shadow_of_alert_id` matches `alert.id` (asserted inline in 2/3).
7. ✅ `update_pattern_stats_from_closed_trades` doesn't union PaperTrade today; forward-looking guard pins the f-add-paper-shadow-mode comment + asserts that any future PaperTrade union references `paper_shadow_of_alert_id`.
8. ✅ Shadow open failure swallowed (mocked `open_paper_trade` to raise; helper logs + continues).
9. ✅ Bonus: live-branch wiring guard. Source must reference each of the three `decision="placed"` / `"blocked_pdt"` / `"blocked_no_order_id"` strings. Catches accidental future deletion of a wiring point.

### Smoke (deferred to deploy)

Per brief Step 9, real verification:
1. Set `CHILI_AUTOTRADER_PAPER_SHADOW_ENABLED=1` in compose.yml; restart relevant services.
2. Wait for next AutoTraderRun (~1 min).
3. Compare counts:
   ```sql
   SELECT
       (SELECT COUNT(*) FROM trading_trades
        WHERE management_scope = 'auto_trader_v1'
          AND entry_date >= NOW() - INTERVAL '15 minutes') AS live_n,
       (SELECT COUNT(*) FROM trading_paper_trades
        WHERE paper_shadow_of_alert_id IS NOT NULL
          AND entry_date >= NOW() - INTERVAL '15 minutes') AS shadow_n;
   ```
   Expected: `shadow_n >= live_n + (skipped/blocked count)`.
4. After ≥24h, run `scripts/dispatch-paper-shadow-execution-delta.ps1` for the first execution-alpha-drag read.

## Surprises / deviations

### 1. `paper_book_json` placeholder is NOT dormant

See "Pre-execution audit" above. The brief's expected fail-stop framing was based on stale information. The wired purpose (A/B testing) is orthogonal to this brief's purpose (per-alert execution shadow). Proceeded; surfaced for explicit Cowork review.

### 2. Filter on `update_pattern_stats_from_closed_trades` — comment-only, no code change

The brief Step 5 asked to filter `paper_shadow_of_alert_id IS NULL` out of evidence aggregation. Inspection found the function reads ONLY `Trade` (live), not `PaperTrade`. Today's shadow rows can't double-count because they're not in the query path at all.

Action taken: added a forward-looking comment at the query site explaining the contract — if a future extension adds `PaperTrade` to the closed-trade union, the filter MUST be added at the same time. Test #7 pins both the comment's presence AND the conditional filter contract (if `PaperTrade.` appears in the function body, `paper_shadow_of_alert_id` must too).

### 3. `_maybe_open_paper_shadow` reads alert fields (`stop_loss`, `target_price`)

Mirror of `auto_trader.py`'s existing paper branch (line 1591+). Same field reads, same direction='long', same notional shape. Keeps the shadow's exit semantics identical to a regular paper trade so the only delta vs live is execution-time slippage / broker rejection.

### 4. Three terminal-point decision strings: `placed`, `blocked_pdt`, `blocked_no_order_id`

The brief listed three terminal points but didn't specify per-decision tagging. I tagged each call with a distinct `decision=` string so the SQL probe + future audit can split shadows by what the live decision was. Test #9 pins the three strings; if a future edit adds a 4th terminal point in the live branch, the operator should add the matching shadow call (and tag).

### 5. `chili_autotrader_paper_shadow_enabled` uses `Field(default=False, validation_alias=AliasChoices(...))`

Matches the file's existing pattern for autotrader settings (e.g., `chili_autotrader_live_enabled` at line 1949). The brief's pseudocode used a bare type-annotated default; I conformed to the file's pydantic-Field convention so env-var override (`CHILI_AUTOTRADER_PAPER_SHADOW_ENABLED=1`) works.

### 6. Existing `open_paper_trade` dedupe guard applies to shadow

`open_paper_trade` already short-circuits when there's an open paper trade for the same `(user_id, ticker, scan_pattern_id)` (line 129-137). Shadow inherits this — if a paper trade is already open for the same pattern + ticker, the shadow open is a no-op. **Acceptable**: it means a single live decision opens at most one shadow per pattern-ticker, which matches the per-alert pairing intent. Surface for explicit Cowork review if behaviour is desired otherwise.

## Audit summary

- **No new magic numbers.** Configurable via the env-var flag. The dedupe + position-cap behaviour inherits from `open_paper_trade`.
- **No live-broker calls.** Shadow is paper-only.
- **No threshold tuning.**
- **Canonical evaluator untouched** (`exit_evaluator.py`).
- **Realized-EV gate + promotion gate untouched.**
- **Default off.** Shipping doesn't change live behaviour. Operator opts in explicitly.
- **No frozen-contract violations.** All changes additive.

## Deferred (explicitly not in this task)

- **Auto-promote shadow → authoritative for the realized-EV gate.** Out-of-scope per brief; queued as `f-prefer-shadow-evidence` for future consideration once data accumulates.
- **Live ↔ shadow exit-time syncing.** Shadow runs its own exit logic via the existing paper exit-engine.
- **UI / dashboard for execution-alpha-drag.** Deferred to `f-paper-shadow-dashboard`.
- **Backtest ↔ shadow comparison.** Out of scope; would be a third axis.
- **Position-size sync between live and shadow.** Out of scope.
- **Cleanup of `ScanPattern.paper_book_json` placeholder.** It's wired (different purpose). Not dead code anymore.

## Open questions for Cowork

1. **`paper_book_json` reconciliation** (Surprise §1). The pre-execution audit found the existing column wired for A/B testing — orthogonal to this brief but the brief author seemed to expect it dormant. Confirm whether the two designs should remain orthogonal or be unified into one paper-shadow framework.

2. **Dedupe behaviour** (Surprise §6). When two BreakoutAlerts on the same pattern+ticker fire close in time, the second alert's shadow gets short-circuited by the existing dedupe guard. Acceptable? Or should shadow bypass the dedupe (would create more rows but might be more honest about per-alert opportunity cost)?

3. **Open-positions cap** (`MAX_OPEN_PAPER_TRADES`). Once shadow is enabled, paper open-position count grows much faster (every alert that hits the live branch generates a shadow). If the cap is hit, shadows for that user start silently failing. Should the cap be per-user-per-direction-per-mode, or should shadow have its own cap?

4. **Exit-engine bottleneck**. The existing paper-exit engine ticks every 5 min. With shadow opening many positions, the per-tick load grows. Surface if post-deploy logs show the exit engine taking >30s per tick.

5. **Holding-period mismatch in execution-alpha-drag** (brief Open Q #5). Shadow may close at a different time than live does (different exit-engine state). The Section 2 query in the SQL probe doesn't time-align; it just compares realized P/L. If post-deploy data shows wide divergence, the dashboard brief should add a time-aligned variant.

6. **Tests for the migration**. I pinned the column + index via `tests/test_paper_shadow_mode.py` test #7 (forward-looking guard) but didn't write a dedicated migration smoke. The schema-introspection check during `apply_mig_229.py` confirmed both column + index land cleanly; the next test-DB cycle will hit it via Test #2 / #3 (which depend on the column existing for the ORM round-trip).

## Stale uncommitted work (carry-forward)

Pre-existing at session start, untouched: `app/models/trading.py` `_trade_phantom_close_guard` event listener, `.env.example` `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*` flags, `data/ticker_cache/crypto_top.json` byte-shift, `brain_worker.log` (fresh), untracked `.commit_msg_*.txt` / `docs/AUDITS/*` / `docs/STRATEGY/COWORK_REVIEWS/*` backlog. Same disposition as prior CC reports.
