# Trading brain: symptoms, architecture, path to more trades (brainstorm v2)

## Plain language: terms from v1

### “Promotion scarcity is a feature of OOS-style gates…”

- **In-sample (IS)**: You fit or tune a pattern on historical data. It is easy to find rules that “won” on that same data.
- **Out-of-sample (OOS)**: You test on data that was **not** used to tune that rule. Many patterns fail here — that is **promotion scarcity** (few patterns pass).
- **Recall vs robustness**: If you **lower** the bar, you get **more** signals (**higher recall**) but more of them may be **lucky noise** (**less robust**). If you **raise** the bar, you get **fewer** promotions but each one is more **trustworthy** on unseen data.
- **Overfitting**: The pattern memorized the past; it does not generalize. **Walk-forward**, **purge/embargo** (not using overlapping future information when labeling), and **locked parameters on OOS** are standard ways to reduce that. Public summaries: e.g. [AlgoXpert IS/WFA/OOS](https://ideas.repec.org/p/arx/papers/2603.09219.pdf); **López de Prado** (*Advances in Financial Machine Learning*) is the common reference for **purging** training sets so labels do not leak across time.

**Bottom line**: CHILI’s OOS-style gates are **designed** to say “no” a lot. That is why you can see mining/backtests **running** but **few tradeable promotions**.

### “Confirm one writer to `run_learning_cycle`…”

`run_learning_cycle` is **one big job** (scan, mine, queue backtests, etc.). **Only one** of these should run at a time for the same database, or they **fight** (duplicate work, locks, or one path returning “already in progress”).

**Writers** that can start it:

1. **`scripts/brain_worker.py`** (loop or wake).
2. **APScheduler** in the web app: the **hourly full `run_learning_cycle` job is disabled** in code (comment in [`trading_scheduler.py`](app/services/trading_scheduler.py) — “Brain Worker now handles…”). The scheduler still runs **price monitor, crypto/stock breakout scans, pattern imminent**, etc.
3. **Manual “Learn”** ([`POST /api/brain/trading/learn`](app/routers/brain.py)).

**Lease enforcement** (`brain_cycle_lease_enforcement_enabled` in [`app/config.py`](app/config.py)): when **on**, the app uses a **lease** so a second starter gets **rejected** instead of running in parallel. When **off**, you still have an in-memory **“already running”** flag that can block overlapping cycles in the **same process**, but **two processes** (worker + web) could still both run unless lease coordinates them.

**Action for you**: Decide **one primary runner** (usually **worker** *or* **scheduler**, not both blindly), and check whether lease is **on** in your `.env`.

### “Confirm worker and app share `DATABASE_URL` and `user_id`…”

- **`DATABASE_URL`**: Worker and FastAPI must point at the **same Postgres** (or you learn in one DB and read another).
- **`user_id`**: Learning often uses `brain_default_user_id` from settings; **proposals/alerts** may use **your logged-in user**. If those diverge, you can see patterns in the DB but **proposals** attached to a different user or **empty** for you.

**You said**: it should be shared — **verify** in both processes’ environment (Docker compose vs local worker).

---

## User clarifications (captured)

### Target: “60 trades” meaning

- **Intent**: Trade **count** like **backtests** — each **entry + exit** = one trade; multiple trades per ticker over time when the setup repeats.
- **Not a hard SLA**: Does not have to be literally 60/day; that number is **directional** (high-throughput, many discrete opportunities).

### Throughput vs quality / continuous learning

- **Separating “research batch” from “intraday signal scan”** does **not** mean the brain stops learning.
- **Learning** can (and should) continue from: resolved backtests, **accepted vs rejected** proposals, **live/paper** outcomes, breakout resolution, hypothesis pool updates, etc. — i.e. **all successes and failures** you care about.
- The split only means: **do not re-run full universe mining every N minutes** just to **refresh signals**; run **lighter** “evaluate already-known patterns on current bars” more often, while **heavy** discovery stays on a **slower** cadence. That **improves** operational quality (less duplicate work, clearer attribution) without turning off feedback loops.

### Runtime: worker vs scheduled Learn — “don’t they do the same?”

**Yes — same function** (`run_learning_cycle`), **different triggers**:

| Trigger | Process | Typical use |
|--------|---------|-------------|
| Brain worker loop / wake | Separate Python process | Dedicated machine; long runs |
| APScheduler | Inside uvicorn | Always-on server |
| Manual Learn | Inside uvicorn (background task) | On-demand |

**Problem if both run**: **double work**, **contention**, or **blocked** cycles — not because the code differs, but because **two schedulers** fire the **same** pipeline. **Mitigation**: prefer **one** scheduled path + optional manual; use **lease** if multiple processes exist.

---

## Gap checklist (unchanged substance)

1. **Runtime**: Which single primary triggers `run_learning_cycle`? Is `brain_cycle_lease_enforcement_enabled` true in prod?
2. **DB/user**: Same `DATABASE_URL`; align `brain_default_user_id` with the user who should see proposals/alerts.
3. **Funnel**: `promotion_status` histogram, queue pending/empty, queue debug IDs.
4. **Data**: Crypto OHLCV path and variant fallbacks (Massive/Polygon rules).
5. **Alerts**: `alert_min_score_proposal` / R:R / SMS config vs observed top picks.

---

## Product direction notes (updated)

- Aspire to **many discrete trades** (backtest-style counting) across tickers and time — implement via **more frequent evaluation** of **validated** patterns + **clear promotion tiers**, not only by loosening OOS (which trades robustness for volume).
- Preserve **feedback from failures and acceptances** as explicit **learning events** (journaling, hypothesis updates, proposal outcomes) — orthogonal to how often you **scan** the full market.

---

## Open verification (still optional)

- [ ] `.env` / compose: `DATABASE_URL` identical for `chili` service and `brain_worker` (if separate container).
- [ ] `brain_default_user_id` and your UI user for proposals.
- [ ] `brain_cycle_lease_enforcement_enabled` and whether scheduler + worker are both enabled.

---

## Live diagnosis run (this workspace, `DATABASE_URL` → `localhost:5433`, 2026-03-27)

Executed read-only SQL against your Postgres (not Docker-in-container). Results:

| Check | Finding |
|--------|---------|
| **`scan_patterns` total** | 100 rows |
| **Active + `promoted`** | **1** (single promoted pattern live) |
| **Active + `legacy`** | 23 (mostly `entry_variant` / `builtin` — these use the **legacy** promotion path, not strict OOS for `web_discovered`/`brain_discovered`) |
| **The one promoted pattern** | `origin='web_discovered'` only |
| **Active by asset_class** | `stocks` 12, `crypto` 8, `all` 4 |
| **Active + never backtested** | 0 (queue is not stuck on “never tested”) |
| **`brain_worker_control` heartbeat** | Present and recent (worker process updating DB) |
| **`trading_proposals`** | 5 `pending`, 14 `expired`, 415 `rejected` — proposals **do** get created; volume is mostly rejections/expiry, not “zero pipeline” |
| **Recent scans (`trading_scans`, 6h)** | 61 rows with `signal=buy` and `score >= 6`; 73 rows with crypto-style tickers (`%-USD`) — **scanner feed is healthy** |
| **`.env`** | `CHILI_BRAIN_DEFAULT_USER_ID` **not set** (insights/proposals may use `NULL` vs your login — worth setting to your `users.id` if you want attribution aligned) |
| **Lease** | Default in repo [`app/config.py`](app/config.py): `brain_cycle_lease_enforcement_enabled=False` unless overridden in `.env` |

**Interpretation**

- The brain **is** touching the DB (heartbeat, scans, proposals). “Nothing happens” is **not** empty mining globally — it is **tight promotion** (only one **promoted** pattern) plus **strict last-mile proposal filters** (`combined_score` / R:R / levels in [`alerts.py`](app/services/trading/alerts.py) + [`scanner.py`](app/services/trading/scanner.py)).
- **Weak predictions** plausibly reflect **few high-conviction promoted patterns** and blending with many **legacy** variants, not absence of crypto **scans** (crypto rows exist in `trading_scans`).

**Highest-leverage work items (pick with intent)**

1. **Proposal / alert path** — Log or UI-debug why picks fail after `trading_scans`: `combined_score` vs `alert_min_score_proposal` (7.5), missing stop/target from quote, R:R floor. *Data shows scan candidates exist; gating likely here.*
2. **Promotion / “tradeable” story** — Decide if you need **more `promoted` (OOS-passed)** patterns vs relying on **legacy** active patterns for predictions; tune `brain_oos_*` / tradeable list gates or miner→pattern bridge (`brain_miner_scanpattern_bridge_enabled` is default off).
3. **Identity** — Set `CHILI_BRAIN_DEFAULT_USER_ID=<your_user_id>` in `.env` so worker-mined rows and proposals line up with the account you use in the UI.
4. **Optional hardening** — Enable `BRAIN_CYCLE_LEASE_ENFORCEMENT_ENABLED=true` if **both** manual Learn and `brain_worker` can run and you want single-flight across processes.

---

## User intent (2026-03-27): proposals + worker “idle”

### Proposals must follow **strong / brain-discovered** patterns only

**Today (roughly):** [`generate_top_picks`](app/services/trading/scanner.py) merges recent **scans** (`score >= 6`, `buy`) with **brain predictions** (`bullish`, `confidence >= 50`). It attaches `scan_pattern_id` from the **strongest matched pattern** on that prediction, with **no** filter on `ScanPattern.promotion_status` or `origin`. So a proposal can still be driven by **legacy / variant / builtin** matches you experience as “weak.”

**Desired (user choice):** Proposals must be tied to patterns that **passed the brain’s promotion pipeline**, without dumbing down the rest of the system:

- **Require `promotion_status == 'promoted'`** on the linked `ScanPattern` (any `origin` — including variants that earned promotion).  
- **Do not** use `origin` as an extra filter if the pattern is already **promoted** (that preserves quality wherever the brain legitimately promoted, e.g. evolved variants).  
- **Scope of change:** **Last-mile only** — filter **who gets a strategy proposal / SMS**, not mining, OOS gates, evolution, or scan scoring. Avoid cutting research quality; avoid proposing off **non-promoted** (“weak”) pattern matches only.

**Execution-phase sketch (when approved):**

- In [`generate_strategy_proposals`](app/services/trading/alerts.py): require `pick.get("scan_pattern_id")`, load `ScanPattern`, **skip** if missing or `promotion_status != 'promoted'` or `active` is false.  
- Optionally mirror the same rule in [`_generate_top_picks_impl`](app/services/trading/scanner.py) for consistency so downstream consumers do not see misleading `combined_score` on non-promoted pattern IDs.  
- **Scan-only picks** without a promoted pattern link: **do not** create proposals (or only after explicit future “tier 2” product — default off).

**User constraint:** Do not weaken trading-brain logic globally; only **tighten proposal eligibility** to promoted patterns.

### Why the worker goes “idle” when patterns still exist in the DB

**You are making sense** as a *goal* (“always chew through the backlog”). The current worker is **not** implemented as an infinite “while queue non-empty” drain; it is a **pulse** design:

1. **Run one full `run_learning_cycle`** (heavy: scan, mine, up to `brain_queue_batch_size` queue backtests, etc.).
2. **Then sleep on purpose**:
   - **~1 minute** if this cycle did backtests, added exploration patterns, or **`queue_pending > 0`** (more patterns **eligible** for the *priority queue* soon),  
   - otherwise **`--interval` minutes** (Compose default **5**): log line *“Retest queue clear and idle cycle…”* — see [`scripts/brain_worker.py`](scripts/brain_worker.py) (~664–715).

**“Patterns in DB” ≠ “eligible for the backtest queue this minute”:** [`get_pending_patterns`](app/services/trading/backtest_queue.py) only includes **active** patterns that are **boosted**, **never tested**, or **`last_backtest_at` older than `brain_retest_interval_days`** (default **7**). Your diagnosis showed **0** active+never-tested — so many rows are **waiting out the retest window**, not starving because the worker stopped forever.

**Summary:** Idle is **scheduled breathing room** and **queue eligibility rules**, not proof that the database has zero `scan_patterns`. Changing behavior would be a **product/engineering change** (e.g. shorter retest interval, larger batch, continuous drain mode, or separate lightweight “signal eval” loop) — not a bug that the worker sleeps after a cycle.

### Breadth **and** depth: “crafting the winning setup”

**User point:** The gap is not only **breadth** (how many tickers/signals/cycles) but **depth** — how fully the system *composes* a credible trade story (regime, structure, liquidity/friction, entry/exit logic, failure modes, correlation to book, etc.).

**Assessment — yes, with a nuance:**

- **Depth exists in the pipeline** but is **split across many steps** ([`run_learning_cycle`](app/services/trading/learning.py): miners, queue backtests with friction + OOS, variant evolution, meta-ML, journal, breakout learning, …). That depth **does not always surface** as one rich “setup object” in the UI or proposals; the **last mile** is still **thin** (pick + score + stop/target + thesis string).
- **Promotion scarcity** and **legacy vs promoted** split mean **few patterns** pass the strict bar — so even deep backtests **rarely** become the **single promoted anchor** you experience as “the brain crafted this.”
- **Depth without breadth** feels underfed: one promoted pattern × routine scan breadth ≠ a desk-grade **multi-constraint** setup generator.

**Directions (planning only, for later phases):**

- **Surface depth:** For proposals, attach **evidence bundle** (pattern name, OOS summary, regime at signal, key conditions met, conflict checks) — not just thresholds.  
- **Compound depth:** Require **multiple independent agrees** for highest tier (e.g. promoted pattern + scan alignment + optional breakout tier), configurable.  
- **Keep research deep, alerts selective:** User direction already — **promoted-only proposals** increases **depth per alert** at the cost of **fewer** alerts until more patterns promote.

**Conclusion:** Your instinct is right: today’s product path emphasizes **pipeline volume + gating**, not **one deeply reasoned setup artifact**. Closing that is mostly **integration and presentation of existing research outputs**, plus **more promoted patterns** over time — not only “run the worker more often.”

---

## Filling the breadth + depth gap (“brain isn’t processing anything”)

**Reframe:** The worker **does** run (heartbeat, scans, queue steps), but **perceived processing** is low because (a) **few outputs cross the bar** you care about (`promoted`, proposals), (b) **work between cycles is invisible**, (c) **last-mile artifacts are thin**. The fix is **three parallel tracks**: prove activity, widen validated throughput where safe, and **surface depth** on the few things that matter.

### Track 1 — Visibility (prove the brain is working)

Without changing alpha logic, make processing **audible**:

- **Cycle digest** after each `run_learning_cycle`: persist or expose counts already in the `report` dict (tickers scanned, patterns mined, queue backtests run, promotions/rejections, proposals created/skipped with **reason codes**). Surface on Brain UI or `/api/brain/trading/metrics` extension.
- **Proposal skip reasons** (structured): e.g. `no_promoted_pattern`, `below_combined_threshold`, `bad_rr`, `quote_missing` — so “nothing happens” becomes **actionable**.

*Outcome:* You stop conflating “quiet product” with “idle brain.”

**User note:** No standalone DB diagnostic script in scope.

### Track 2 — Breadth (more real work per calendar week, without fake signals)

Tune **eligibility and cadence** so the same pipeline chews more **meaningful** volume:

- **Queue / retest:** Lower `brain_retest_interval_days` *or* raise `brain_queue_batch_size` *carefully* (watch Postgres pool + provider RPS). Goal: **more pattern-years re-evaluated**, not just sleep.
- **Exploration:** Confirm `brain_queue_exploration_enabled` / `brain_queue_exploration_max` are effective; if funnel is top-heavy with stale actives, exploration is what **pulls** under-tested patterns forward.
- **Identity:** Set `CHILI_BRAIN_DEFAULT_USER_ID` so worker output and UI **same user**.

#### Promoted-pattern fast eval (**user priority** — separate from full learning cycle)

Lightweight, **high-frequency** job that does **not** run `mine_patterns`, queue backtests, or evolution — only scores **live** tickers against patterns that are **`promotion_status='promoted'`** and **`active`**.

**Intent:** More “brain on ticker X **now**” (matches, strength, directional hints) **without** paying full-cycle OHLCV/CPU every time.

**Execution-phase sketch (when approved):**

- **Tickers:** Reuse [`_build_prediction_tickers`](app/services/trading/learning.py) or a slimmer list + cap (new setting e.g. `brain_fast_eval_max_tickers`).
- **Patterns:** Load promoted-only subset (filter after `get_active_patterns` or dedicated query).
- **Core:** Indicator snapshot per ticker → existing [`evaluate_patterns_with_strength`](app/services/trading/pattern_engine.py) / prediction blending; optional thin entrypoint `run_promoted_pattern_fast_eval(db)` returning structured rows.
- **Output:** Minimum: feed the same prediction cache / Brain UI path so lists refresh between heavy cycles; optional: append-only events or mirror rows if you already use prediction mirror infra.
- **Scheduler:** New interval job in [`trading_scheduler.py`](app/services/trading_scheduler.py) (e.g. **5–15 min**, `brain_fast_eval_interval_minutes`), `max_instances=1`. Must **not** toggle global `_learning_status["running"]` or contend with learning lease — fast eval is a **separate short session**.

*Outcome:* **Live breadth** on **validated** patterns only; pairs with **promoted-only proposals** (Track 3).

*Track 2 overall:* More **touch points** between data and patterns; queue tuning still feeds **research** depth over time.

### Track 3 — Depth (compose the “setup” users expect)

Bundle research outputs into a **single setup view** (quant-style **signal + context + evidence**):

- **Evidence bundle** on `StrategyProposal` (or parallel JSON): `ScanPattern` id, name, `promotion_status`, **OOS headline** (from stored bench/summary if present), **conditions snapshot** at signal time, **VIX/regime** string, link to last `BacktestResult` ids.
- **Multi-agree tier (configurable):** Highest tier only when e.g. **promoted pattern match** + **scan score floor** + optional **breakout/imminent** flag — aligns with user’s **depth over spam**.
- **Promoted-only proposals** (already agreed): implements **depth per alert**; pair with Track 1 skip reasons so empty states explain **which gate** to tune next.

*Outcome:* The product **reads** like a quant desk note, not a lone number.

### Track 4 — Throughput to `promoted` (research, higher risk)

Only after Tracks 1–3 or in parallel with care:

- **Miner → ScanPattern bridge** (`brain_miner_scanpattern_bridge_enabled`) or tuned prescreen so more hypotheses enter the **same** OOS queue (does not bypass gates).
- **OOS tuning:** If promotions are **zero**, review `brain_oos_*` vs asset class; loosening **raises false positives** — do only with **Track 1** metrics to watch.

### Suggested order

1. **Track 1** (visibility + proposal skip reasons) — fastest trust win.  
2. **Promoted-pattern fast eval** (Track 2 subsection) — **user priority**; improves live “brain on name X” without full cycles.  
3. **Track 3** evidence bundle + **promoted-only proposals** — depth and quality bar.  
4. **Track 2** remainder (queue/retest/exploration/identity tuning).  
5. **Track 4** only with metrics.

### Non-goals (for this gap-fill)

- Disabling OOS wholesale to “feel busy.”  
- Replacing the planner or adding a second orchestration brain.  
- Promising 60 trades/day without capacity and correlation design.
