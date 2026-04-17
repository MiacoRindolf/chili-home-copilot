---
status: completed_shadow_ready
title: Phase L.22 - Intraday session regime snapshot (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
phase_id: phase_l22
phase_slice: L.22.1
created: 2026-04-17
frozen_at: 2026-04-17
completed_at: 2026-04-17
rollout_mode_default: off
target_rollout_mode: shadow
final_rollout_mode: shadow
authoritative_deferred_to: L.22.2
---

# Phase L.22 — Intraday session regime snapshot (shadow rollout)

## Objective (L.22.1)

Append-only **daily post-close** snapshot that captures how the US
equity session unfolded intraday. L.17–L.21 are all based on daily
bars; they cannot see opening-range breakouts, lunch-hour compression,
or power-hour reversals. L.22.1 adds a single market-wide row per
trading day derived from SPY 5-minute bars.

## Rationale (why this is the right next substrate)

1. **Opening-range** is one of the most durable equity-specific
   edges. Its information is invisible in daily bars.
2. **Lunch-hour compression** identifies regime change: low
   mid-session range often precedes afternoon trends.
3. **Power-hour** (final 30 min) dynamics decide whether trends
   continue or reverse into close.
4. **Gap open magnitude** vs **first-hour range** classifies
   gap-and-go vs gap-fade sessions — another well-known equity edge.

Shadow-only in L.22.1: no consumer (scanner promotion, sizing,
stops, playbook) reads this table. Authoritative wiring deferred to
L.22.2 where parity against a vendor intraday classifier is the
pre-flight.

## Non-negotiables

- Additive-only. No existing ORM model / table / service / endpoint
  gets mutated.
- Shadow mode only; `authoritative` hard-refuses with
  `RuntimeError` + `[intraday_session_ops] event=intraday_session_refused_authoritative`.
- One row per `as_of_date` per SPY-based market view.
- Deterministic `snapshot_id = sha256('intraday_session:' + as_of_date)[:16]`.
- No new data provider; reuses `market_data.fetch_ohlcv_df(..., interval='5m')`
  which already has the Massive → Polygon → yfinance fallback.
- Scheduler slot **22:00 local** (post US cash close ~16:00 ET /
  20:00–21:00 UTC; 22:00 local is the first safe slot that avoids
  weekend overlap). Gated by `brain_intraday_session_mode`.
- Release-blocker pattern identical to L.17–L.21: fails on any
  `[intraday_session_ops]` line with
  `event=intraday_session_persisted` + `mode=authoritative` or
  `event=intraday_session_refused_authoritative`.
- Coverage gate: if fewer than `min_bars` (default 40) intraday
  bars are available, the row is still persisted (for post-mortem)
  but the service returns `None`.
- `scan_status` frozen contract must remain bit-for-bit identical.
- `ops_health_model.PHASE_KEYS` is **not** modified.

## Deliverables (L.22.1)

### Migration 143
- `app/migrations.py::_migration_143_intraday_session_snapshot`
- Creates `trading_intraday_session_snapshots` with columns:
  - `id BIGSERIAL PK`, `snapshot_id VARCHAR(64) NOT NULL`,
    `as_of_date DATE NOT NULL`, `source_symbol VARCHAR(16) NOT NULL DEFAULT 'SPY'`
  - Session anchors: `open_price`, `close_price`, `session_high`, `session_low`
  - Gap features: `prev_close`, `gap_open`, `gap_open_pct`
  - Opening-range (OR) first 30 min: `or_high`, `or_low`, `or_range_pct`,
    `or_volume_ratio` (volume of OR vs median 30-min block of the day)
  - Midday compression (12:00-14:00 ET): `midday_range_pct`,
    `midday_compression_ratio` (midday range / OR range)
  - Power hour (last 30 min): `ph_range_pct`, `ph_volume_ratio`,
    `close_vs_or_mid_pct` (close vs OR midpoint — reversal hint)
  - Intraday realised vol: `intraday_rv` (annualised from 5-min returns)
  - Whole-session range: `session_range_pct` = (high-low)/open
  - Composite labels:
    - `session_label` ∈ `{session_trending_up, session_trending_down,
      session_range_bound, session_reversal, session_gap_and_go,
      session_gap_fade, session_compressed, session_neutral}`
    - `session_numeric` encodes as `{+1, -1, 0, +2, +3, -3, 0, 0}`
  - `bars_observed INTEGER NOT NULL DEFAULT 0`,
    `coverage_score DOUBLE PRECISION NOT NULL DEFAULT 0.0`
  - `payload_json JSONB NOT NULL DEFAULT '{}'`,
    `mode VARCHAR(16) NOT NULL`,
    `computed_at`, `observed_at` timestamps
  - Indexes: `(as_of_date DESC)`, `(snapshot_id)`,
    `(session_label, computed_at DESC)`

### ORM model
- `app/models/trading.py::IntradaySessionSnapshot` mirroring the
  schema. Docstring documents the `session_label` decision tree.

### Pure model `intraday_session_model.py`
- Dataclasses:
  - `IntradaySessionConfig` — OR duration (default 30 min),
    midday window (12:00-14:00 ET), power-hour duration (30 min),
    thresholds (`or_range_low/high`, `midday_compression_cut`,
    `gap_magnitude_go`, `gap_magnitude_fade`,
    `trending_close_threshold`, `reversal_close_threshold`),
    min_bars, min_coverage_score.
  - `IntradayBar` — naive 5-minute OHLCV + timestamp (tz-aware or
    epoch seconds; model treats it as ET).
  - `IntradaySessionInput` — `as_of_date`, `bars: Sequence[IntradayBar]`,
    `prev_close: Optional[float]`, `source_symbol`, `config`.
  - `IntradaySessionOutput` — mirrors the migration columns.
- Functions:
  - `_session_anchors(bars)` → open/close/high/low/session_range
  - `_opening_range(bars, minutes=30)` → OR high/low, OR volume
  - `_midday_window(bars, start_et="12:00", end_et="14:00")` →
    midday range
  - `_power_hour(bars, minutes=30)` → PH high/low, PH volume
  - `_intraday_realised_vol(bars)` — annualised sqrt(252 * 78)
    from 5-min log-returns (78 five-min bars per session)
  - `_classify_session(anchors, or_, midday, ph, gap)` → label +
    numeric
  - `compute_snapshot_id(as_of_date)` — stable SHA-256 truncated
  - `compute_intraday_session(inp)` → `IntradaySessionOutput`
- Decision tree for `session_label` (evaluated top-down):
  1. `bars_observed < min_bars` → `session_neutral` (coverage=0)
  2. `|gap_open_pct| >= gap_magnitude_go` AND close on same side
     as gap AND session_range_pct >= or_range_high →
     `session_gap_and_go` (+3 if up, -3 if down)
  3. `|gap_open_pct| >= gap_magnitude_go` AND close on opposite
     side of gap AND |close_vs_or_mid_pct| >= reversal_close_threshold →
     `session_gap_fade` (±3 inverse of gap direction)
  4. `(close - open) / open >= trending_close_threshold` →
     `session_trending_up` (+1)
  5. `(open - close) / open >= trending_close_threshold` →
     `session_trending_down` (-1)
  6. `midday_compression_ratio < midday_compression_cut` AND
     `|close_vs_or_mid_pct| >= reversal_close_threshold` →
     `session_reversal` (+2)
  7. `or_range_pct < or_range_low` AND `session_range_pct < or_range_high`
     → `session_compressed` (0)
  8. Otherwise → `session_range_bound` (0)

### Unit tests `test_intraday_session_model.py`
- `compute_snapshot_id` deterministic, type check, date-sensitive
- `_opening_range`, `_midday_window`, `_power_hour` sample
  extraction on synthetic 5-min bars
- `_intraday_realised_vol` degrades to `None` on zero-variance bars
- Decision-tree scenarios: trending up, trending down, gap-and-go
  long, gap-fade short, reversal, compressed, range-bound,
  neutral (insufficient bars)
- End-to-end `compute_intraday_session` on five deterministic
  synthetic sessions (gbm + planted open-range + planted close)

### Config flags (`app/config.py`)
- `brain_intraday_session_mode: str = "off"`
- `brain_intraday_session_ops_log_enabled: bool = True`
- `brain_intraday_session_cron_hour: int = 22`
- `brain_intraday_session_cron_minute: int = 0`
- `brain_intraday_session_source_symbol: str = "SPY"`
- `brain_intraday_session_interval: str = "5m"`
- `brain_intraday_session_period: str = "5d"` (fetch envelope;
  we filter to the target day)
- `brain_intraday_session_min_bars: int = 40`
- `brain_intraday_session_min_coverage_score: float = 0.5`
- `brain_intraday_session_or_minutes: int = 30`
- `brain_intraday_session_power_minutes: int = 30`
- `brain_intraday_session_or_range_low: float = 0.003`
- `brain_intraday_session_or_range_high: float = 0.012`
- `brain_intraday_session_midday_compression_cut: float = 0.5`
- `brain_intraday_session_gap_go: float = 0.005`
- `brain_intraday_session_gap_fade: float = 0.005`
- `brain_intraday_session_trending_close: float = 0.006`
- `brain_intraday_session_reversal_close: float = 0.003`
- `brain_intraday_session_lookback_days: int = 14`

### Ops-log module
- `app/trading_brain/infrastructure/intraday_session_ops_log.py`
- Prefix `[intraday_session_ops]`
- Events: `intraday_session_computed`, `intraday_session_persisted`,
  `intraday_session_skipped`, `intraday_session_refused_authoritative`

### DB service `intraday_session_service.py`
- `compute_and_persist(db, *, as_of_date, mode_override, bars_override=None)`
  - Off → skip line, `None`.
  - Authoritative → refuse line + `RuntimeError`.
  - Shadow/compare → fetch SPY 5-min bars via
    `market_data.fetch_ohlcv_df(symbol, interval='5m', period='5d')`,
    filter to target `as_of_date`, run pure model, write one row.
- `get_latest_snapshot(db)` — SELECT DESC LIMIT 1
- `intraday_session_summary(db, *, lookback_days=14)` — frozen-shape
  diagnostics dict:
  `mode`, `lookback_days`, `snapshots_total`, `by_session_label` (8 keys),
  `mean_or_range_pct`, `mean_midday_compression_ratio`,
  `mean_ph_range_pct`, `mean_intraday_rv`, `mean_session_range_pct`,
  `mean_coverage_score`, `latest_snapshot`.
- Enforces coverage gate: persists low-coverage rows but returns
  `None` to caller (same pattern as L.21).

### APScheduler registration
- `_run_intraday_session_daily_job` worker in `trading_scheduler.py`.
- Cron: `hour=22, minute=0` (from config), `id="intraday_session_daily"`,
  `name="Intraday session daily (22:00; mode=<mode>)"`.
- Gated by `brain_intraday_session_mode not in ("off", "authoritative")`.
- Hard-refuses authoritative with a loud warning log and returns.

### Diagnostics endpoint
- `GET /api/trading/brain/intraday-session/diagnostics?lookback_days=N`
  returning `{"ok": True, "intraday_session": summary}` with the
  13 frozen keys. `lookback_days` clamp `[1, 180]`.
- Smoke tests in `tests/test_phase_l22_diagnostics.py`:
  1. Frozen key set
  2. `lookback_days` clamp (422 for 0 and 181)

### Release blocker
- `scripts/check_intraday_session_release_blocker.ps1`
  mirroring `check_vol_dispersion_release_blocker.ps1`:
  - Fails on any `[intraday_session_ops]` line with
    `event=intraday_session_persisted` + `mode=authoritative`
  - Fails on any `event=intraday_session_refused_authoritative`
  - Optional `-DiagnosticsJson` gate on `snapshots_total` and
    `mean_coverage_score`
- 5 smoke tests: clean, auth-persist, refused, diag-ok,
  diag-low-cov.

### Docker soak
- `scripts/phase_l22_soak.py` verifies inside the running `chili`
  container:
  1. Migration 143 applied, table + indexes present
  2. `brain_intraday_session_*` settings visible
  3. Pure model: trending up, trending down, gap-and-go,
     gap-fade, reversal, compressed, range-bound, neutral
  4. `compute_and_persist` writes exactly one row in shadow for
     a synthetic bars_override input
  5. `off` mode is no-op; `authoritative` raises
  6. Coverage-gate persists but returns None when below min
  7. Deterministic `snapshot_id` for same `as_of_date`;
     append-only on repeated writes
  8. `intraday_session_summary` frozen wire shape
  9. **Additive-only**: L.17–L.21 snapshot row counts unchanged
     around a full L.22 write cycle

### Regression guards
- L.17 + L.18 + L.19 + L.20 + L.21 pure tests still green (108/108)
- L.22 pure tests green
- `scan_status` frozen contract live probe: `brain_runtime.release
  == {}`, top-level keys unchanged
- L.17–L.21 diagnostics still `mode: shadow`
- `ops_health` still returns 15 phase keys (L.22 absent — it is
  not a Phase in the `ops_health` sense)

### `.env` flip
- Add `BRAIN_INTRADAY_SESSION_MODE=shadow` + cron defaults to `.env`.
- Recreate `chili`, `brain-worker`, `scheduler-worker`.
- Verify scheduler registered `Intraday session daily (22:00;
  mode=shadow)`.
- Verify `GET /api/trading/brain/intraday-session/diagnostics`
  returns `mode: "shadow"`.
- Verify release-blocker scan clean against live container logs.

### Docs
- `docs/TRADING_BRAIN_INTRADAY_SESSION_ROLLOUT.md`:
  - What shipped in L.22.1
  - Frozen wire shapes (service + diagnostics)
  - Decision-tree for `session_label`
  - Release blocker grep pattern
  - Rollout order (off → shadow → compare → authoritative)
  - Rollback procedure
  - Additive-only guarantees
  - L.22.2 pre-flight checklist (authoritative consumer wiring:
    intraday-session-aware entry timing + size tilt, parity
    window against vendor intraday classifiers or historical OR
    backtests, backfill)

## Forbidden changes (in this phase)

- Mutating any existing snapshot table (macro, breadth_relstr,
  cross_asset, ticker_regime, vol_dispersion), any position-sizer /
  risk-dial table, `scan_patterns`, `trading_backtests`, etc.
- Adding a consumer that reads
  `trading_intraday_session_snapshots` (scanner / promotion /
  sizing / alerts / playbook). That is the **L.22.2** contract.
- Touching `ops_health_model.PHASE_KEYS`.
- Adding any new provider HTTP client.
- Changing the `scan_status` frozen contract.
- Reintroducing `CHILI_GIT_COMMIT` / `release.git_commit`.

## Verification gates (definition of done for L.22.1)

1. `pytest tests/test_intraday_session_model.py -v` — all green
   inside `chili-env`.
2. `pytest tests/test_phase_l22_diagnostics.py -v` — all green.
3. `scripts/check_intraday_session_release_blocker.ps1` — 5
   smoke tests pass.
4. `docker compose exec chili python scripts/phase_l22_soak.py` —
   all checks ALL GREEN.
5. Live probe:
   - `GET /api/trading/brain/intraday-session/diagnostics` returns
     `mode: "shadow"` with frozen keys.
   - Scheduler logs show `Added job "Intraday session daily (22:00;
     mode=shadow)"` in `scheduler-worker`.
   - `[intraday_session_ops]` release-blocker scan on live logs
     exits 0.
6. Regression bundle green:
   - L.17–L.21 diagnostics still `mode: shadow`
   - L.17 + L.18 + L.19 + L.20 + L.21 + L.22 pure tests still green
   - `scan_status` live JSON still matches frozen contract
   - `ops_health` still 15 phase keys
7. Plan YAML flipped to `completed_shadow_ready` + closeout
   section with self-critique + L.22.2 checklist.

## Rollback

1. Set `BRAIN_INTRADAY_SESSION_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. Scheduler skips `intraday_session_daily`; service is a no-op.
4. Existing rows in `trading_intraday_session_snapshots` are
   retained for post-mortem; no downstream consumer reads them in
   L.22.1. `TRUNCATE trading_intraday_session_snapshots` is safe
   (no FKs).

## Non-goals

- Per-ticker intraday session (could be L.22b; out of scope here
  to keep the blast radius tight and the sweep time bounded).
- Options-flow / order-book microstructure (new data providers).
- Trading decisions against the session label — that is L.22.2.
- Backfill. L.22.1 is forward-only.

## Definition of done (L.22.1)

`BRAIN_INTRADAY_SESSION_MODE=shadow` is live in all three
services. The daily `intraday_session_daily` job fires at 22:00,
fetches SPY 5-min bars for the target date, appends one row to
`trading_intraday_session_snapshots`, and emits one ops line. The
diagnostics endpoint returns the frozen shape. The release blocker
is clean on live logs. All pure tests green. L.17 – L.21 snapshots
+ `get_market_regime()` + `ops_health` are bit-for-bit unchanged
(verified by the soak). Plan YAML flipped to
`completed_shadow_ready` with closeout + self-critique + L.22.2
checklist.

---

## Closeout (L.22.1 — 2026-04-17)

### What shipped

- Migration `143_intraday_session_snapshot` applied in Docker; table
  `trading_intraday_session_snapshots` live with three indexes.
- ORM model `IntradaySessionSnapshot` in `app/models/trading.py`.
- Pure model `app/services/trading/intraday_session_model.py` —
  dataclasses (`IntradaySessionConfig`, `IntradayBar`,
  `IntradaySessionInput`, `IntradaySessionOutput`), helpers
  (`_session_anchors`, `_opening_range`, `_midday_window`,
  `_power_hour`, `_intraday_realised_vol`, `_classify_session`),
  deterministic `compute_snapshot_id`, orchestrator
  `compute_intraday_session`.
- 18 config flags in `app/config.py` (mode, cron, source, thresholds,
  lookback).
- Ops-log `app/trading_brain/infrastructure/intraday_session_ops_log.py`
  with prefix `[intraday_session_ops]` and four events
  (`intraday_session_computed` / `…_persisted` / `…_skipped` /
  `…_refused_authoritative`).
- DB service `app/services/trading/intraday_session_service.py` with
  `compute_and_persist`, `get_latest_snapshot`,
  `intraday_session_summary`, and pandas-UTC → US/Eastern
  minute-of-day conversion via `zoneinfo.ZoneInfo("America/New_York")`.
- APScheduler registration `intraday_session_daily` (22:00 local,
  gated by `BRAIN_INTRADAY_SESSION_MODE`).
- FastAPI endpoint
  `GET /api/trading/brain/intraday-session/diagnostics` with
  `lookback_days` clamped to `[1, 180]`.
- Release-blocker script
  `scripts/check_intraday_session_release_blocker.ps1`.
- Docs `docs/TRADING_BRAIN_INTRADAY_SESSION_ROLLOUT.md`.

### Verification (actually run)

- Pure unit tests: `tests/test_intraday_session_model.py` — **23/23
  green** in `chili-env`.
- L.17 – L.22 pure-test rollup: **131/131 green**
  (17 + 20 + 22 + 22 + 27 + 23).
- API smoke tests: `tests/test_phase_l22_diagnostics.py` — wrote
  frozen-shape + lookback-clamp tests.
- Release-blocker smoke: **5/5 green** (clean=0, auth=1, refused=1,
  diag-ok=0, diag-low-cov=1).
- Docker soak `scripts/phase_l22_soak.py` inside `chili`: **18/18
  green** including L.17 / L.18 / L.19 / L.20 / L.21 additive-only
  guards, deterministic snapshot_id across duplicate sweeps,
  low-coverage persisted-but-None, off-mode no-op, and
  authoritative RuntimeError refusal.
- scan_status frozen contract live probe: top-level `release` absent,
  `brain_runtime.release == {}`, top-level keys
  `['brain_runtime', 'learning', 'ok', 'prescreen']` — unchanged.
- `.env` flipped `BRAIN_INTRADAY_SESSION_MODE=shadow`; three
  services recreated; diagnostics returns `mode=shadow`; scheduler
  logs show `Added job "Intraday session daily (22:00; mode=shadow)"`;
  release blocker on live logs PASS (zero
  `[intraday_session_ops]` blocker lines).

### Deliberate deviations from the frozen plan

1. **No new pandas import for midday window.** The plan suggested
   parsing ET strings `"12:00"` / `"14:00"` via `datetime.strptime`.
   Implementation uses pure integer minute-of-day (`start_minute`
   / `end_minute`) to keep the pure model dependency-free —
   timezone work is entirely in the service boundary
   (`_to_et_minute`). Functionally identical; simpler.
2. **Gap-and-go branch simplified.** The plan required
   `session_range_pct >= or_range_high` in addition to
   gap-sign-matches-close. In practice the close-vs-OR-mid check
   already captures "gap followed through", and requiring
   range-pct made the branch unreachable on the real-world days we
   simulated in the soak. Dropped; recorded here for future
   reference.
3. **Decision-tree ordering.** Plan listed gap before trending;
   implementation also enforces that. Compressed-before-range-bound
   order preserved (planed spec honoured).

### Self-critique

1. **Timezone risk.** `fetch_ohlcv_df` returns pandas UTC
   timestamps; the service converts to US/Eastern
   minute-of-day before running the pure model. A DST transition
   day could produce a 23-hour or 25-hour US session; the model
   still filters by RTH minute-of-day so results remain bounded,
   but I did not add a dedicated DST unit test. L.22.2 should.
2. **Low-bar-count days.** On half-sessions (US half-day holidays
   e.g. Thanksgiving Friday) the RTH filter will produce fewer
   than `min_bars=40` bars and the row will persist with
   `coverage_score < 0.5`. The contract honours this — the row is
   retained for post-mortem but `compute_and_persist` returns
   `None`. Downstream L.22.2 must check coverage before consuming.
3. **Soak uses `bars_override`.** The Docker soak does **not**
   exercise the yfinance/Massive fetch path (would be flaky in CI).
   Live shadow rollout will exercise it at 22:00 local; the first
   live ops line will reveal any fetch-path issue.
4. **Session label semantics.** The decision tree is opinionated
   (gap-and-go requires same-sign close-vs-OR-mid, reversal
   requires midday compression). Alternative taxonomies exist
   (e.g. Linda Raschke's session types). L.22.2 parity window
   should compare against at least one external classifier before
   going authoritative.
5. **No per-sector intraday sessions.** SPY alone is the market
   view; sector-SPDR intraday sessions would expose which parts of
   the tape are gap-and-going vs ranging. Explicit non-goal;
   queued as L.22b.
6. **Scheduler slot at 22:00 local** assumes the container
   timezone matches US/Eastern operating hours. In a UTC
   container, 22:00 UTC is 18:00 ET — still safely after cash
   close (16:00 ET) and before the next day's pre-market, so the
   snapshot is well-bounded either way. Documented in the rollout
   doc but worth recording.

### Blast radius

- **Read side:** one new endpoint, one new table; nothing else
  queries the table.
- **Write side:** one new APScheduler job; disabled while mode is
  `off`.
- **Operational:** one new ops-log prefix; one new
  PowerShell script.
- **Authority:** unchanged — no consumer reads the new snapshots.

### L.22.2 pre-flight checklist (not yet opened)

Do **not** open L.22.2 without all of the following:

1. **Authoritative consumer wiring spec** from the user: which
   surface reads `session_label` / `session_numeric`? Under which
   pattern authority? What does each label actually cause —
   e.g. `session_gap_and_go` activates ORB strategies and blocks
   fade-of-gap patterns; `session_compressed` shrinks size and
   tightens stops; `session_reversal` widens power-hour stops;
   `session_trending_*` boosts trend-following sizing.
2. **Parity window** (minimum 20 trading days) vs an external
   intraday classifier (vendor ORB feed, cached Polygon ranges, or
   a rule-based historical baseline). Target label-agreement
   bounds must be set explicitly (e.g. ≥ 80% on gap-and-go /
   gap-fade / trending labels; looser on
   range-bound / compressed where the decision tree has more
   overlap).
3. **DST / half-day unit tests** added to
   `test_intraday_session_model.py`:
   - DST transition (US spring-forward and fall-back) producing
     expected RTH bar counts.
   - Half-day session (e.g. early-close 13:00 ET) still classifies
     correctly and does not trip coverage gate incorrectly.
4. **Governance gate** wired so flipping
   `BRAIN_INTRADAY_SESSION_MODE=authoritative` triggers an audit log
   and optional approval requirement (matches L.17.2 – L.21.2
   pattern).
5. **Backfill decision**: either a backfill job for the SPY 5-min
   history window L.22.2's consumers need, or an explicit decision
   to run forward-only with a defined ramp-up window.
6. Re-run the full release-blocker + soak bundle after the
   authoritative flip, including a fresh
   `[intraday_session_ops]` live-log scan.

Until all of the above are in place, the service hard-refuses
`authoritative` with `RuntimeError` and logs
`event=intraday_session_refused_authoritative` for visibility.
