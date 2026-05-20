# CHILI Trading Brain Audit - 2026-04-30

Read-only audit against the local codebase, Docker runtime, logs, and Postgres. No code, broker, DB, or migration changes were made. The only writes from this audit are this report and `reference_2026_04_30_audit.md`.

## Prioritized Punch List

CRITICAL (fix before next trading session):
  1. Robinhood exit monitor crashes on normal stock exits - local `logger` assignment makes `submit_robinhood_trade_exit` raise `UnboundLocalError` every monitor cycle - at `app/services/trading/robinhood_exit_execution.py:608,655` - fix: remove the local logger assignment and use the module logger.
  2. Live positions are missing effective stop enforcement while bracket reconciliation is shadow-only - logs show `missing_stop` for live holdings and audit rows show repeated PDT/wide-spread exit deferrals - at `broker-sync-worker bracket_reconciliation` and `trading_autotrader_runs` - fix: repair exit submission bug, then move reconciliation from shadow to audited live repair for eligible positions.

HIGH (this week):
  1. `^VIX` OHLCV wrapper returns 0 rows even though direct yfinance returns data - zero-volume cleaning deletes valid index bars after Massive returns 403 for `I:VIX` - at `app/services/trading/data_quality.py:60` and `app/services/trading/market_data.py:580` - fix: allow zero-volume OHLCV for index tickers only.
  2. yfinance history cache lies across different `end` dates - cache key omits `end` - at `app/services/yf_session.py:221` - fix: include normalized `end` and other range inputs in the cache key.
  3. Runtime regime modes are still shadow despite live ledger rows - all containers report regime modes as `shadow`, snapshot writers are shadow, and no runtime override rows exist - fix: make one authoritative mode source and explicitly promote only validated gates.
  4. Stale `brain_batch_jobs` are piling up as `running` with no heartbeat - multi-hour `brain_market_snapshots` and `daily_market_scan` rows remain running - at `brain_batch_jobs` - fix: heartbeat active jobs and have the watchdog timeout/mark orphans predictably.
  5. Trade catalog contains invalid zero-entry rows and lacks range CHECKs - 67 trades have zero/invalid entry or quantity values - at `trading_trades` - fix: add NOT VALID constraints plus a cleanup/exclusion migration for legacy cancelled rows.
  6. Promoted patterns include negative average-return rows - promoted rows such as 1004, 1006, and 981 have negative `avg_return_pct` - at `scan_patterns` and promotion gates - fix: make EV authority explicit and demote/block promoted rows that fail the chosen raw realized/backtest EV source.

MEDIUM (this month):
  1. Numeric trading tables rely on application hygiene instead of DB constraints - many rate/ratio/confidence columns have no CHECKs - fix: add range constraints after measuring legacy violations.
  2. FRED fetch succeeds but latest macro snapshots do not carry the FRED yield-slope source - `macro_fred_fetch_log` is fresh while latest macro snapshots have blank/NULL source - fix: wire the recent fetch into the current snapshot writer.
  3. Operator-facing confidence still uses smoothing in `TradingInsight` - scan pattern writes are raw, but insight confidence still EWMA-blends - fix: store/display raw and smoothed values separately or rename the smoothed field.
  4. Active candidate patterns include never-backtested rows - 43 active full-scope candidates had no `last_backtested_at` - fix: drain these before further promotion/review cycles.
  5. Unbounded in-process caches remain - broker instrument cache and Massive websocket quote cache have no size/TTL eviction - fix: cap and expire them.

LOW (informational):
  1. Migration numbering is healthy, but prompt context is stale - local migrations parse as 1..212 with no holes/collisions, not "current: 197" - keep reports synced with migration reality.
  2. Restore migrations 168/170 remain as authority-drift history - pattern 1047 is no longer promoted, but the rescue migrations are still present and should be treated as cautionary.
  3. Pattern-linked breakout alert audit coverage is complete - last-24h pattern-linked breakout alerts were all represented in `trading_autotrader_runs`; non-pattern breakout alerts are separate and not evidence of a gap.

## 1. Data Hygiene - NaN, Range Violations, Percent/Fraction Confusion

[SEVERITY: HIGH]  Invalid zero-entry trades remain in the live trade catalog

WHERE: `trading_trades.entry_price`, `trading_trades.quantity`; missing CHECK constraints on `trading_trades`

EVIDENCE: SQL probe found `trades_bad_ranges=67`. Breakdown: `cancelled/coinbase=60`, `closed/robinhood=5`, `cancelled/robinhood=2`. Recent examples include ETH-USD closed/cancelled rows with `entry_price=0` and tiny quantities, plus many cancelled Coinbase crypto rows with `entry_price=0`. Constraint probe showed `trading_trades` only has `chk_trade_asset_kind`; there is no CHECK on positive entry price, exit price, quantity, or PnL sanity.

IMPACT: Closed/cancelled rows can still poison analytics, sync reconciliation, realized-EV calculations, and dashboards unless every reader remembers to filter them. A zero entry can also produce undefined return math in paths that divide by entry price.

ROOT CAUSE: Legacy or broker-sync placeholder rows were allowed into `trading_trades`, and the DB does not enforce the numeric invariants implied by the rest of the trading brain.

FIX: Add a cleanup/exclusion migration, then add NOT VALID CHECKs such as `entry_price > 0` for filled/open/closed trades, `quantity > 0` for non-cancelled trades, and finite/sane return constraints where return columns exist. Validate after legacy rows are either corrected or explicitly quarantined.

RISK: Do not blanket-delete these rows; cancelled broker artifacts may still be needed for audit chronology. Quarantine or exclude them from realized analytics first.

[SEVERITY: MEDIUM]  Trading numeric columns lack DB range constraints

WHERE: `trading_*` tables with columns matching rate/ratio/percentile/score/confidence/pct

EVIDENCE: Constraint inventory showed only a few trading CHECKs: `scan_patterns` has win-rate/oos-win-rate checks, `trading_pattern_trades` has `pattern_trades_ret_sane`, and `trading_trades` only checks asset kind. A column inventory found unconstrained rate/ratio/confidence-like fields across `trading_intraday_session_snapshots`, `trading_pattern_regime_performance_daily`, `trading_cross_asset_snapshots`, and others.

IMPACT: A single writer bug can persist 60.0 where 0.60 is expected, negative confidence, or non-finite ratios. Since these tables feed gates and dashboards, bad units can look like strong evidence.

ROOT CAUSE: The schema relies on application writers for numeric hygiene instead of making the DB enforce implicit ranges.

FIX: Add NOT VALID CHECK constraints in batches: probabilities and confidence in `[0,1]`, pct-return fields within sane bounds, ratios non-negative or bounded as appropriate. Validate after measuring and fixing legacy rows.

RISK: Do not guess ranges for every column in one migration. Some fields use percentage points while others use fractions; inspect each writer before constraining.

[SEVERITY: MEDIUM]  Operator-facing confidence still uses smoothing

WHERE: `app/services/trading/learning.py:4767-4770`, stale docstring at `app/services/trading/learning.py:4801`

EVIDENCE: Source review found `TradingInsight.confidence` is still blended as `existing.confidence * 0.4 + confidence * 0.6`. The scan-pattern writer now stores raw `win_rate` and `avg_return_pct`, but this insight path remains smoothed. The nearby docstring still says the method uses exponential blending.

IMPACT: Operator-facing confidence can hide a sudden loss pattern or sudden improvement. That violates the operator preference that raw realized data be visible and smoothing be explicit.

ROOT CAUSE: The prior raw-realized fix was applied to `scan_patterns`, but the companion `TradingInsight` summary path retained EWMA semantics.

FIX: Store `raw_confidence` and `smoothed_confidence` separately, or rename the existing field so consumers cannot mistake it for raw realized evidence. Update the stale docstring.

RISK: Do not silently replace a smoothed value with raw if any UI or threshold already depends on dampening; make both values explicit during the transition.

## 2. Provider Chain - Massive, Polygon, yfinance, FRED, Coinbase/CoinGecko

[SEVERITY: HIGH]  `^VIX` wrapper returns empty while direct yfinance succeeds

WHERE: `app/services/trading/data_quality.py:60-69`, `app/services/trading/market_data.py:580-585`, `app/services/massive_client.py:373-395`

EVIDENCE: Runtime probe in `chili` showed `fetch_ohlcv_df("^VIX", interval="1d", period="5d")` returned 0 rows. Direct `yf.Ticker("^VIX").history()` and `yf_session.get_history("^VIX")` returned non-empty data. Logs showed Massive translated `^VIX` to `I:VIX` correctly, then returned 403 `NOT_AUTHORIZED`. After fallback, the wrapper cleaned the yfinance index bars and dropped all zero-volume rows.

IMPACT: VIX-dependent regime, risk, and volatility gates can see "no data" even when the upstream provider has valid data. This can make gates skip, stale, or trust the wrong fallback.

ROOT CAUSE: The generic OHLCV quality filter treats zero volume as invalid for every symbol. That is reasonable for most equities, but invalid for yfinance index series such as `^VIX`, where zero volume is normal.

FIX: Add an index-aware path: for known index tickers or `^`-prefixed symbols, preserve zero-volume OHLCV while still validating price fields. Keep the existing zero-volume rejection for stocks/ETFs/crypto.

RISK: Do not globally allow zero-volume bars. That would admit broken equity feeds and make liquidity filters lie.

[SEVERITY: HIGH]  yfinance history cache omits `end`

WHERE: `app/services/yf_session.py:221`

EVIDENCE: Read-only monkeypatch proof called `get_history("SPY", start="2026-01-01", end="2026-02-01")`, then `get_history("SPY", start="2026-01-01", end="2026-03-01")`. The second call returned the first cached DataFrame. Observed cache key: `hist:SPY:6mo:1d:2026-01-01`, with no `end`.

IMPACT: Backtests, scanners, and diagnostics can receive stale slices for different requested end dates. This is a lying cache: the caller asks a different temporal question and gets the earlier answer.

ROOT CAUSE: `YFinanceSession.get_history` builds the cache key from symbol, period, interval, and start, but not end.

FIX: Include normalized `end` in the key, and preferably include every yfinance query parameter that changes the returned rows.

RISK: Do not disable the cache globally; yfinance rate limits are real. Fix key correctness while preserving bounded caching.

[SEVERITY: MEDIUM]  Crypto/new-symbol provider provenance is ambiguous

WHERE: `app/services/trading/market_data.py:254-343`, `app/services/yf_session.py:337-395`, runtime provider metadata

EVIDENCE: Runtime probe showed `BTC-USD` resolved through Massive, while `ASTER-USD` OHLCV fell back to Coinbase after Massive marked `X:ASTERUSD` dead. Direct yfinance history for ASTER was empty, but the quote wrapper reported source `yfinance` for ASTER quote data. Massive dead ticker sample included `I:VIX` and `X:ASTERUSD`.

IMPACT: Operators and gates can misread which upstream is actually supplying quote or bar data. A quote labeled yfinance after yfinance history is empty makes provider outages harder to localize.

ROOT CAUSE: The fallback chain mixes OHLCV providers, quote providers, and crypto rescue paths without a consistently audited provider provenance field at every return point.

FIX: Return explicit provider provenance for every OHLCV and quote path, including Coinbase and CoinGecko rescue paths. Log both attempted provider and final provider.

RISK: Do not remove fallback. The bug is attribution and coverage clarity, not the existence of fallback.

[SEVERITY: MEDIUM]  Recent macro snapshots are not carrying fresh FRED yield-slope source

WHERE: `macro_fred_fetch_log`, `trading_macro_regime_snapshots.yield_slope_source`

EVIDENCE: FRED fetch log showed fresh `DGS10`/`DGS2` success for 2026-04-28 fetched on 2026-04-30 06:35. But `trading_macro_regime_snapshots` showed `yield_slope_source='fred_dgs10_dgs2'` latest at 2026-04-27 06:30, while more recent snapshots through 2026-04-30 had blank/NULL source.

IMPACT: Macro regime snapshots can appear fresh while omitting the underlying FRED evidence. Gates or dashboards may silently fall back to partial macro inputs.

ROOT CAUSE: The FRED fetch path is alive, but the current macro snapshot writer is not consistently carrying the fetched slope source into recent rows.

FIX: Trace the latest macro snapshot writer and require it to persist `yield_slope_source` and the source timestamp whenever FRED values are used or intentionally absent.

RISK: Do not fabricate a source for rows where FRED is missing. Preserve NULL when genuinely unavailable, but make that absence explicit.

## 3. Pattern Lifecycle - Promotion Gates, Demote Audits, Rescue Migrations

[SEVERITY: HIGH]  Promoted patterns include negative average-return rows

WHERE: `scan_patterns.lifecycle_stage`, `scan_patterns.promotion_status`, `app/services/trading/realized_ev_gate.py`

EVIDENCE: SQL probe found promoted rows with negative `avg_return_pct`, including pattern 1004 (`promotion_status='promoted_via_bt_ev_199_rc'`, `win_rate=0.4517`, `avg_return_pct=-0.529`, `trade_count=2143`), pattern 1006 (`promoted_via_bt_ev_197`, `avg_return_pct=-0.668`), and pattern 981 (`promoted_via_bt_ev_197`, `avg_return_pct=-0.234`). `realized_ev_gate.py` requires positive raw EV when that gate is applied.

IMPACT: The catalog can advertise a pattern as promoted while its stored average return is negative. Depending on the reader, alerts and autotrader gates may treat it as eligible despite negative catalog evidence.

ROOT CAUSE: Promotion authority is split between backtest EV, realized EV, lifecycle stage, and legacy promotion statuses. Some promoted statuses appear to bypass the current raw-EV invariant or preserve stale negative fields.

FIX: Define one authoritative EV source per promotion status and run a retroactive audit: either demote promoted rows with negative authoritative EV or update fields so the displayed EV source matches the promotion evidence.

RISK: Do not demote solely on an ambiguous field if `avg_return_pct` is known to represent a stale backtest metric for that status. First bind each promotion status to its source of truth.

[SEVERITY: LOW]  Pattern 1047 rescue migrations remain as authority-drift history

WHERE: `app/migrations.py` migrations 168 and 170

EVIDENCE: Migration parse found `168_restore_pattern_1047_cpcv_miscalibration` and `170_restore_pattern_1047_n_paths_threshold_second` still present. Current lifecycle query no longer showed pattern 1047 as promoted, so the active state appears corrected, but the rescue migrations remain in history.

IMPACT: These migrations are durable evidence of a failure mode: state was restored against gate evidence. Future manual rescues can repeat this if migration review does not flag authority drift.

ROOT CAUSE: Corrective migrations encoded a policy override rather than making the gate evidence and override reason first-class.

FIX: Leave historical migrations intact, but add a migration-review guardrail: any `restore_*`, `force_*`, or pattern-specific rescue must include the gate evidence it overrides and an expiry/re-review plan.

RISK: Do not rewrite old applied migrations. Treat them as audit history and prevent recurrence.

## 4. Regime Ledger + Gates - Ticker, Breadth, Cross-Asset, Vol Coverage

[SEVERITY: HIGH]  Regime modes are shadow at runtime while ledger contains live rows

WHERE: runtime settings in `chili`, `brain-worker`, `scheduler-worker`, `autotrader-worker`; `trading_pattern_regime_performance_daily.mode`; snapshot tables

EVIDENCE: Runtime settings print in all checked containers reported `chili_regime_gate_mode='shadow'`, `ticker/breadth/cross_asset/vol/macro/intraday` modes all `shadow`, and `brain_pattern_regime_autopilot_enabled=True`. `trading_brain_runtime_modes` had no per-regime override rows. Snapshot tables were all or mostly shadow: ticker 2384 shadow/0 live, breadth 11/0, cross_asset 12/0, vol 12/0, intraday 11/0. But `trading_pattern_regime_performance_daily` had `live` rows across ticker, breadth, cross_asset, and vol dimensions.

IMPACT: The ledger can look live while the consumer-side gates still act in shadow. Operators may believe regime evidence is constraining trades when it is only being recorded or inconsistently labeled.

ROOT CAUSE: Mode authority is split among environment settings, runtime modes table, snapshot writer labels, and ledger writer labels.

FIX: Make regime mode resolution single-source and auditable. Add a startup log or DB row recording the effective mode for each dimension and gate consumer. Promote dimensions to live only after a smoke test shows snapshots, ledger rows, and gate decisions agree.

RISK: Do not flip every regime dimension to live blindly. Some dimensions are fresh but sparse; promote one dimension at a time with a rollback path.

## 5. Autotrader Entry Funnel - Gate Stack Health + Decision Distribution

[SEVERITY: LOW]  Pattern-linked breakout alert audit path is healthy, but the split is easy to misread

WHERE: `trading_breakout_alerts.id -> trading_autotrader_runs.breakout_alert_id`

EVIDENCE: Last-24h SQL probe found 1844 pattern-linked breakout alerts and 1844 audited autotrader rows. It also found 266 non-pattern breakout alerts and 0 autotrader rows for those, which is expected because they are separate from the pattern-linked autotrader path. Joining `trading_alerts` directly to autotrader runs produced false zeroes and should not be used for this audit.

IMPACT: The entry funnel is not missing pattern-linked audit rows, but future audits can falsely report a gap if they use the wrong alert table or include non-pattern breakout alerts.

ROOT CAUSE: There are multiple alert tables with different semantics, and the correct autotrader key is `breakout_alert_id`, not `trading_alerts.id`.

FIX: Document the audit query in a repo runbook and add a small SQL view or diagnostic that separates pattern-linked and non-pattern breakout alerts.

RISK: Do not force non-pattern alerts into the autotrader audit path unless product policy says they are trade candidates.

[SEVERITY: MEDIUM]  Entry decisions show recurring quote/profitability skips that need trend dashboards

WHERE: `trading_autotrader_runs.decision`, `trading_autotrader_runs.reason`

EVIDENCE: Last-24h decision distribution included repeated entry-side skips: `stop_not_below_entry=39`, `projected_profit_below_min=36`, `no_quote=33`, `blocked pdt_guard:pdt_limit_reached:47>=3=18`, and `llm_not_viable=13`. Placed entries were only 10 in the same sample.

IMPACT: Some skips are protective and correct, but without a trend view they can hide upstream quote holes, stale pattern economics, or persistent PDT saturation.

ROOT CAUSE: Autotrader decisions are audited row-by-row, but there is no compact health surface separating healthy protective skips from degradation patterns.

FIX: Add a daily decision histogram grouped by entry/exit, reason family, ticker, and provider state. Use it for alerts before changing gates.

RISK: Do not weaken `projected_profit`, stop placement, PDT, or wide-spread guards just to raise conversion. Those guards are protecting fills and compliance.

## 6. Exit Monitor - Target/Stop Fills, Wide-Spread Defers, Dup-COID Recovery

[SEVERITY: CRITICAL]  Robinhood stock exits crash before submission

WHERE: `app/services/trading/robinhood_exit_execution.py:608,655`

EVIDENCE: Source review found a local assignment `logger = __import__("logging").getLogger(__name__)` inside the option-specific branch of `submit_robinhood_trade_exit`. Python therefore treats `logger` as local throughout the function. Live `autotrader-worker` logs repeatedly showed `UnboundLocalError: cannot access local variable 'logger' where it is not associated with a value` at line 655 on the non-option path, reached from `auto_trader_monitor.py:484`, every monitor cycle.

IMPACT: Normal stock exit handling crashes before it can submit or fully audit an exit. This is load-bearing because live broker exits are involved.

ROOT CAUSE: A local logger assignment inside one branch shadows the module-level logger for the whole function.

FIX: Remove the local assignment and use the module-level logger. Minimal proposed diff: delete the branch-local `logger = ...` line; no behavior change besides unshadowing.

RISK: Do not catch and ignore this exception. The monitor must either submit, reject with an audit row, or defer with a clear reason.

[SEVERITY: CRITICAL]  Live positions have missing stops while bracket reconciliation is shadow-only

WHERE: `broker-sync-worker` `bracket_reconciliation` logs; `trading_trades`; `trading_autotrader_runs`

EVIDENCE: Broker-sync logs repeatedly emitted `bracket_reconciliation ... mode=shadow` and `missing_stop` severity errors for live holdings including ADT, WDCX, and ABEV. Open trade probe showed ADT with `pending_exit_status=deferred`, `pending_exit_reason=stop`, stop 7.35, entry 7.135; WDCX and ABEV were open with no active repair shown. Last-24h autotrader decisions showed heavy exit failures: `monitor_exit_rejected` PDT "Sell may cause PDT designation" 1333 times, `monitor_exit_deferred` wide spread 227 times, and "Not enough shares to sell" 56 times.

IMPACT: The system detects missing protective exits but does not repair them in live mode, while the exit monitor is also crashing on a core stock-exit path. A position can remain exposed after the system knows the stop is missing.

ROOT CAUSE: Bracket reconciliation is configured as detection-only shadow, and the live exit monitor is impaired by the Robinhood logger bug plus broker/PDT rejections.

FIX: First fix the exit submission crash. Then enable a constrained live reconciliation mode that repairs missing stops only for broker-confirmed holdings and records an audit row for every repair, rejection, or defer.

RISK: Do not bypass PDT or wide-spread protections. The repair path must respect compliance and fill-quality guards, but it must make unresolved exposure visible and escalated.

[SEVERITY: HIGH]  Exit rejection/defer rows are dominated by PDT and spread failures

WHERE: `trading_autotrader_runs` last-24h decision distribution

EVIDENCE: Last-24h grouped decisions found `monitor_exit_rejected` PDT broker rejections 1333 times, `monitor_exit_deferred` wide-spread 227 times, "Not enough shares to sell" 56 times, `monitor_exit_filled pattern_exit_now` 15 times, and `monitor_exit_recovered recovered_from_dup_coid` 12 times.

IMPACT: Even when the monitor does not crash, exits are frequently not executed. Some reasons are correct broker/compliance responses, but the repeated volume means positions need an escalation state rather than endless retries.

ROOT CAUSE: Exit monitor retries protective exits into broker/compliance constraints without a higher-level unresolved-risk workflow.

FIX: Add an unresolved-exit escalation state keyed by trade/ticker/reason after repeated PDT/spread/share rejections, and surface it in alerts. Keep retry cadence bounded.

RISK: Do not widen spread thresholds or suppress PDT checks. Escalation is for visibility and manual/alternate handling, not worse fills.

## 7. Brain-Worker / Scheduler - Cycle Completion, Subtask Cadence, Stalls

[SEVERITY: HIGH]  `brain_batch_jobs` has stale running rows without heartbeats

WHERE: `brain_batch_jobs.status`, `brain_batch_jobs.heartbeat_at`

EVIDENCE: Last-24h job audit found `ok=839`, `orphaned=18`, `running=7`, `timeout=1`. Running rows included multi-hour `brain_market_snapshots` and `daily_market_scan` jobs with NULL heartbeats, plus `crypto_breakout_scanner` and `price_monitor` rows. Older orphaned rows covered pattern and breakout scanner jobs.

IMPACT: Operators cannot tell whether a cycle is active, wedged, or already superseded. Stale running rows also hide scheduler health regressions and can interfere with singleton job logic if locks depend on status.

ROOT CAUSE: Some batch jobs do not heartbeat or are not consistently marked timeout/orphaned by the watchdog.

FIX: Require every long-running job wrapper to set `heartbeat_at` on a fixed cadence and have the watchdog transition stale running rows to `timeout` or `orphaned` with an explicit reason.

RISK: Do not simply mark all running jobs failed on startup; a real in-flight job may exist. Use heartbeat age plus worker identity.

[SEVERITY: LOW]  Idle-in-transaction sessions were present but not yet stale

WHERE: `pg_stat_activity`

EVIDENCE: Runtime probe saw two `chili-brain-worker` and one `chili-scheduler-cron` session in `idle in transaction`, with transaction ages under roughly two minutes at sample time. The `db_watchdog` exists, and this sample did not show multi-hour idle transactions.

IMPACT: Current sample is not an outage, but idle transactions can hold locks or prevent vacuum if they age out unnoticed.

ROOT CAUSE: Some worker paths leave transactions open between operations, at least briefly.

FIX: Keep the idle-transaction watchdog alerting, and add the application/job name to job audit rows so any future stale transaction maps back to a scheduler task.

RISK: Do not terminate short-lived transactions aggressively; use age thresholds and query state.

## 8. Backtest Queue - Staleness, Queue Depth, Wall-Clock Budget

[SEVERITY: LOW]  Backtest queue is moving, but active candidates include never-backtested rows

WHERE: `scan_patterns.lifecycle_stage`, `scan_patterns.last_backtested_at`, `trading_backtests`

EVIDENCE: `trading_backtests` had 39,186 rows total, latest at 2026-04-30 13:23:51, with 3,121 in the last 24h. Active full-scope candidates included 43 rows with `last_backtested_at IS NULL`; three candidates were stale more than seven days.

IMPACT: The backtest system is not stalled, but candidate review/promotion can keep circulating around patterns that have never received a current backtest.

ROOT CAUSE: Queue throughput exists, but priority and eligibility do not guarantee every active candidate drains before other repeated refresh work.

FIX: Add a starvation guard: active candidates with no backtest get a bounded priority boost until first test completion, then priority resets through the existing `mark_pattern_tested` path.

RISK: Do not starve promoted/challenged refresh entirely; first-test fairness should be capped.

## 9. Migrations - ID Collisions, Retired IDs, Contradictory Rescues

[SEVERITY: LOW]  Migration numbering is clean; prompt migration context is stale

WHERE: `app/migrations.py`

EVIDENCE: AST parse succeeded. Migration inventory showed `migration_count=212`, min ID 1, max ID 212, no duplicate IDs, no duplicate numbers, no holes, and `RETIRED_MIGRATIONS` empty. The prompt's "current: 197" is stale against the local codebase.

IMPACT: Operators using the old prompt can audit the wrong migration boundary and miss newer safety or rescue changes.

ROOT CAUSE: The audit prompt was not updated after migrations 198-212 landed.

FIX: Update the standing audit prompt/reference docs to say current migration max is 212 as of 2026-04-30, and include a migration inventory command in the audit checklist.

RISK: Do not renumber migrations. The sequence is healthy.

[SEVERITY: LOW]  Restore/force migration names should trigger extra review

WHERE: `app/migrations.py` migrations 168, 170

EVIDENCE: `git`/source scan found restore-pattern migrations for 1047, while no ID collisions or holes were present.

IMPACT: The codebase contains a historical example of policy override via migration. That is useful context but dangerous if normalized.

ROOT CAUSE: Pattern-specific operational decisions were encoded as migrations instead of time-limited operator overrides.

FIX: Add a migration review checklist item that flags `restore_`, `force_`, `rescue_`, and pattern-specific migrations for explicit gate-evidence review.

RISK: Do not delete applied history; prevent the pattern from recurring.

## 10. Settings Drift - Config Defaults vs Docker Env vs Runtime

[SEVERITY: HIGH]  Effective runtime modes disagree with recent operator expectations

WHERE: Docker runtime settings for `chili`, `brain-worker`, `scheduler-worker`, `autotrader-worker`; `trading_brain_runtime_modes`

EVIDENCE: Runtime probes showed all checked containers reporting regime modes as `shadow`. `trading_brain_runtime_modes` contained only broad runtime-surface rows and `autotrader_v1_desk`, not per-regime gate overrides. Service settings also differed: scheduler used `brain_queue_backtest_executor='process'` and cap 6, while web/brain/autotrader reported thread executor or no cap.

IMPACT: A prior session believed some modes had been flipped live, but the effective runtime says shadow. Safety gates, ledger labels, and operator expectations can diverge.

ROOT CAUSE: Mode/config authority is spread across defaults, Docker env, runtime DB rows, and service-specific settings.

FIX: Add an effective-settings diagnostic endpoint or script that prints the resolved mode source for each service. Use it as part of deploy/startup and before trading sessions.

RISK: Do not assume a docker-compose edit changed running containers; verify inside each container.

## 11. Memory - Unbounded Caches, Dict Growth, Idle Sessions

[SEVERITY: MEDIUM]  Broker instrument and websocket quote caches are unbounded

WHERE: `app/services/trading/broker_service.py:2747`, `app/services/massive_client.py:959`

EVIDENCE: Source review found `_instrument_cache: dict[str, str] = {}` in broker service with no TTL/size cap; `clear_cache()` clears `_cache` but not `_instrument_cache`. Massive websocket quotes use `_ws_cache: dict[str, QuoteSnapshot] = {}` with staleness checks on read but no pruning. Docker stats during audit were not alarming (`brain-worker` about 392 MiB, `scheduler-worker` about 1.28 GiB), so this is a growth risk rather than a current OOM.

IMPACT: Long-lived workers can accumulate one entry per symbol/instrument ever seen. Over weeks, scanners and crypto symbols can make memory and stale quote behavior drift.

ROOT CAUSE: Some caches were added as plain dicts without bounded eviction, unlike `yf_session` and Massive REST caches that have explicit maximum sizes.

FIX: Add TTL + max-size eviction, and make `clear_cache()` cover all broker caches. For websocket quotes, prune expired symbols periodically or when inserting.

RISK: Do not set a tiny cap that forces broker instrument lookups on every tick. Instrument cache misses can be rate-limited; use a practical bound.

## 12. Audit Trails - Gaps Where Decisions Do Not Get Logged

[SEVERITY: HIGH]  Exit monitor crash path is logged but not reliably audited as a decision

WHERE: `autotrader-worker` logs; `trading_autotrader_runs`

EVIDENCE: Logs showed the monitor raising `UnboundLocalError` before completing the stock-exit path. Autotrader decision rows do capture many monitor outcomes, but this crash happens inside the submission function and can terminate that attempt before a normal `filled/rejected/deferred` audit row exists for the specific exit.

IMPACT: The operator can see a stack trace in logs, but the structured autotrader audit trail may undercount failed exit attempts caused by code exceptions.

ROOT CAUSE: Exception handling around exit submission does not always convert internal errors into structured trade-decision audit rows.

FIX: After fixing the logger bug, wrap broker submission calls so unexpected exceptions write a `monitor_exit_error` audit row with ticker, trade id, exception class, and retry/escalation state.

RISK: Do not let a structured error row make the system think the exit was handled. It must remain unresolved until a real fill, broker rejection, or explicit defer is recorded.

[SEVERITY: LOW]  Batch-job audit rows lack reliable liveness semantics

WHERE: `brain_batch_jobs`

EVIDENCE: Stale `running` rows with NULL heartbeats coexist with orphaned and timeout rows. This means the table is an audit trail of starts, but not always a reliable trail of current liveness.

IMPACT: Health checks and future audits must run extra inference instead of trusting job status.

ROOT CAUSE: Job status and heartbeat are not enforced as a contract for every scheduler path.

FIX: Treat `heartbeat_at` as required for any job expected to exceed a short threshold, and have a single watchdog own stale transitions.

RISK: Do not add multiple watchdogs that race each other. One owner should write final status transitions.
