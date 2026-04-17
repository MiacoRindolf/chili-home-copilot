---
name: Phase C - PIT hygiene + historical universe snapshot (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
overview: Introduce an explicit point-in-time (PIT) contract for mining-derived `ScanPattern.rules_json` conditions, audit every active pattern for non-PIT field use, and stand up a per-day historical universe snapshot table so backtests can ask "what tickers were tradable on date D?" Shadow mode first; a release blocker prevents authoritative cutover while violations exist.
status: completed_shadow_ready
phase_ladder:
  - off
  - shadow
  - compare
  - authoritative
depends_on:
  - phase_a_economic_truth_ledger (shadow-ready)
---

## Objective

Prevent subtle lookahead from polluting the miner and, eventually, NetEdgeRanker calibration. Concretely:

1. Define an explicit **PIT allowlist** of `ScanPattern` condition `indicator` names that are provably computed from data <= bar close.
2. Define an explicit **PIT denylist** (`future_return_*`, `forward_*`, `label_*`, `target_hit_*`, etc.).
3. Audit every active `ScanPattern.rules_json` against allowlist/denylist, write results to `trading_pit_audit_log`, and log a `[pit_audit]` one-liner per pattern.
4. Persist a per-day, per-ticker `trading_universe_snapshots` row (active/delisted/halted flag + primary exchange) so backtests and PIT audits can answer "was ticker T in our universe on date D?" without re-fetching from live sources.
5. Ship a release-blocker script that fails when any **active** pattern contains a non-PIT / unknown field in shadow mode, and treats any `mode=authoritative` audit line as a deployment leak during Phase C.

Legacy mining keeps running untouched. The audit is advisory this phase; nothing gets auto-quarantined. That is Phase J's job.

## Why now

- Phase A gives us a trusted realized-PnL stream. If mining uses non-PIT fields, the PnL stream is still honest but the mined pattern is useless — it only "worked" because the backtest peeked.
- Phase D (triple-barrier labels + economic promotion) depends on a clean separation between features (PIT) and labels (future). Phase C makes that boundary explicit and auditable.
- Phase J will later CUSUM-quarantine patterns by drift. We need the PIT contract in place first, otherwise Phase J quarantines symptoms instead of causes.

## Scope (what we change)

### 1. Canonical PIT sets: `app/services/trading/pit_contract.py` (new)

Pure module. No DB writes. No network.

```
ALLOWED_INDICATORS: frozenset[str]   # e.g. rsi_14, macd, macd_signal, macd_histogram, adx, bb_pct, bb_squeeze, atr, stochastic_k, stoch_bull_div, ema_stack, sma_20, sma_50, sma_100, sma_200, volume_ratio, vol_z_20, realized_vol_20, news_sentiment, news_count, regime, price, is_crypto
FORBIDDEN_INDICATORS: frozenset[str] # future_return_1d, future_return_3d, future_return_5d, future_return_10d, forward_return_*, tp_hit, sl_hit, triple_barrier_*, expected_pnl, realized_pnl, post_event_return, predicted_score (if read from *future* snapshot)
def classify(indicator: str) -> Literal["pit", "non_pit", "unknown"]
def classify_rules(rules_json: dict | str) -> dict  # {pit: [..], non_pit: [..], unknown: [..]}
```

`predicted_score` is allowlisted **only** when it is the model prediction stored alongside the same snapshot row (i.e. same bar_start_at). Because `MarketSnapshot.predicted_score` is written at snapshot time and never mutated with future data, it is PIT-safe for mining. Document this explicitly in the module docstring.

Extension policy: any new indicator field added to `trading_snapshots.indicator_data` must be declared in `pit_contract.py` before it can appear in a mined rule — the audit's `unknown` bucket surfaces regressions automatically.

### 2. Audit service: `app/services/trading/pit_audit.py` (new)

```
def audit_pattern(pattern: ScanPattern, *, db: Session) -> PitAuditResult
def audit_active_patterns(db: Session, *, lifecycle_stages: tuple[str, ...] = ("backtested", "validated", "challenged", "promoted", "live")) -> list[PitAuditResult]
def record_audit(db: Session, result: PitAuditResult) -> int  # returns pit_audit_log.id
```

`PitAuditResult` dataclass: `pattern_id`, `name`, `origin`, `lifecycle_stage`, `indicators_pit`, `indicators_non_pit`, `indicators_unknown`, `violation_count`, `agree_bool` (non_pit + unknown == 0).

Writes one row per audit pass to `trading_pit_audit_log`. Multiple passes per pattern are allowed — we keep a history.

### 3. Ops log: `app/trading_brain/infrastructure/pit_ops_log.py` (new)

Same shape as `ledger_ops_log.py`:

```
[pit_ops] mode=<off|shadow|compare|authoritative> pattern_id=<id> name="..." lifecycle=<stage> pit=<n> non_pit=<n> unknown=<n> agree=<true|false>
```

### 4. Historical universe snapshot: `trading_universe_snapshots` (migration 130)

Columns:
- `id BIGSERIAL`
- `as_of_date DATE NOT NULL`
- `ticker VARCHAR(32) NOT NULL`
- `asset_class VARCHAR(16) NOT NULL`  (`equity`, `crypto`, `etf`, …)
- `status VARCHAR(16) NOT NULL`  (`active`, `halted`, `delisted`, `unknown`)
- `primary_exchange VARCHAR(32) NULL`
- `source VARCHAR(32) NULL` (`massive`, `polygon`, `yfinance`, `manual`, `derived`)
- `provenance_json JSONB NULL`
- `created_at TIMESTAMP NOT NULL DEFAULT NOW()`
- `UNIQUE (as_of_date, ticker)`
- Indexes: `(as_of_date)`, `(ticker, as_of_date DESC)`.

No automatic backfill — Phase C only creates the table and one small writer helper `universe.record_snapshot(db, as_of_date, ticker, *, asset_class, status, primary_exchange, source, provenance)`. The existing daily cycles can start calling it opportunistically in later phases; Phase D / F / G will backfill meaningfully.

### 5. PIT audit log: `trading_pit_audit_log` (migration 130)

Columns:
- `id BIGSERIAL`
- `pattern_id INTEGER NOT NULL`  (logical FK; no cascade — keep history if pattern deleted)
- `name VARCHAR(200) NULL`
- `origin VARCHAR(32) NULL`
- `lifecycle_stage VARCHAR(32) NULL`
- `pit_count INTEGER NOT NULL`
- `non_pit_count INTEGER NOT NULL`
- `unknown_count INTEGER NOT NULL`
- `pit_fields JSONB NOT NULL DEFAULT '[]'`
- `non_pit_fields JSONB NOT NULL DEFAULT '[]'`
- `unknown_fields JSONB NOT NULL DEFAULT '[]'`
- `agree_bool BOOLEAN NOT NULL`
- `mode VARCHAR(16) NOT NULL`
- `created_at TIMESTAMP NOT NULL DEFAULT NOW()`
- Indexes: `(pattern_id, created_at DESC)`, `(agree_bool, created_at DESC)`.

### 6. Config (`app/config.py`)

```
brain_pit_audit_mode: str = "off"              # off | shadow | compare | authoritative
brain_pit_audit_ops_log_enabled: bool = True
```

No per-environment default flips in this PR. `.env` opt-in only.

### 7. Diagnostics endpoint (`app/routers/trading_sub/ai.py`)

`GET /api/trading/brain/pit/diagnostics?lookback_hours=24`

Returns:
```json
{
  "ok": true,
  "pit": {
    "mode": "shadow",
    "lookback_hours": 24,
    "audits_total": <n>,
    "patterns_audited": <n>,
    "patterns_clean": <n>,
    "patterns_violating": <n>,
    "top_violators": [{"pattern_id": ..., "name": "...", "non_pit_fields": [...], "unknown_fields": [...]}],
    "forbidden_hits_by_field": {"future_return_5d": <n>, ...},
    "unknown_hits_by_field": {"some_future_field": <n>, ...}
  }
}
```

### 8. Release blocker: `scripts/check_pit_release_blocker.ps1`

A line is a blocker if it contains BOTH:
- `[pit_ops]`
- `mode=authoritative`

AND at least one of:
- `agree=false`

Phase C is shadow-only, so any `mode=authoritative` line is treated as a deployment leak and fails the script even when `agree=true`. (Same rule as ledger.)

Also add a `--patterns-json <path>` dry-run mode that reads a JSON dump of the audit endpoint and fails if `patterns_violating > 0`, so CI can enforce it before the ladder advances.

### 9. Wiring — shadow hook

Shadow mode runs an on-demand audit via a new learning-cycle hook (no loop-breaking change):

- In `app/services/trading/learning.py` inside `run_learning_cycle` (or the single entry point that fires at cycle end; search for the function that already invokes `get_brain_stats`), add a try/except call:
  ```
  if settings.brain_pit_audit_mode != "off":
      try:
          from .pit_audit import audit_active_patterns, record_audit
          results = audit_active_patterns(db)
          for r in results:
              record_audit(db, r)
      except Exception:
          logger.debug("[pit_audit] shadow audit failed", exc_info=True)
  ```
- Any failure is swallowed. Legacy learning cycle output is unchanged.

### 10. Tests

`tests/test_pit_contract.py` and `tests/test_pit_audit.py`:

- `classify()` happy + edge cases (allow / deny / unknown).
- `classify_rules()` on a synthetic `{"conditions": [...]}` with mixed fields.
- Known allowed patterns (from `app/services/trading/pattern_engine.py` `default_patterns`) all classify as `pit`.
- Forbidden sentinel pattern (`{"indicator": "future_return_5d", "op": ">", "value": 0.02}`) classifies as `non_pit` and emits the release blocker line.
- Unknown field (`{"indicator": "my_secret_feature", ...}`) classifies as `unknown`.
- Audit writes exactly one `trading_pit_audit_log` row per pattern per call, with correct counts.
- Universe snapshot: `record_snapshot` is idempotent on `(as_of_date, ticker)`.

### 11. Docs: `docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md` (new)

Rollout ladder, allow / deny lists, auditor behavior, diagnostics response shape, release blocker rules, extension policy for adding new PIT-safe fields, non-goals.

## Forbidden this phase

- Do NOT silently drop patterns that fail PIT audit. Audit is advisory. Phase J handles quarantine.
- Do NOT change `_eval_condition` or `_eval_condition_bt` logic or add branches that skip conditions — PIT audit does not change runtime evaluation.
- Do NOT backfill `trading_universe_snapshots` historically. Writer stays dormant except for opportunistic calls; Phase D/F/G owns that.
- Do NOT touch mining condition generators in `learning.py` other than the single try/except shadow hook.
- Do NOT add PIT enforcement on user-submitted patterns (different trust boundary).
- Do NOT raise on missing `pattern.rules_json` — return an empty audit result.
- Do NOT import live market data in `pit_contract.py` or `pit_audit.py`.

## File-touch order

1. Plan file (this file).
2. `app/config.py` — 2 settings.
3. `app/migrations.py` — migration 130 (two tables).
4. `app/models/trading.py` — `UniverseSnapshot`, `PitAuditLog` ORM.
5. `app/trading_brain/infrastructure/pit_ops_log.py`.
6. `app/services/trading/pit_contract.py`.
7. `app/services/trading/pit_audit.py`.
8. `app/services/trading/universe_snapshot.py` (thin writer helper).
9. `tests/test_pit_contract.py` + `tests/test_pit_audit.py`.
10. Shadow hook in `learning.py`.
11. `app/routers/trading_sub/ai.py` — diagnostics endpoint.
12. `scripts/check_pit_release_blocker.ps1`.
13. `docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md`.
14. Docker soak: run learning cycle (or trigger audit manually via scripted endpoint), verify rows, check diagnostics, run release blocker against live logs and synthetic authoritative.

## Verification gates

1. Unit tests green: `tests/test_pit_contract.py`, `tests/test_pit_audit.py`.
2. Frozen contract tests still green: `tests/test_scan_status_brain_runtime.py`, `tests/test_exit_evaluator.py`, `tests/test_economic_ledger.py` (regression guard).
3. Docker soak with `BRAIN_PIT_AUDIT_MODE=shadow`: migration 130 applies; issuing at least one synthetic audit produces ≥1 `trading_pit_audit_log` row per active pattern (or 0 rows and `agree=true` if database has no active patterns — acceptable for green dev DB); diagnostics endpoint returns non-empty payload; release blocker exits 0 on real logs and 1 on a synthetic `mode=authoritative` line.
4. `rg "future_return_" app/services/trading/pit_contract.py` matches (denylist present).
5. Frozen `release` contract: **no** new `brain_runtime.release.*` keys leaked by this phase; `/api/trading/scan/status` payload shape unchanged (see `.cursor/rules/chili-scan-status-deploy-validation.mdc`).

## Rollback criteria

- If shadow audit throws in learning cycle: swallow is in place; verify legacy cycle finishes and re-disable via `BRAIN_PIT_AUDIT_MODE=off` in `.env`, recreate chili container.
- If migration 130 fails: drop both tables manually (Phase C has no cross-phase FKs) and remove migration entry with a follow-up migration; do not rewrite history.

## Non-goals

- Automatic quarantine (Phase J).
- Universe snapshot backfill (Phase D/F/G).
- Replacing `_eval_condition` in live/backtest paths.
- Enforcing PIT on user patterns.
- Point-in-time news/fundamentals re-fetch — we only classify; data provenance is a later phase.

## Definition of done

- Migration 130 applied; both tables exist; `UNIQUE (as_of_date, ticker)` and audit indexes present.
- `pit_contract.classify` and `classify_rules` imported and tested.
- Shadow learning-cycle hook wired and failure-tolerant.
- One `trading_pit_audit_log` row per active pattern per cycle (or empty DB acceptable).
- Diagnostics endpoint returns shadow payload with patterns_audited/clean/violating counts.
- Release blocker shipped and tested (exit 0 on shadow, exit 1 on synthetic authoritative).
- Docs + plan both closed; master plan flipped to `completed_shadow_ready`.

## Todos

- [x] **pc-config** - Add 2 settings to `app/config.py`.
- [x] **pc-migration** - Migration 130: `trading_pit_audit_log`, `trading_universe_snapshots`.
- [x] **pc-models** - `PitAuditLog`, `UniverseSnapshot` ORM.
- [x] **pc-opslog** - `pit_ops_log.py` formatter.
- [x] **pc-contract** - `pit_contract.py` with ALLOWED/FORBIDDEN + `classify` / `classify_rules`.
- [x] **pc-auditor** - `pit_audit.py` with `audit_pattern`, `audit_active_patterns`, `record_audit`.
- [x] **pc-universe-writer** - `universe_snapshot.py` thin idempotent writer.
- [x] **pc-tests** - `tests/test_pit_contract.py` (25 passed) + `tests/test_pit_audit.py` (19 passed).
- [x] **pc-shadow-hook** - try/except hook in `learning.run_learning_cycle` (post-`promoted_fast_eval` block).
- [x] **pc-diag-endpoint** - `GET /api/trading/brain/pit/diagnostics`.
- [x] **pc-release-blocker** - `check_pit_release_blocker.ps1` (verified: exit 0 on real shadow logs, exit 1 on synthetic `mode=authoritative`).
- [x] **pc-docs** - `docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md`.
- [x] **pc-soak** - Docker soak: migration 130 applied, BRAIN_PIT_AUDIT_MODE=shadow in container, 24 patterns audited (21 clean / 3 violating), including one real-world discovery (`gap_pct` used by promoted pattern `ema_uptrend_support_zone_breakout_watch` was not in allowlist — added). Universe snapshot idempotent write succeeded. Diagnostics endpoint returned patterns_audited=24, patterns_violating=3, forbidden/unknown hits tallied. Frozen scan-status contract still green.
