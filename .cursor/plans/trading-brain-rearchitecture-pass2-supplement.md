# Trading-brain rearchitecture — Pass 2 supplement (ARCHIVED)

**Superseded by:** [trading-brain-canonical-blueprint.md](trading-brain-canonical-blueprint.md) (single canonical blueprint). The content below is kept for history.

---

# Trading-brain rearchitecture — Pass 2 supplement (outcome feedback, governance, retention, security, migration gates)

This document **extends** the first-pass blueprint; it does not replace it. Core decisions (dedicated service, own DB, HTTP + events, stage jobs, C+ patterns, snapshot predictions, etc.) are unchanged.

---

## 1. What Remains Strong in the Existing Blueprint

- **Service boundary and strangler direction:** Dedicated trading-brain with its own database, main app retaining product UX and portfolio concerns, is the right split for this codebase (today `chili-brain` still delegates into `app.services.trading.learning` with shared `DATABASE_URL` — the blueprint correctly calls that out as incomplete extraction).
- **Snapshot-first predictions** vs ephemeral recompute: Matches the gap in current code where `get_current_predictions` uses **per-process SWR memory cache** and no immutable `prediction_snapshot` authority.
- **C+ decomposition** (definition / evaluation / lifecycle / allocation): Aligns with reality where `ScanPattern` collapses many concerns into one row (`rules_json`, `promotion_status`, JSONB OOS/paper fields) without separate evaluation-run or allocation-snapshot entities.
- **Medium-grained stage jobs** replacing the `run_learning_cycle` god-method: Grounded in `learning.py` sequencing (~25 steps, single function, in-process `_learning_status`).
- **Explicit freshness classes:** Directly addresses today’s mixed semantics (prediction cache TTL vs explicit ticker-list bypass vs live quotes inside prediction).
- **Phased migration with dual-write and parity thinking:** Reduces big-bang risk while the monolith still owns `trading_*` tables and routers call `ts.run_learning_cycle` / `get_current_predictions` directly.

---

## 2. Where the Existing Blueprint Is Still Underspecified

- **Closed-loop feedback:** Events were named at a high level; there was no **canonical outcome lineage** from prediction snapshot → proposal → order → fill → closed trade, no **attribution graph**, and no **anti–look-ahead** rules for feeding outcomes back into learning.
- **Promotion / allocation governance:** Subdomains were listed, but not **policy modules**, **decision records**, **hysteresis/churn control**, or the split between **deterministic gates** vs **model-assisted ranking** (today OOS gates and `promotion_status` live partly in `learning.py` / config `brain_oos_*`, partly on the row).
- **Retention and storage growth:** “Relational-first + JSON” without **TTL tiers**, **compaction**, **immutability classes**, or **when to move blobs** — critical for `PatternTradeRow`-scale growth, evaluation history, and replay.
- **Security / trust:** “Tokens from main app” without **threat model**, **network placement**, **event integrity**, or **tiered API hardening** (Admin/Research vs Product).
- **Migration acceptance:** Phases lacked **measurable done criteria**, **parity tolerances**, **rollback triggers**, and **invariants** (e.g. single-flight cycle, publication source of truth).

---

## 3. Revised Outcome Feedback Model

### 3.1 Purpose

Close the loop from **downstream execution and product state** into brain **evaluation, allocation confidence, and advisory model calibration** — without contaminating research with **hindsight** or **label leakage**.

### 3.2 Canonical entities (conceptual; implement as brain DB tables)

| Record | Role |
|--------|------|
| `opportunity_signal` | Optional internal: “brain scored this ticker at as_of” — may reference `prediction_line_id` or internal candidate id (not necessarily published). |
| `prediction_snapshot` / `prediction_line` | **Frozen** published (or internal-debug) forecast; `as_of_ts`, `universe_id`, `regime_snapshot_id`, `allocation_snapshot_id`, `model_version_ids[]`. |
| `strategy_proposal` | Distinct artifact: proposed levels, horizon, policy version; states below. |
| `execution_intent` | User/system chose to act: links proposal → broker submission (paper or live). |
| `trade_lifecycle` | Normalized execution: working / partial / filled / cancelled / expired; links to fills. |
| `outcome_observation` | **Normalized realized outcome** at a horizon (or at exit): return, MFE/MAE if computed, label_win, holding_bars — always tied to **label_cutoff_ts** and **horizon_spec**. |
| `attribution_edge` | Weighted contributions: pattern, model advisory, policy path — for analytics, not sovereign truth. |

### 3.3 Distinct lifecycle states (proposal + execution)

**Proposal:** `draft_internal` → `candidate` → `published` → `acted` | `expired` | `suppressed` | `retracted`  
**Execution:** `none` → `intent_created` → `submitted` → `partially_filled` → `filled` | `cancelled` | `rejected`  
**Outcome:** `pending` → `partial` (optional intraday marks) → `closed` (required for learning consumption)

**Paper vs real:** Every `execution_intent` and `trade_lifecycle` row carries `account_mode: paper|live` and optional `broker_account_id` (opaque id from main app).

### 3.4 Events the main app (or execution adapter) MUST emit

Envelope: `event_id` (UUID), `occurred_at`, `idempotency_key`, `correlation_id`, `schema_version`, `payload`.

| Event name | When | Payload essentials |
|------------|------|---------------------|
| `proposal.published` | User-visible proposal created from brain | `proposal_id`, `prediction_snapshot_id?`, `line_ids[]`, `universe_id` |
| `proposal.status_changed` | Approve/reject/expire | `proposal_id`, `from`, `to`, `actor_id` |
| `execution.intent_recorded` | User clicked trade / auto-route | `proposal_id`, `intent_id`, `paper|live`, `target_qty`, `limit_px?` |
| `execution.order_update` | Broker webhook/poll | `intent_id`, `broker_order_id`, `status`, `filled_qty`, `avg_px`, `updated_at` |
| `execution.fill` | Partial or full fill | `fill_id`, `intent_id`, `qty`, `px`, `fees`, `liquidity?` |
| `execution.position_closed` | Position flat or proposal horizon exit | `intent_id` or `trade_id`, `exit_px`, `exit_ts`, `realized_pnl`, `holding_bars` |
| `proposal.cancelled` | Before or after partial fill | `proposal_id`, `reason` |

**Brain behavior:** Ingest → upsert normalized rows → emit internal `OutcomeObservationReady` when **labeling window** is satisfied (see 3.7).

### 3.5 Normalized realized outcomes

- **Primary key for learning:** `(source_ref_type, source_ref_id, horizon_name, label_cutoff_ts)` where `source_ref` is preferably `prediction_line_id` or `proposal_id` + `as_of_ts`, not “latest scan row.”
- **Returns:** Log return or simple return from **decision_px** (reference at publish or at first fill — **policy-chosen**, stored on artifact) to **exit_px** or **horizon_px**.
- **Slippage / latency (optional columns on fill/outcome):** `reference_px` (from proposal), `fill_px`, `entry_slippage_bps`, `signal_to_order_ms`, `order_to_fill_ms` — mirrors existing main-app ideas on `Trade` (`tca_*` columns in [`app/models/trading.py`](app/models/trading.py)) but owned in brain **outcome** tables for intelligence.

### 3.6 Attribution model (graph, not single winner)

- **`attribution_edge`:** `parent_type` (proposal_line | prediction_line), `child_type` (pattern | model_version | policy_rule_set), `weight` ∈ [0,1], `method` (shapley_approx | rule_contribution | uniform_over_matched_patterns), `artifact_id` pointing to **decision trace** JSON used at scoring time.
- **Pattern contribution:** Derived from **matched pattern list** stored on `prediction_line` at snapshot time (today-like “matched_patterns” but **persisted**, not recomputed).
- **Model advisory:** `model_version_id` + **delta to score** if available from meta-learner; if not, tag `presence_only`.
- **Policy/guardrail:** Reference `policy_evaluation_id` that recorded pass/fail and caps — outcomes **do not** train policy directly without a separate **offline policy eval** job.

### 3.7 Holding period / horizon labeling

- **Declare at publish time:** `horizon_spec` enum or struct: e.g. `{ "kind": "calendar", "days": 5 }`, `{ "kind": "bars", "n": 10, "timeframe": "1d" }`, `{ "kind": "exit_rule", "rule_id": "..." }`.
- **`label_cutoff_ts`:** Computed deterministically from `as_of_ts` + horizon; **outcome_observation** is immutable after lock.
- **Avoid hindsight:** Learning jobs may only consume `outcome_observation` rows where `label_cutoff_ts < job_as_of_ts` (enforced in SQL + coordinator).

### 3.8 Feedback consumers (what updates)

| Consumer | Update |
|----------|--------|
| **Pattern evaluation** | New **evaluation_run** including live/paper buckets; rolling metrics; **no** rewrite of historical `evaluation_run` rows — append new. |
| **Allocation confidence** | Updates **allocation scores** / weights on `pattern_allocation_rule` outputs; uses **decayed** evidence + recent outcomes. |
| **Proposal effectiveness** | `proposal_outcome_summary` aggregates by universe, regime, pattern bundle — feeds **publication** and **policy** thresholds. |
| **Advisory models** | **Offline** `model_eval_run` and optional **retrain** job; training rows must reference **frozen** features at `as_of_ts`, not post-hoc features. |

### 3.9 Hindsight / leakage guardrails

- **Time-travel constraint:** All training/feature jobs take `data_cutoff_ts`; market_data reads **must** reject bars with `ts > data_cutoff_ts`.
- **Walk-forward discipline:** Outcome-labeled rows enter **only** the next `model_train_run` after lock — never the same run that produced the prediction (enforced by job DAG).
- **Shadow outcomes:** Paper fills can inform **shadow** lifecycle patterns without affecting **active** promotion until explicit promotion job.

---

## 4. Revised Promotion and Allocation Policy Framework

### 4.1 Policy modules (pluggable, versioned)

| Module | Deterministic? | Output artifact |
|--------|----------------|-----------------|
| `EvidenceThresholdPolicy` | Yes | `policy_evaluation_id`: min trades, min wins+losses, min time span, min tickers touched |
| `OosGatePolicy` | Yes | pass/fail + reasons (extends current `brain_oos_*` / bench walk-forward concepts) |
| `RegimeConditionalPolicy` | Yes | requires evidence **per regime bucket** or downgrades lifecycle |
| `LiquidityQualityPolicy` | Yes | min dollar volume, max spread proxy |
| `ChurnGuardPolicy` | Yes | min dwell time in state, max transitions per window |
| `ModelAssistRanker` | No (advisory) | **rank_score** suggestions; cannot override hard fails |
| `OperatorSuppressPolicy` | Yes | merges active `operator_override` windows |

**Rule:** **Hard gates = deterministic.** Meta-learner may **reorder** or **suggest throttle** within passes, never “promote” alone.

### 4.2 Promotion decision record (append-only)

`promotion_decision` (or `lifecycle_transition` with embedded JSON):

```json
{
  "pattern_definition_id": "...",
  "from_state": "probationary",
  "to_state": "active",
  "as_of_ts": "...",
  "universe_id": "...",
  "regime_snapshot_id": "...",
  "inputs": {
    "evaluation_run_ids": ["..."],
    "allocation_snapshot_id": "..."
  },
  "policy_bundle_version": "2025-03-21",
  "module_results": [
    {"module": "OosGatePolicy", "pass": true, "detail": {...}},
    {"module": "RegimeConditionalPolicy", "pass": true, "detail": {...}}
  ],
  "model_advisory": {"rank_score": 0.82, "model_version_id": "..."},
  "operator_override_id": null
}
```

### 4.3 Evidence quality and sample quality

- **Separate:** `statistical_evidence` (N, win rate CI width, OOS metrics) vs **market_quality** (avg spread, volume, borrow constraints if short).
- **Minimums:** Configured per `hypothesis_family` / universe (today’s compression vs high-vol split in code maps to **separate policy profiles**).

### 4.4 Regime-specific promotion

- **Regime-stratified buckets:** Evaluation runs produce metrics **per regime_snapshot.label**; promotion requires pass in **current** regime or **aggregate with penalty** (config).
- **Allocation:** `pattern_allocation_snapshot` stores **regime → weight map**; at scoring time, pick weights for **current** regime with fallback to default.

### 4.5 Degradation, throttling, cooldown

| State | Operational meaning |
|-------|---------------------|
| `shadow` | Scores internally; **not** in published allocation; outcomes still collected |
| `probationary` | Published with **reduced cap** (max names, max gross, tighter stop policy) |
| `active` | Full internal allocation subject to caps |
| `throttled` | Temporary reduce after drawdown spike or operator rule; **time-bounded** |
| `degraded` | Hard quality fail (data gaps, OOS breach); **no publication** until new evaluation pass |
| `retired` | No scoring; immutable history |
| `archived` | Compact summary only; detail in cold storage |

**Churn control:** `ChurnGuardPolicy` enforces e.g. min 7d in `probationary` before `active`, max 1 demotion/promotion per 14d unless **operator** or **severity-1** data bug.

### 4.6 Allocation / usage weights

- **Inputs:** Latest **passed** `evaluation_run`, `outcome_observation` rollups, regime, `operator_override`, liquidity.
- **Deterministic base weight:** e.g. monotonic function of OOS expectancy minus costs with floor/ceiling.
- **Model assist:** multiplicative **tilt** in [0.85, 1.15] unless disabled — **bounded** so models stay advisory.

### 4.7 Internal activation vs publication

- **Internal:** lifecycle may be `active` while **publication** marks pattern as `shadow_public` (UI sees nothing or “research only”).
- **Publication job** moves `prediction_snapshot` / pattern catalog to **product-visible** only when **both** lifecycle and publication policy pass.

---

## 5. Revised Artifact Retention and Storage Lifecycle Policy

### 5.1 Immutability classes

| Class | Rule |
|-------|------|
| **Immutable** | `prediction_snapshot`, `prediction_line`, `promotion_decision`, `operator_override`, sealed `outcome_observation`, published `artifact` with hash |
| **Append-only** | `evaluation_run`, `stage_job` history, event outbox |
| **Mutable operational** | `stage_job` while queued/running (status fields only), raw cache rows |

### 5.2 Tiered retention (defaults — tune per env)

| Category | Hot (queryable OLTP) | Warm (same DB, compressed/partitioned) | Cold (blob + index row) | Delete |
|----------|----------------------|----------------------------------------|-------------------------|--------|
| Raw quote/bar cache | 24–72h | 14d | 90d summary | After cold snapshot |
| OHLCV used for intelligence | 30–90d rolling | 1y | Multi-year in Parquet in object store | Never delete without legal hold policy |
| Regime snapshots | 90d | 2y | Forever (tiny) | — |
| Feature materializations | 7–30d | 90d | Archive per `learning_cycle_run` | Drop hot after snapshot sealed |
| Stage-job debug artifacts | 7d | 30d | 180d | Purge |
| Learning cycle summary artifacts | 90d | 2y | Forever | — |
| Prediction snapshots (published) | All | — | Move large JSON to blob after 1y | Retain header row forever |
| Proposal artifacts (internal) | 30d | 1y | 7y | — |
| Proposal (published product copy may stay in main DB) | N/A | N/A | Per main app | — |
| Pattern evaluation history | 180d hot detail | 2y | Forever summaries | Raw trades compacted |
| Retired patterns | Forever row; trim heavy JSON | — | Large eval blobs to cold | — |
| Model binaries / pickles | Current + N-3 versions hot | Older in blob | — | After N |
| Replay/simulation outputs | 14d | 90d | 1y | Purge |
| Operator audit | Forever | — | — | — |

### 5.3 Compaction / summarization

- **Pattern trade analytics:** Roll `PatternTradeRow`-like detail into **monthly rollup artifacts** (counts, mean return, hit rate by regime) — keep **raw sample** for last N trades per pattern for debugging.
- **Stage jobs:** Store **stdout digest** + S3 URI for full log in long runs.

### 5.4 Evolution to hybrid storage

- **Now:** Postgres JSONB + `content_hash` on `artifact`.
- **Later:** `artifact.storage_uri` (S3/Azure) + inline `summary_json`; DB holds **index + hash + ACL**.

---

## 6. Revised Security and Trust Boundary Model

### 6.1 Trust zones

| Zone | Components |
|------|------------|
| **Public / user** | Browser → main app only |
| **Product BFF** | Main app → brain **Product API** (server-side, no browser keys) |
| **Control plane** | Admin UI / automation → main app → brain **Admin API** with elevated token |
| **Research** | Internal network / VPN / localhost only → brain **Research API** |
| **Data plane** | Brain workers + DB + cache; no inbound from internet |

### 6.2 Service-to-service auth (main ↔ brain)

- **mTLS** (preferred in prod) **or** HMAC-signed requests with rotating secret + `X-Chili-Timestamp` anti-replay.
- **Product API:** OAuth2-style **client credential** JWT issued by main (short TTL, audience=`trading-brain`, scope=`brain:product:read`).
- **Admin API:** Separate scope `brain:admin:*`; issued only to main’s **operator** role backend — **never** exposed to browser.
- **Research API:** Scope `brain:research:*`; **disabled by default** in prod unless `RESEARCH_API_ENABLED=1` and **bind 127.0.0.1** or private SG.

### 6.3 Async events

- **Authenticity:** HMAC-SHA256 on payload + `event_id` dedup table (brain side) with **48h idempotency window**.
- **Integrity:** Payload hash stored in `integration_event` row before processing; reject on mismatch.
- **Authorization:** Only **main app’s** `execution_adapter` service account may emit `execution.*` events.

### 6.4 Operator overrides

- **Require:** `actor_id`, `reason` (enum + free text), `expires_at` for non-permanent overrides.
- **Dual-control (optional):** sensitive actions (`kill_switch`, `publication_force`) require two-person approval artifact in later phase.

### 6.5 Misuse protection

- **Rate limits** on Admin (stricter than Product).
- **Allowlist** of IPs for Research in prod.
- **Dangerous endpoints** (`replay`, `recompute_all`, `delete_artifact`) behind feature flag + extra scope.
- **Audit:** Every Admin mutation → `operator_audit_log` immutable row + structured log with `correlation_id`.

---

## 7. Revised Migration Acceptance Criteria and Rollback Gates

### 7.1 Phase 0 — Seams / freeze

- **Done:** No new direct router imports from `learning.py` except via `BrainFacade`; list in CODEOWNERS.
- **Invariant:** CI grep fails on new violations.
- **Rollback:** Revert facade-only commits (no schema change).

### 7.2 Phase 1 — `learning_cycle_run` + `stage_job` in DB

- **Done:** Every cycle creates rows; worker/API read same status (replace in-memory `_learning_status` for **authoritative** view).
- **Metrics:** 100% of cycles have `stage_job` rows for each step (allow `skipped` with reason artifact).
- **Gate:** No cycle with `running` > `learning_cycle_stale_seconds` without **alert** + auto `failed_stale`.
- **Rollback:** Feature flag to old status JSON; DB rows optional read.

### 7.3 Phase 2 — Single-flight + no overlap

- **Done:** Postgres advisory lock or `cycle_lease` row — **at most one** `running` cycle per **universe_id** (or global if single universe MVP).
- **Gate:** 0 incidents of overlapping `running` cycles in 7d soak.
- **Rollback:** Disable second worker replica; keep lock.

### 7.4 Phase 3 — Prediction snapshot dual-write

- **Done:** Every **published** product prediction path writes `prediction_snapshot` + lines; returns `snapshot_id`.
- **Parity:** For sample set of 50 tickers, **max score delta** vs legacy ≤ ε (define ε per score type, e.g. 0.01 for normalized score) on same `as_of` bar.
- **Publication lag:** p95 lag snapshot `created_at` − `as_of_ts` < **SLO** (e.g. 120s) for batch publish job.
- **Rollback:** Read path still legacy; dual-write off.

### 7.5 Phase 4 — Published predictions **only** from snapshots

- **Done:** Product API / main BFF never calls `get_current_predictions_impl` directly.
- **Gate:** Stale-read: product returns `freshness_class=published` + `snapshot_ts`; **no** silent live recompute except admin path with `debug=true` header.
- **Rollback:** Feature flag to re-enable live path for emergency.

### 7.6 Phase 5 — Outcome events + attribution

- **Done:** All fills/closes from main emit events; brain has **≥95%** join rate from `proposal_id` to outcomes in 30d.
- **Invariant:** No `outcome_observation` with `label_cutoff_ts` in the future relative to job `as_of`.
- **Rollback:** Disable outcome-driven promotion; evaluation uses backtest-only.

### 7.7 Phase 6 — Brain DB authority

- **Done:** Brain migrations own intelligence tables; main DB **read-only replica** or API for product-only data.
- **Replay parity:** ≥ **99%** of sampled snapshots match replay score within ε (known exceptions documented).
- **Rollback:** Re-point to shared DB read (read-only); disable brain writes.

### 7.8 Operational SLOs (examples)

| Metric | Target |
|--------|--------|
| Stage job failure rate | < 2% / day (excluding vendor maintenance windows) |
| Event processing lag | p95 < 30s from `occurred_at` to durable brain row |
| Worker queue depth | Bounded; alert if > N for > M minutes |
| Override audit completeness | 100% of Admin mutations have audit row |

### 7.9 Rollback signals (automatic or human)

- Publication SLO breach > 15m → **stop auto-publish**, serve last good snapshot.
- Replay parity drop > threshold → **halt promotion jobs**, notify operator.
- Event forgery / HMAC failure spike → **pause event ingestion**, read-only mode.

---

## 8. Additional Immediate Refactors Needed Because of These Revisions

1. **Outcome event ingest module** in monolith (thin): map existing `Trade`, `StrategyProposal`, broker webhooks → outbound events (even before brain split).
2. **Persist matched_patterns / decision trace** on prediction path (currently explanation is partly ephemeral) — prerequisite for attribution.
3. **Unify `horizon_spec`** on proposals (today `timeframe` string on `StrategyProposal` is a start — formalize).
4. **Policy bundle version** config object in repo (`policy_bundle_version.yaml` hash) — referenced by `promotion_decision`.
5. **Retention job** skeleton: TTL cleanup for cache tables + artifact compaction (no-op schedules first).
6. **Separate JWT scopes** in config docs for Product vs Admin brain routes (main app).
7. **Idempotency store** for events (`integration_event`) — small table early.
8. **Evaluation window** definitions shared between backtest OOS and live outcome rollups (one module).

---

## 9. Updated Open Questions / Assumptions

- **Who owns broker webhooks long-term** — main app only (recommended) vs brain — assumed **main app emits execution.* events**.
- **Multi-horizon labels:** If both 5d and 10d are required, **two** `outcome_observation` rows or one row with JSON — pick early for indexing.
- **Short selling / margin:** Attribution and liquidity policy may need borrow flag — absent in current models for many flows.
- **Crypto vs equity** outcome semantics (24/7 vs session) — `label_cutoff_ts` calendar must be **per asset_class**.
- **Legal/compliance retention:** Warm/cold durations may require **user-configurable** retention policy object.
- **SQLite dev:** Row-level `SKIP LOCKED` unavailable — use **Postgres-only** for worker dev or degrade to single worker with documented limitation.
