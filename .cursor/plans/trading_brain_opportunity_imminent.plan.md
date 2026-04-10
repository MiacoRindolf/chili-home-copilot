---
name: Trading Brain opportunity + imminent
overview: Merge imminent-alert quality redesign with a shared scoring model, new opportunity_board service + GET API, and board-first Trading Brain UI (debug second). Manual trading only; Tier A Telegram remains high quality.
todos:
  - id: imminent-core
    content: pattern_imminent_alerts.py — gating, universe, coverage, composite rank, diversity, BreakoutAlert, dry-run summary, config, tests
    status: completed
  - id: scoring-shared
    content: Extract or centralize composite/coverage helpers used by both imminent dispatch and opportunity_board (avoid duplicate formulas)
    status: completed
  - id: opportunity-service
    content: opportunity_board.py — tiers A–D; generated_at; session/market summary; plain-English why_here / why_not_higher / main_risk; no_trade + human reasons; prediction_ticker overlap; stale threshold in config
    status: completed
  - id: api-opportunity-board
    content: GET /api/trading/opportunity-board (+ optional debug=1) in trading router or ai.py; wire identity
    status: completed
  - id: ui-brain-board
    content: brain.html — operator summary (counts, no_trade, refresh, session); staleness banner; compact tiers + show more; plain-English reasons; predictions overlap; strong empty-state; Debug tab only
    status: completed
  - id: validation-docs
    content: Example JSON + manual checklist + UI QA matrix (fresh+A, B/C only, no-trade, stale) + env vars
    status: completed
isProject: false
---

# Trading Brain Opportunity Board + imminent alert redesign (merged plan)

## Reference UI

Production desk: `https://getchili.app/brain?domain=trading`. Repo template: [`app/templates/brain.html`](app/templates/brain.html).

**Current layout (trading domain):**

- `#trading-brain-pane-output` → `.trading-main-content` contains **ZONE 1** status bar (`#brain-status-bar`), **ZONE 2** edge grid (playbook + P&amp;L), **Live Predictions**, **Tradeable Patterns** (`#brain-tradeable-section`), then **ZONE 3** deep-dive tabs (Patterns / Cycles / Ops / Research / Analytics).
- Assistant: `#trading-brain-assistant-section`.
- Network graph: separate pane `#trading-brain-pane-network`.

**Integration choice (repo-consistent):**

1. Insert **Opportunity Board** as a new **brain-section** immediately **above** “Live Predictions” (or directly under ZONE 1 status bar) so it is the **first scroll-stopping operator output** after status.
2. Add a **compact chip row** in `#brain-status-bar` (`.bsb-center` or new row under `.bsb-row`): `Actionable: X | Watch soon: Y | Watch today: Z | Refreshed: …` — clicks scroll to `#brain-opportunity-board` (anchor id).
3. Preserve **Live Predictions** + **Tradeable Patterns**; optionally collapse “Tradeable Patterns” slightly or add a one-line cross-link (“Pattern detail → Tradeable tab”).
4. **Debug / raw:** add a **sixth deep-dive tab** `Debug` **or** a collapsible panel under the board “Show scan debug” that calls the same API with `debug=1`. Prefer **tab `Debug`** in `#bdd-tabs` to avoid cluttering the default view — keeps “board-first, debug-second” explicit.
5. Do **not** remove existing Ops endpoints or worker queue debug; Debug tab surfaces **opportunity-board debug** + optional link text to Ops for scheduler/imminent dry-run.

---

## Execution addendum (UI, API, scoring discipline)

### 1) Freshness / staleness (API + UI)

- API returns `generated_at` (ISO UTC) and `is_stale` (bool) when `now - generated_at > opportunity_board_stale_seconds` (new setting, conservative default e.g. 120–300s depending on product tolerance — document choice).
- UI: show **Last refreshed** on every load; if stale, **visible warning** (banner or bordered state on `#brain-opportunity-board`) — e.g. amber strip “Data may be outdated — refresh.” Do **not** present stale rows as implicitly current.

### 2) Market / session context (board summary)

- API: `session_context` object — e.g. `us_session: regular_hours | premarket | after_hours | closed`, `crypto_context: active` (24/7), `equity_evaluation_active: bool` aligned with imminent stock gating. Reuse [`us_stock_session_open`](app/services/trading/pattern_imminent_alerts.py) and extend minimally for pre/after if a helper exists in `market_data` / elsewhere; if not, document “regular vs closed only” in v1 and extend when data exists.
- Surface in **operator summary** so empty Tier A is explainable (“US stocks: session closed”).

### 3) Compact operator surface

- Default **small cap per tier** in API (`max_per_tier` defaults, e.g. A≤3, B≤5, C≤8) with `has_more` flags; UI **“Show more”** or collapsible expand per tier. Tier A and B: **scannable** rows/cards (dense, not tables of 50).

### 4) Plain-English reason blocks (every candidate)

- Each candidate JSON includes short strings (not only metrics): `why_here`, `why_not_higher_tier`, `main_risk` — generated deterministically from tier rules + sources (templates OK). Keep under ~1–2 lines each for UI.

### 5) Alignment with Live Predictions

- If ticker ∈ current prediction set, set `also_in_live_predictions: true` (or `prediction_support: { direction, confidence }` when cheap). UI: small badge “In live predictions” / link scroll to `#brain-predictions` for that ticker. **Single source:** board API may call `get_current_predictions` once and index tickers for overlap — avoid conflicting narratives in copy (“Board + predictions agree” vs “Scanner only”).

### 6) Strong empty / no-trade state

- When no Tier A (or no-trade): show **plain-language** explanations driven by API `no_trade_summary_lines[]` or structured codes mapped to copy — e.g. no promoted/live patterns qualified, session closed for stocks, coverage too weak, all below score threshold, **data stale** (if `is_stale`). Builds **trust**.

### 7) Operator summary block (top of board — required)

- Fixed at top of `#brain-opportunity-board`: **Actionable: X | Watch soon: Y | Watch today: Z | No-trade now: yes/no | Last refresh: … | Session: …** — compact, no scroll required for this strip.

### 8) Debug secondary

- Suppressions, skip lists, raw score dumps **only** in `debug=1` response **and** Debug tab / collapsible — never in default board payload beyond short counts.

### 9) Shared scoring — no formula drift

- **One implementation** of composite + coverage math. Tier and Telegram gates differ **only** by thresholds / eligibility filters / `min_composite`, not a second scoring function. Comment this invariant in `opportunity_scoring.py`.

### 10) UI validation examples (manual / QA)

After implementation, verify four states in browser:

1. **Fresh board with Tier A** — summary shows A≥1, no stale banner, reasons visible.
2. **Fresh board, only B/C** — no false Tier A; no-trade or “nothing actionable now” honest copy; summary counts match.
3. **No-trade** — empty actionable with specific plain-language reasons from API.
4. **Stale** — artificially shorten `opportunity_board_stale_seconds` in dev or mock old `generated_at` — banner visible, copy warns not current.

---

## Part 1 — Imminent alert redesign (unchanged intent)

Implement in [`app/services/trading/pattern_imminent_alerts.py`](app/services/trading/pattern_imminent_alerts.py) + [`app/config.py`](app/config.py):

- **Main Telegram path:** default **promoted/live quality only** — filter `ScanPattern` with `lifecycle_stage in ("promoted", "live")` **or** legacy `promotion_status == "promoted"` where lifecycle not migrated (document safest OR in SQL).
- **Universe:** watchlist + prescreener-backed tickers (query prescreen tables via [`prescreen_job`](app/services/trading/prescreen_job.py) after read) + recent `ScanResult` + prediction/top-pick tickers (reuse [`learning_predictions._build_prediction_tickers`](app/services/trading/learning_predictions.py) / `get_current_predictions` as appropriate) + scoped pattern tickers; per-source caps; dedupe; session/asset gating unchanged.
- **Coverage:** `pattern_rule_indicator_keys`, coverage ratio, missing-field list; **stricter** minimum for main dispatch than “2 evaluable clauses”; skip reasons in summary.
- **Ranking:** deterministic **composite** (quality first, ETA second); stop sorting primarily by `(eta_hi, -readiness)`.
- **Batching:** per-run caps per ticker / pattern; cooldown unchanged in spirit (AlertHistory pair); optional near-miss **log-only** (DEBUG or `LearningEvent` if low-noise).
- **BreakoutAlert:** on **actual** Tier-A-equivalent imminent **dispatch** (main channel), insert row with JSONB scorecard in `indicator_snapshot` (or `signals_snapshot`); **no** row for dry-run or near-miss.
- **Dry-run:** `pattern_imminent_debug_dry_run_enabled` or query param; full structured summary; no SMS.
- **Tests:** extend [`tests/test_pattern_imminent_alerts.py`](tests/test_pattern_imminent_alerts.py).

**Scheduler note:** `pattern_imminent_scanner` is under **`include_heavy`** (`CHILI_SCHEDULER_ROLE` in `all` | `worker`). **`web`-only role does not run it.** [`docker-compose.yml`](docker-compose.yml) `scheduler-worker` uses `all` → OK.

---

## Part 2 — Opportunity board service

**New:** [`app/services/trading/opportunity_board.py`](app/services/trading/opportunity_board.py)

**Entrypoint (adjust names to match repo style):**

`get_trading_opportunity_board(db, user_id=None, *, dry_run=False, include_research=False, include_debug=False, max_per_tier: dict | None = None) -> dict`

**Behavior:**

- **Single scoring pipeline** shared with imminent (import shared helpers from a small module e.g. `opportunity_scoring.py` or `pattern_imminent_alerts` re-exports — **one source of truth** for composite + coverage).
- **Inputs (native):**
  - Imminent-style **evaluated candidates** (reuse internal function that builds scored rows **before** Telegram gate — not duplicate scoring).
  - **Predictions:** `get_current_predictions` rows (tier C/B material).
  - **Scanner:** recent `ScanResult` (tier B/C).
  - **Prescreener:** global candidates from DB (tier C).
  - **Scoped patterns:** tickers from active patterns.
  - **Optional:** recent `BreakoutAlert` / `AlertHistory` winners for freshness bonus (grounded only).
- **Output JSON shape:** `generated_at`, `is_stale`, `session_context` (see addendum), `operator_summary` (counts + no_trade_now + session one-liner), `no_trade_now`, `no_trade_reason_codes[]`, `no_trade_summary_lines[]` (plain English for UI), `counts`, `tiers.*` (each candidate: `why_here`, `why_not_higher_tier`, `main_risk`, `sources[]`, `also_in_live_predictions` / `prediction_support`, metrics), `source_stats`, `suppressions` / `skip_reasons` **only when `debug=1`**.

**No fake data:** omit fields not available.

---

## Part 3 — Tiers (honest mapping in code)

| Tier | Label | Telegram default | In-app board |
|------|--------|------------------|-------------|
| **A** | Actionable now | **Yes** (same bar as current main imminent) | Yes |
| **B** | Watch soon | No | Yes |
| **C** | Watch today / swing | No | Yes |
| **D** | Research only | No | Only if `include_research` or debug |

**Tier rules (implement explicitly):**

- **A:** `lifecycle_stage in (promoted, live)` **or** legacy `promotion_status == promoted`, composite ≥ `opportunity_tier_a_min_score` (or reuse imminent min), coverage ≥ **strict** floor, ETA within “soon” window (e.g. ≤ 4h configurable), R:R sanity pass.
- **B:** high composite, slightly lower coverage or wider ETA (e.g. 15m–4h), still quality-filtered; may include strong scanner/prediction rows without full pattern match.
- **C:** predictions + prescreener + scanner context; lower urgency; explicit `why_tier`.
- **D:** candidate/backtested patterns, low coverage, near-misses; **never** main Telegram.

**`no_trade_now`:** `true` when tier A empty **and** (optional) no B/C above minimal floor; include `no_trade_reasons: string[]` (e.g. `session_closed`, `no_eligible_patterns`, `all_below_threshold`).

**Per candidate fields:** `why_here`, `why_not_higher_tier`, `main_risk` (always for operator UI), `score_breakdown`, `sources[]`, pattern snapshot, entry/stop/target, ETA, prediction overlap fields.

---

## Part 4 — Scoring coherence

- **One module** for: coverage ratio, pattern-quality subscore (win_rate, evidence, OOS, promotion), R:R sanity, overextension penalty, ETA bonus **secondary**.
- **Imminent dispatch** and **opportunity board** both call it — **no divergent formulas**; tier/Telegram differ **only** by thresholds and eligibility filters (see addendum §9).

---

## Part 5 — API (Part 6 in user spec)

- **Route:** `GET /api/trading/opportunity-board` in [`app/routers/trading_sub/ai.py`](app/routers/trading_sub/ai.py) (alongside other `/api/trading/brain/*`) **or** [`app/routers/trading.py`](app/routers/trading.py) — prefer **ai.py** for brain-domain consistency.
- **Query params:** `debug=1`, `include_research=1`, optional `dry_run=1` if board should skip side effects (board normally has none except logging).
- **Auth:** same as other brain endpoints (`get_identity_ctx` / paired user).

---

## Part 6 — Learning honesty (Part 7)

- **BreakoutAlert:** only on **real** imminent Tier-A (main channel) dispatch.
- **Board rows:** not alerts; optional future `LearningEvent` type `opportunity_board_exposure` — **defer** unless trivial; default **debug JSON only** to avoid noise.
- Document in code comment why.

---

## Part 8 — Validation deliverables (post-implementation narrative)

Provide in the implementation PR / final message:

1. Short summary of changes.
2. File list.
3. Migrations (default none if JSONB-only).
4. New settings keys.
5. Example JSON: imminent dry-run summary + opportunity-board response.
6. **Code-level diff Tier A vs B** (threshold names / filters).
7. **`no_trade_now` representation** in JSON.
8. Manual checklist: scheduler role, gating, BreakoutAlert SQL, universe sources, tiering, UI anchor, Telegram unchanged for B/C/D.
9. **UI behavior matrix** (addendum §10): fresh+A, fresh+B/C only, no-trade, stale.

---

## Files likely touched (execution order)

1. [`app/config.py`](app/config.py) — imminent + opportunity tier thresholds + universe caps + `opportunity_board_stale_seconds` + default `max_per_tier` caps.
2. New [`app/services/trading/opportunity_scoring.py`](app/services/trading/opportunity_scoring.py) (or equivalent) — shared composite + coverage.
3. [`app/services/trading/pattern_imminent_alerts.py`](app/services/trading/pattern_imminent_alerts.py) — refactor to use shared scoring; gating; universe; summary; BreakoutAlert.
4. New [`app/services/trading/opportunity_board.py`](app/services/trading/opportunity_board.py).
5. [`app/routers/trading_sub/ai.py`](app/routers/trading_sub/ai.py) — `GET .../opportunity-board`.
6. [`app/templates/brain.html`](app/templates/brain.html) + inline JS — `loadOpportunityBoard()`, status chips, Debug tab.
7. [`tests/test_pattern_imminent_alerts.py`](tests/test_pattern_imminent_alerts.py) + new `tests/test_opportunity_board.py` (light).

---

## Non-goals

- Auto-trading, broker orders.
- Full redesign of brain.html visual system — **extend** existing sections/tabs only.
- Replacing `getchili.app` — use as UX reference only.
