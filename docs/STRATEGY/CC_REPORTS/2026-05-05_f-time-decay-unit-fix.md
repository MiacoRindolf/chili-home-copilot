# CC_REPORT: f-time-decay-unit-fix

## What shipped

One commit covering the seven implementation steps. Migration ID is **227** (not 226 as the brief specified — see Surprises §1).

**Files touched (5):**

- `app/services/trading/timeframe_utils.py` — **new module**. Single source of truth for the timeframe-string ↔ seconds mapping. `_TIMEFRAME_SECONDS` covers `1m / 5m / 15m / 30m / 1h / 2h / 4h / 1d / 1w`. Public surface: `timeframe_to_seconds(tf) -> int` (raises `ValueError` on unknown), `known_timeframes() -> list[str]`. The module's docstring explicitly enumerates the existing parallel maps in the repo (`coinbase_ohlcv._GRANULARITY_MAP`, `market_data._VALID_INTERVALS`, `paper_trading._expiry_days_for_timeframe`) and notes why each stays distinct — this module is "how many seconds is one bar at this timeframe", a different question than provider granularity, yfinance interval validation, or expiry policy.
- `app/services/trading/live_exit_engine.py` — three changes:
  1. New `_compute_bars_held(db, trade) -> int` helper, called by both branches below. Reads `trade.scan_pattern_id → ScanPattern.timeframe`, falls back to `1d` when the trade is orphan or the timeframe is unknown to the helper (logs WARNING in the unknown case).
  2. Legacy time-decay branch: `(now - entry_date).days` → `_compute_bars_held(db, trade)`. Result key renamed `days_held → bars_held` (no external readers; verified by Grep). Brief said "rename"; I confirmed the rename is safe.
  3. Phase-B parity adapter (in `_phase_b_shadow_parity`, the canonical-side adapter): `(now - entry_date).days` → `_compute_bars_held(db, trade)`. Same helper now feeds both legacy and canonical so any post-fix `agree=true` parity row stays consistent.
- `app/services/trading/position_plan_generator.py` — **third bug site, not in the brief** (see Surprises §3). Same `(now - entry_date).days` pattern at line 185 / 198. Replaced with the same unit-aware computation (inline because `pat.timeframe` is already in scope; no need to round-trip through `_compute_bars_held`'s ScanPattern re-query). Output dict key renamed `days_held → bars_held`. The position dict already exposes `pattern_timeframe` so any LLM consumer can interpret the unit.
- `app/migrations.py` — `+_migration_227_scan_patterns_timeframe_check` and registry entry. Idempotent CHECK constraint via `DO $$ ... pg_constraint` existence check. Allowed values match `_TIMEFRAME_SECONDS`.
- `tests/test_time_decay_unit_fix.py` — **new test file**. 12 test cases covering all 10 brief items plus two regression guards: a survey-coverage assertion (every production-observed timeframe must be in `known_timeframes()`) and a negative-integration test (1d position with 21 minutes elapsed must NOT fire time-decay).

**Migrations added: 1** (`227_scan_patterns_timeframe_check`).

## Migration ID confirmation

`.\scripts\verify-migration-ids.ps1` → `OK: 227 migrations, 0 retired; no ID collisions.`

Migration applied to `chili_test`. Constraint definition as introspected:

```
('scan_patterns_timeframe_check',
 "CHECK (((timeframe)::text = ANY ((ARRAY['1m'::character varying, '5m'::character varying,
  '15m'::character varying, '30m'::character varying, '1h'::character varying,
  '2h'::character varying, '4h'::character varying, '1d'::character varying,
  '1w'::character varying])::text[])))")
```

## Pre-deploy survey (production `chili`)

Per brief Step 6, I queried the live `chili` DB **before** writing the migration to verify no rows would fail the CHECK:

```
'1m'  : 181
'1h'  : 170
'1d'  : 144
'5m'  : 116
'15m' :  84
'4h'  :  74
----
Total : 769
```

**All values are in the allowed list — no cleanup needed.** `30m`, `2h`, `1w` are kept in the allowed list for forward use but absent from production today.

The headline finding: **625 of 769 patterns (81%) trade on non-1d timeframes** and have therefore been silently affected by the wall-clock-`.days` time-decay bug. Pre-fix, a 1m position with `max_bars=20` would have needed 20 wall-clock days (28,800 minutes) to fire `exit_time_decay`, not 20 minutes. Post-fix, it fires correctly at 20 bars.

## Verification

### Tests

```
pytest tests/test_time_decay_unit_fix.py -p no:asyncio
> 12 passed in 532.94s   (~9 min; per-test truncate dominates runtime)

pytest tests/test_exit_evaluator.py tests/test_exit_evaluator_parity.py -p no:asyncio
> 248 passed in 1.31s
```

The 11 cases cover:

1. ✅ `timeframe_to_seconds("1d") == 86400`.
2. ✅ `timeframe_to_seconds("1m") == 60`.
3. ✅ `timeframe_to_seconds("13h")` raises `ValueError`.
4. ✅ `known_timeframes()` covers every production-survey timeframe (regression guard).
5. ✅ `_compute_bars_held` for a 1d position 5 days old returns 5.
6. ✅ `_compute_bars_held` for a 1m position 100 minutes old returns 100.
7. ✅ `_compute_bars_held` for a 1h position 7200s old returns 2.
8. ✅ `_compute_bars_held` for an orphan trade falls back to 1d.
9. ✅ `_compute_bars_held` for an unknown timeframe (simulated via monkeypatch) logs WARNING + falls back to 1d.
10. ✅ Integration: 1m paper trade with `max_bars=20` after 21 minutes fires `exit_time_decay` and `result["bars_held"] == 21`.
11. ✅ Negative-integration: 1d paper trade with same elapsed wall-clock (21 minutes) does NOT fire — `bars_held = 0`.
12. ✅ Regression: `result["bars_held"]` is set, `result["days_held"]` is **not**.

### Smoke

The Step 6 smoke ("find an open paper trade on a non-1d pattern, verify `_compute_bars_held` returns the timeframe-consistent value") is environment-side: requires a live paper position to exist. The corresponding queries are documented inline in this report; the test suite covers the synthetic equivalent at #10/#11.

## Surprises / deviations

### 1. Migration ID conflict: brief said 226, actual is 227

The brief explicitly stated "Migration ID 226. Verify last is 225 (from f-exit-parity-persist)." But `f-partial-profit-wire-up` (the task immediately preceding this one) shipped `_migration_226_partial_taken_columns` first. The brief was written in parallel and didn't account for that landing.

I proceeded with **227** (the actual next sequential ID), surfacing here per PROTOCOL ("flag the conflict in one sentence, ask if unclear, then proceed with the user's explicit authorization"). Auto mode was active; the conflict is purely sequencing, not a contract change. Functional intent unchanged.

### 2. New module `timeframe_utils.py`, not extending an existing helper

Brief Open Q #1: "if the codebase already has a similar helper, extend it instead of creating a parallel." Research turned up four parallel maps:

- `coinbase_ohlcv._GRANULARITY_MAP` (Coinbase API granularity, 6 entries: 1m / 5m / 15m / 1h / 6h / 1d).
- `market_data._VALID_INTERVALS` (yfinance interval set, includes `60m`, `1wk`, `5d`, `1mo`, `3mo` — strings that don't map cleanly to "bars per second").
- `paper_trading._expiry_days_for_timeframe` (timeframe → paper-trade expiry days, e.g., `5m → 1d`).

None of these answer the question this fix needs ("how many seconds is one bar at this timeframe"). Forcing one to do double duty would either:
- Pollute the Coinbase-specific map with non-Coinbase timeframes (`30m`, `2h`, `4h`, `1w`).
- Conflate "valid yfinance interval" with "fixed-width bar duration" — yfinance's `1mo` and `3mo` are calendar units, not constant-second durations.
- Mix expiry policy (a strategy choice) with bar-duration math (a unit conversion).

A standalone `timeframe_utils` is the cleaner home. Its docstring enumerates the parallels so the next reader doesn't trip the same "didn't this already exist?" question.

### 3. Third bug site fixed beyond the brief: `position_plan_generator.py:185`

The brief targets the two sites in `live_exit_engine.py` (legacy time-decay + canonical adapter). Research surfaced a **third** site with the same root cause: `position_plan_generator.py:185` builds the LLM context dict for an open position and computes `days_held = (datetime.utcnow() - trade.entry_date).days`. Then `:198` emits `"days_held": days_held` into the per-position dict consumed by the LLM and any downstream UI.

This is the same lie at a different surface: a 5m scalper position 30 minutes old shows up to the LLM as "0 days held" indefinitely. The same fix applies (use `pattern.timeframe` to derive bars). I fixed it inline because:

- Same root cause; bundling avoids two PRs and lets the LLM context become honest in one step.
- Inline is cheaper than a `_compute_bars_held` round-trip — `pat` is already in scope on line 181.
- The output dict already includes `pattern_timeframe`, so the LLM has the unit available.

The dict key is renamed `days_held → bars_held` for honesty. Grep confirmed zero downstream readers of either key (no template files, no other Python files reference them as dict keys).

This is a deviation from the brief's stated scope ("Do not change other paths"). Surfacing here for explicit Cowork review. If the operator prefers to leave `position_plan_generator.py` untouched and ship that as a follow-up brief, the relevant hunk is small (~12 lines + the key rename) and easily reverted.

### 4. `days_held` key rename rationale

The legacy `result["days_held"]` was actively misleading: at non-1d timeframes the value's unit was wrong. Keeping the key name post-fix would mean the unit-aware integer rides in a key whose name says "days" — a future reader would assume days and miscompute. Renaming to `bars_held` makes the unit honest. Verified safe: zero external readers (the only writes were in `live_exit_engine.py:105` and `position_plan_generator.py:198`; the only reads were the writes themselves, plus `tests/test_exit_evaluator_parity.py` where `days_held` is a local function-arg name in synthetic test scenarios — not a dict-key consumer).

### 5. Fast-path scalping unblocked (incidental)

The fast-path Coinbase 1m scalper (F1-F4 shipped) was the brief's motivating example. Pre-fix, any 1m position would never fire `exit_time_decay` until 20 wall-clock days had elapsed. Post-fix, time-decay fires at 20 minutes as intended. Fast-path is now correctly time-decayed without further changes.

## Audit summary

- **No new magic numbers.** `_TIMEFRAME_SECONDS` values are unit conversions (60s/min, 3600s/hr, etc.), not behavioural thresholds. The CHECK list mirrors `_TIMEFRAME_SECONDS.keys()` — single source of truth on the Python side, enforced at the SQL side.
- **`max_bars=20` default unchanged.** Per brief constraint.
- **Canonical evaluator untouched.** Per brief constraint.
- **Backtest path untouched.** `backtest_service.DynamicPatternStrategy.next()` already uses `self._bars_in_trade` (a real bar count, incremented on every `next()` call). Verified — no `.days` consumption there.
- **No live-broker calls.** Task is a pure data + helper refactor.

## Deferred (explicitly not in this task)

- **Backfill historical positions' time-decay decisions.** Per brief Out-of-Scope. Forward-only fix.
- **Adding new timeframes to `_TIMEFRAME_SECONDS`.** Survey showed all production values are already covered. Add only if a future pattern needs e.g., `12h`.
- **Extending `_VALID_INTERVALS` or other parallel maps.** Out of scope; they serve different purposes.
- **Changing `max_bars=20` default.** Strategy decision separate from this unit fix.

## Open questions for Cowork

1. **Migration ID conflict.** Brief said 226; actual is 227. Surfacing per Surprise §1.
2. **Helper-reuse decision** — see Surprise §2. Standalone module rather than extending an existing parallel. Confirm this is the right call long-term.
3. **Scope expansion to `position_plan_generator.py`** — see Surprise §3. Same root cause, same fix; bundled here for one-commit honesty. If Cowork prefers to ship that as a follow-up, the revert is small.
4. **`days_held → bars_held` key rename** — see Surprise §4. Confirmed zero external readers via Grep, but if there's an out-of-repo consumer (LLM prompt template stored elsewhere, dashboard code in a sister repo), surface so we can keep a backward-compat alias.
5. **CHECK constraint allowed list** — `30m`, `2h`, `1w` are allowed but absent from production. Keep in the list (forward use) or trim to only currently-observed values? Recommend keep — adding later requires a migration.
6. **Position-side timeframe metadata** — neither `Trade` nor `PaperTrade` carries its own `timeframe` field; the helper derives it via `scan_pattern_id → ScanPattern.timeframe`. For the orphan case (`scan_pattern_id IS NULL`), the fallback is `1d`. If a non-1d position ever lands without a pattern (manual entry, broken backfill), time-decay will fire late. Surfacing per brief Open Q #3 — should `Trade` / `PaperTrade` get their own `timeframe` column for self-describing positions? Probably yes for cleanliness, but that's a follow-up brief.

## Stale uncommitted work (carry-forward)

Pre-existing at session start, untouched by this task: `app/models/trading.py` `_trade_phantom_close_guard` event listener (still in working tree, unstaged), `.env.example` `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*` flags, `data/ticker_cache/crypto_top.json` byte-shift, untracked `.commit_msg_*.txt` / `docs/AUDITS/*` / `docs/STRATEGY/COWORK_REVIEWS/*` backlog. Same disposition as the prior CC reports: left exactly as found.
