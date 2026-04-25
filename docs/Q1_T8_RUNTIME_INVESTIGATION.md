# Q1.T8 runtime investigation (read-only)

**Scope:** identify operational sources of `BRAIN_PREDICTION_OPS_LOG_ENABLED=false` and `CHILI_AUTOTRADER_ENABLED=true` and document consumers, DB state, and related behavior. **No remediation.**

---

## Investigation A — `BRAIN_PREDICTION_OPS_LOG_ENABLED=false`

### A.1 — Where the env var is set (repo and compose; `.env` is the source in this worktree)

**Order checked:** `.env` → `docker-compose.yml` → `docker-compose.override.yml` (none) → `*.env` in repo (none besides root `.env`).

**`docker-compose.yml` — `chili` service loads optional `.env` (does *not* set this variable in `environment:`):**

```95:100:c:\dev\chili-home-copilot\docker-compose.yml
    ports:
      - "8000:8000"
    env_file:
      - path: .env
        required: false
    environment:
```

**`docker-compose.override.yml`:** not present in the repository (no matching file at repo root; glob 2026-04-25).

**Root `.env` (file is gitignored — see `c:\dev\chili-home-copilot\.gitignore:4` — so it is **not** in git history; below is read-only from disk):** setting and surrounding *non-secret* context:

- Lines 53–55: `TEST_DATABASE_URL=...` (pytest DB).
- Lines 56–60: *Phase 6 prediction mirror block* — includes:

```56:60:c:\dev\chili-home-copilot\.env
# Phase 6 validation complete — safe defaults (prediction mirror flags off)
BRAIN_PREDICTION_OPS_LOG_ENABLED=false
BRAIN_PREDICTION_DUAL_WRITE_ENABLED=false
BRAIN_PREDICTION_READ_COMPARE_ENABLED=false
BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED=false
```

**`docker-compose.yml`:** no occurrence of `BRAIN_PREDICTION_OPS` (ripgrep over `docker-compose.yml`).

**Default in code (post–Q1.T8 merge) for comparison:** `c:\dev\chili-home-copilot\app\config.py:217-219` — `default=True` (bool) with alias `BRAIN_PREDICTION_OPS_LOG_ENABLED` at line 219.

**`.env.example`:** `c:\dev\chili-home-copilot\.env.example:100` — `BRAIN_PREDICTION_OPS_LOG_ENABLED=true` (example only; not what Docker loads if `.env` overrides).

---

### A.2 — Consumers: `grep` "brain_prediction_ops_log_enabled" under `app/`

| File | Line(s) | Role |
|------|---------|------|
| `app/config.py` | 217, 219 | Field definition + `AliasChoices("BRAIN_PREDICTION_OPS_LOG_ENABLED")` |
| `app/services/trading/learning_predictions.py` | 620-630 | When **True**: emit **one** `logger.info` using `format_chili_prediction_ops_line(...)`; when **False**: block skipped — Phase 4/5 WARNING behavior unchanged (see `app/trading_brain/README.md:52-53`) |
| `app/trading_brain/README.md` | 14-15, 52-89 | Documents Phase 6 switch and `[chili_prediction_ops]` contract |

**True vs False (code path):** `app/services/trading/learning_predictions.py:620-630` — the **`if getattr(..., "brain_prediction_ops_log_enabled", False):`** body runs only when **True**; otherwise no Phase-6 one-line `INFO` is emitted at this callsite.

---

### A.3 — `app/trading_brain/` and `[chili_prediction_ops]`

- **Prefix / formatter:** `app/trading_brain/infrastructure/prediction_ops_log.py:7` — `CHILI_PREDICTION_OPS_PREFIX = "[chili_prediction_ops]"`.  
- **Log line content:** `app/trading_brain/infrastructure/prediction_ops_log.py:37-54` — `format_chili_prediction_ops_line` returns a **single** bounded `INFO` string: `dual_write=... read=... explicit_api_tickers=... fp16=... snapshot_id=... line_count=...` (no ticker lists).
- **Volume:** **At most one** such line per successful execution of the gated block in `learning_predictions.py` (i.e., per call into that path when the flag is **True**). Contract text: `app/trading_brain/README.md:51-52` — “**one** bounded `INFO` line per `_get_current_predictions_impl`”.

- **HR5 / ops prefix policy:** `app/services/trading/ops_log_prefixes.py:40-49` — documents frozen `PREDICTION_OPS` literal and release-blocker grep expectations.

---

### A.4 — Git history (tracked files only)

`git check-ignore -v .env` → `c:\dev\chili-home-copilot\.gitignore:4` marks `.env` ignored, so **`.env` is not versioned** and `git log -p -S "BRAIN_PREDICTION_OPS_LOG_ENABLED" -- .env docker-compose.yml` returns **no introducer commit for `.env`**.

- `git log -p -S "BRAIN_PREDICTION_OPS_LOG_ENABLED" -- docker-compose.yml` — **no matches** (var not set in compose).

Sample of **other** tracked changes touching compose (not this var):

```text
$ git log --oneline --diff-filter=AM -- .env "docker-compose*.yml" | head
a79bd59 Increase Docker container resource limits across all services
2c7e4c6 fix(autopilot): loop sessions after trade, ...
(etc.)
```

**Conclusion (history):** the live **`false`** for `BRAIN_PREDICTION_OPS_LOG_ENABLED` in Docker is from **local `.env`**, not from a committed `docker-compose` line; **first introduction in git for that key in `.env` cannot be shown** while `.env` is untracked.

---

### A.5 — Best-guess summary (evidence)

| Hypothesis | Verdict | Evidence |
|------------|--------|----------|
| (a) HR5-freeze: logs misleading post-freeze | **Partial / weak** | `app/trading_brain/README.md:14` still documents `brain_prediction_ops_log_enabled=False` as shipped default for Phase 6; **HR5** freeze is about **editing** `app/trading_brain/*` (see `ops_log_prefixes.py:31-35`), not automatically requiring ops log off. |
| (b) Log volume too high | **Unlikely** as primary | `prediction_ops_log.py:37-54` and `README:51-52` — **one line per prediction path** when on; not high-volume. |
| (c) Intentional “safe defaults” after Phase 6 validation | **Strong** | `c:\dev\chili-home-copilot\.env:56-57` — comment *“Phase 6 validation complete — safe defaults (prediction mirror flags off)”* immediately before `BRAIN_PREDICTION_OPS_LOG_ENABLED=false`. Q1.T8 raised code default to `True` in `app/config.py:218`, but **env still overrides to false**. |
| (d) Other | **Accurate** | **Pydantic env wins over new default** — documented pattern in `audit_readonly_inventory.md:12-13` (env mapping). |

---

## Investigation B — `CHILI_AUTOTRADER_ENABLED=true`

### B.1 — Where the env var is set

**`docker-compose.yml`:** no `CHILI_AUTOTRADER` (ripgrep on file).

**Root `.env` (gitignored) — autotrader block:**

```259:264:c:\dev\chili-home-copilot\.env
# AutoTrader v1 live flip (desk-only flow still governs runtime behavior)
CHILI_AUTOTRADER_ENABLED=true
CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED=true
# Owner user id for system-scope (user_id IS NULL) pattern_imminent alerts
CHILI_AUTOTRADER_USER_ID=1
```

(Additional autotrader-related overrides follow at `c:\dev\chili-home-copilot\.env:265-280`.)

**`docker-compose.yml:97-99`:** `env_file: .env` (optional) for `chili` — same mechanism as A.

**No** `docker-compose.override.yml` in repo.

---

### B.2 — Code references (`chili_autotrader_enabled` / `CHILI_AUTOTRADER_ENABLED`)

| File | Line(s) |
|------|---------|
| `app/config.py` | 1717-1719 |
| `app/services/trading_scheduler.py` | 1656-1657, 1673-1674, 3010-3030 (registration + tick/monitor) |
| `app/services/trading/auto_trader.py` | 299-300 (early return `skipped: disabled` when false) |
| `app/services/trading/auto_trader_monitor.py` | 196-197 (skip when false) |
| `app/services/trading/autotrader_desk.py` | 53-54 (desk `tick_allowed` / `monitor_entries_allowed`) |
| `app/routers/trading.py` | 1206 (`env_enabled` in API payload) |
| `app/templates/trading/_autopilot_pattern_positions.html` | 9 |
| `app/static/js/autopilot-pattern-desk.js` | 71 |

**True vs False:**  
- **False:** `trading_scheduler.py:1656-1657` and `1673-1674` — job handlers **return immediately**; `auto_trader.py:299-300` — `run_auto_trader_tick` returns `skipped: True, reason: disabled`.  
- **True:** tick/monitor run `run_auto_trader_tick` / `tick_auto_trader_monitor` (same files).

---

### B.3 — Scheduler: AutoTrader tick + monitor jobs (`~3010-3030`)

`c:\dev\chili-home-copilot\app\services\trading_scheduler.py:3010-3030`

- **Tick cadence:** `IntervalTrigger(seconds=_at_tick_s)` where `_at_tick_s = max(5, int(getattr(settings, "chili_autotrader_tick_interval_seconds", 10)))` — **line 3011, 3015-3016**.
- **Monitor cadence:** `IntervalTrigger(seconds=_at_mon_s)` where `_at_mon_s = max(5, int(getattr(settings, "chili_autotrader_monitor_interval_seconds", 30)))` — **line 3012, 3024-3025**.

**What tick does (summary from code):** `app/services/trading/auto_trader.py:297-334` — if disabled / kill switch / not `tick_allowed` → return early; else resolve user id, query **pattern_imminent** `BreakoutAlert` candidates (batch limit 5), process with advisory locks, rules, placement path — returns counts (`processed`, `placed`, etc. — see `auto_trader.py:336+`).

**What monitor does (summary from code):** `app/services/trading/auto_trader_monitor.py:194-257` — if disabled or kill switch → early; if **not** live effective, **paper exit** path and daily loss cap check; if live effective, continues past line 256+ (Robinhood adapter path) — `auto_trader_monitor.py:256-259`.

---

### B.4 — Defaults from `app/config.py` (and note `.env` overrides for this worktree)

**From `c:\dev\chili-home-copilot\app\config.py` (Field defaults only):**

| Setting | Line | Default in code |
|---------|------|-----------------|
| `chili_autotrader_live_enabled` | 1721-1723 | `True` |
| `chili_autotrader_per_trade_notional_usd` | 1729-1732 | `300.0` |
| `chili_autotrader_daily_loss_cap_usd` | 1743-1746 | `150.0` |
| `chili_autotrader_max_concurrent` | 1748-1752 | `3` |
| `chili_autotrader_confidence_floor` | 1754-1758 | `0.7` |
| `chili_autotrader_min_projected_profit_pct` | 1760-1763 | `12.0` |
| `chili_autotrader_max_symbol_price_usd` | 1765-1768 | `50.0` |
| `chili_autotrader_max_entry_slippage_pct` | 1770-1773 | `1.0` |
| `chili_autotrader_rth_only` | 1888-1890 | `True` |
| `chili_autotrader_allow_extended_hours` | 1892-1900 | `False` |
| `chili_autotrader_assumed_capital_usd` | 1906-1909 | `25000.0` |
| `chili_autotrader_tick_interval_seconds` | 1911-1915 | `10` |
| `chili_autotrader_monitor_interval_seconds` | 1776-1780 | `30` |
| `chili_autotrader_synergy_enabled` | 1739-1741 | `False` |
| `chili_autotrader_llm_revalidation_enabled` | 1902-1904 | `True` |
| `chili_autotrader_broker_equity_cache_enabled` (next block) | 1918+ | (see file — env-driven broker equity cache) |

**This worktree’s `.env` explicitly overrides (non-default) for operator tuning** — e.g. `c:\dev\chili-home-copilot\.env:265`, `278-280`: `CHILI_AUTOTRADER_ALLOW_EXTENDED_HOURS=true`, `CHILI_AUTOTRADER_MIN_PROJECTED_PROFIT_PCT=8.0`, `CHILI_AUTOTRADER_MAX_SYMBOL_PRICE_USD=200.0`, `CHILI_AUTOTRADER_MAX_ENTRY_SLIPPAGE_PCT=2.0`. **Runtime `settings` in Docker reflect env over code defaults** for those keys.

---

### B.5 — PostgreSQL (canonical `chili` DB) — read-only

**`trading_trades` autotrader scope:** `app/models/trading.py:93` — `management_scope` column. Constant: `app/services/trading/management_scope.py:7` — `MANAGEMENT_SCOPE_AUTO_TRADER_V1 = "auto_trader_v1"`.

**No `created_at` on `trading_trades`** — `information_schema` query confirmed `entry_date` and `management_scope` only (no `created_at` for this table in result set). Counts use **`entry_date`**.

**Query:**

```sql
SELECT COUNT(*) AS total_autotrader_trades,
       COUNT(*) FILTER (WHERE entry_date >= NOW() - INTERVAL '7 days') AS last_7_days,
       COUNT(*) FILTER (WHERE entry_date >= NOW() - INTERVAL '24 hours') AS last_24_hours,
       MIN(entry_date) AS first_trade,
       MAX(entry_date) AS most_recent_trade
FROM trading_trades
WHERE management_scope = 'auto_trader_v1';
```

**Result (2026-04-25, `docker exec chili-home-copilot-postgres-1 psql`):

| total_autotrader_trades | last_7_days | last_24_hours | first_trade | most_recent_trade |
|-------------------------|-------------|-----------------|------------|-------------------|
| 19 | 19 | 3 | 2026-04-21 13:49:56.356021 | 2026-04-24 13:52:36.734697 |

---

### B.6 — `trading_autotrader_runs`

`to_regclass('public.trading_autotrader_runs')` → **`trading_autotrader_runs`** (exists).

**Row count and span:**

| n | last_run | first_run |
|---|----------|-----------|
| 7538 | 2026-04-25 03:18:46.559592 | 2026-04-18 20:54:24.997742 |

**`decision` distribution:**

| decision | count |
|----------|-------|
| monitor_exit_deferred | 6622 |
| skipped | 826 |
| monitor_exit_submitted | 32 |
| monitor_exit_filled | 20 |
| placed | 19 |
| monitor_exit_rejected | 17 |
| monitor_exit_cancelled | 2 |

**Interpretation (factual):** `placed=19` matches `trading_trades` autotrader count `19` for scope `auto_trader_v1` (both measure “placement”-level decisions, not a second source of truth for fills).

---

### B.7 — `broker_sessions`

```sql
SELECT broker, COUNT(*), MAX(updated_at) AS max_updated FROM broker_sessions GROUP BY broker;
```

**Result:**

| broker | count | max_updated |
|--------|-------|-------------|
| robinhood | 1 | 2026-04-21 14:12:22.16721 |

**Factual note:** `max_updated` is **several days** before the investigation date; this **does not** by itself prove current API auth, but is consistent with **stale** session metadata relative to “today.”

---

### B.8 — Robinhood `401` on `.../instruments?symbol=...` (log line shape from Step 6)

- **Chili’s explicit instrument resolution** includes `app/services/trading/venue/robinhood_spot.py:144-145` — `rh.stocks.get_instruments_by_symbols([ticker])` (robin_stocks / Robinhood REST).
- **Broker order sync** uses `app/services/broker_service.py:1618-1626` — `get_instrument_by_url` for resolving ticker labels from order payloads.

**Typical `401 Client Error: Unauthorized` for a REST URL to `api.robinhood.com/instruments/...` originates in the **robin_stocks** HTTP layer** when the **stored Robinhood session is invalid or expired** (library raises through `requests`).

**Autotrader interaction:** if Robinhood session is **invalid**, **order placement and instrument lookups** that require authenticated REST would **fail** (not “silent success” for those calls); the **monitor/tick** paths still run per scheduler, but **broker** operations log errors. **Stuck order** risk is a separate path (`stuck_order_watchdog` — `trading_scheduler.py:3036+`); this report does not assert stuck orders without querying `trading_trades` order states.

---

### B.9 — Best-guess summary

| Question | Answer (evidence-based) |
|----------|-------------------------|
| Is the autotrader **placing** trades in DB terms? | **Yes, historically** — `trading_trades` shows **19** rows with `management_scope = 'auto_trader_v1'`; `trading_autotrader_runs` shows **`decision=placed` count 19** (see B.5–B.6). |
| At what **rate** recently? | **3** in last **24h** and **19** in last **7d** by `entry_date` on `trading_trades` (B.5). |
| Sizing / gates | **Code defaults** in B.4; **`.env` overrides** profit floor 8.0, max price 200, slippage 2.0, extended hours (`.env:265, 278-280`); per-trade notional default **300** in code **unless** overridden in env (not shown in the quoted `.env` snippet — grep only showed the keys above for autotrader block). |
| `401` on instruments | Implies **unauthenticated** Robinhood REST for those calls; **separate** from “scheduler tick is running” (which only needs `chili_autotrader_enabled` true). **Auth health** for live orders should be validated via `broker_sessions` + live API checks outside this read-only pass. |
| Failing “safely”? | **Gates in code** return `skipped`/`blocked` dicts; **401** is an **upstream auth failure** for RH REST — not a silent “trade placed” success. **Exact** live vs paper behavior depends on `effective_autotrader_runtime` + `chili_autotrader_live_enabled` and desk state (`autotrader_desk.py` / `auto_trader_monitor.py:215-256`) — not fully expanded here. |

---

*Report generated read-only. `.env` contains secrets: do not commit. `docker-compose` citation paths use the workspace root.*

## Resolution

Investigation complete on 2026-04-25. Two flag overrides confirmed as **deliberate**:

- **`BRAIN_PREDICTION_OPS_LOG_ENABLED=false`** — Phase 6 safe-default, preserved per `c:\dev\chili-home-copilot\.env:56-57` comment (`# Phase 6 validation complete — safe defaults (prediction mirror flags off)`). Q1.T8’s code-default change to `True` in `app/config.py:217-219` is correct in principle but **operationally overridden** by `.env`. No change from this follow-up.

- **`CHILI_AUTOTRADER_ENABLED=true`** — Set in `c:\dev\chili-home-copilot\.env:260`; live activity since first autotrader-tagged `trading_trades.entry_date` **2026-04-21** (first in scope `auto_trader_v1`); `trading_autotrader_runs` activity from **2026-04-18** (see SQL in body above). As of the closeout SQL (2026-04-25): **19** autotrader-tagged trades, aggregate **realized+closed P\&L on settled rows: total -30.36 USD** (4 wins, 8 losses, 6 open/unsettled `pnl` null); per-trade list used columns `direction` (not `side` — `trading_trades` has no `side`). **No change** to the flag; recorded as **operator-deliberate** state with tuned autotrader env (see same `.env` block).

**Robinhood auth state:** `public.broker_sessions` has **no** `user_id` / `expires_at` columns (see `\d broker_sessions`); reported row: `id=1`, `broker=robinhood`, `username=rindolf.miaco@gmail.com`, `updated_at=2026-04-21 14:12:22.16721` (stale relative to 2026-04-25). **401 Client Error** on `https://api.robinhood.com/instruments/?symbol=...` in the last **6h**: **~150** lines matching `401 Client Error` in **`chili-home-copilot-chili-1` logs**; **0** matching `401 Client Error` in **`brain-worker`** (stricter than naive `401` which false-matches `0.401` in OHLCV split ratios). Log lines are bare `requests`/`robin_stocks` style, followed by **“Warning: ... is not a valid stock ticker”** (string **not** in `app/` — library output); instrument-by-symbol resolution is discussed in `app/services/trading/venue/robinhood_spot.py:144-145` and `app/services/broker_service.py:1618-1626` for in-repo call paths.

**`trading_autotrader_runs` last 24h (2026-04-25):** `monitor_exit_deferred` 369, `skipped` 143, `placed` 3, `monitor_exit_filled` 2, `monitor_exit_submitted` 1 (column `created_at`).

**No flag changes** from this investigation. Q1.T8 closes with **documented runtime divergence** between **code defaults (post-merge)** and **operator-deployed `.env`** (Phase 6 safe-defaults for prediction-mirror + autotrader-on with operator-tuned parameters).
