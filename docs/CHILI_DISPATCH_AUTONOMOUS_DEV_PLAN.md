# CHILI Dispatch — Autonomous Self-Coding Brain

**Author:** Claude (research preview)
**Date:** 2026-04-24
**Status:** Proposal — phased rollout, frozen-scope review required
**Related rules:** `CLAUDE.md` Hard Rules 1–6, `.cursor/rules/*.mdc`

---

## 1. Why this exists

You are paying for two premium coding LLMs (Cursor Max + Claude Pro) and currently bridging them by hand: when one runs out, you copy a Claude-Opus answer into Cursor's free Composer 2 to keep going. That bridge is a queue you are walking by hand.

You already have:

- A working **OpenAI key** wired up in the app (`PAID_OPENAI_API_KEY`, `gpt-4o-mini` default — `app/openai_client.py:39–40`).
- A multi-provider **LLM cascade** with weak-response detection (Groq → OpenAI → Gemini, `openai_client.py:489–520`).
- A running **Ollama** service in Docker on `:11434` (`docker-compose.yml:52–78`) — currently configured but **not** in the active cascade.
- A **Code Brain** schema (`app/models/code_brain.py`) with repos, insights, snapshots, hotspots, learning events, dependencies, quality snapshots, reviews, dep-alerts, and a search index.
- A **single-shot agent loop** already implemented: `planner_coding.api_agent_suggest` → `agent_suggest.run_agent_suggest_for_task` → `agent_suggestion_store.insert_suggestion` → `api_apply_agent_suggestion` (git apply) → `api_run_validation` (`run_phase1_validation`).
- A **continuous worker daemon**: `scripts/brain_worker.py` (lean-cycle every 5 min) + `scripts/scheduler_worker.py` (APScheduler with full role).
- The exact pattern to mirror: trading's `auto_trader.py` — *rule gates → LLM revalidation → audit → execute → kill-switch* — already runs autonomously over real money. Code is lower risk than money.

The gap is small. Five deliberate additions turn this into an unattended, self-improving coder:

1. **Prompt/completion ledger** — capture every (prompt, response, model, validation outcome) tuple. This is the distillation training set; without it, no local model ever gets smarter.
2. **Code Dispatch loop** — a `code_learning_cycle` that picks tasks → drafts → tests → commits → audits, mirroring `learning.py`'s 13-step cycle.
3. **Code kill-switch + frozen-scope guard** — extends `governance.py` with `is_code_agent_enabled()`; auto-halts on consecutive failures and blocks merges that touch frozen paths.
4. **Multi-LLM router with Ollama-first** — extends `openai_client.chat()` so cheap/local models handle easy tasks and premium models only fire when complexity demands it.
5. **Distillation pipeline** — periodic job that filters successful (prompt, completion, validation) rows, fine-tunes a local Qwen2.5-Coder / DeepSeek-Coder via Ollama, runs an eval gate, and promotes the new local model into the router using the existing **ensemble_promotion_check** pattern.

Net effect: as the system runs, the local model absorbs your real workflow. Within weeks of operation, the share of tasks the local model can handle should climb monotonically — premium tokens get spent only on what the local model actually fails at, then those failures themselves become the next training batch.

---

## 2. Current state — what's already there

### 2.1 Brain subsystems (verified by code audit)

| Subsystem | File(s) | Status | Reuse for Dispatch? |
|---|---|---|---|
| Trading learning cycle | `app/services/trading/learning.py` (393KB) | Production | Architectural template only |
| Trading brain phased migration | `app/trading_brain/` (50+ files, phases 2–8) | Frozen authority contract | Copy hexagonal structure |
| Code Brain models | `app/models/code_brain.py` | 10 tables present | **Direct reuse** |
| Coding planner API | `app/routers/planner_coding.py` (698 lines) | Single-shot agent loop | **Direct reuse**; wrap in dispatcher |
| Brain worker | `scripts/brain_worker.py` | Running every 5 min | **Add `code-cycle` mode** |
| Scheduler worker | `scripts/scheduler_worker.py` | APScheduler, role=all | **Add `code_learning_cycle` job** |
| Auto-trader (gate→LLM→audit) | `app/services/trading/auto_trader.py` | Production | Architectural template |
| Governance (kill switch) | `app/services/trading/governance.py` | Production | **Extend** with code agent flag |
| Promotion gate (CPCV/LightGBM) | `app/services/trading/promotion_gate.py` | Production | Template for code promotion gate |
| Pattern-regime promotion | `app/services/trading/pattern_regime_promotion_service.py` | Production | Template for model promotion |
| LLM cascade | `app/openai_client.py` (Groq → OpenAI → Gemini) | Production | **Extend with Ollama-first** |
| Ollama runtime | `docker-compose.yml:52–78` | Running, unwired | **Wire into cascade** |
| Validation runner | `services/coding_task/service.py:run_validation_for_task` | Production | Reuse as autonomous-loop oracle |

### 2.2 Critical gaps

1. **No prompt/completion logging table.** `app/llm_caller.py` only has an in-process LRU cache (256 entries, 600s TTL). When the process exits, all prompt/response history dies. **Without persistence, distillation is impossible.**
2. **Ollama is configured but not in the cascade.** `CLAUDE.md` claims "Ollama → NLU parser → Groq/Gemini" but `openai_client.chat()` (line 489–520) doesn't call Ollama. Document and code disagree.
3. **Agent loop is single-shot.** `api_agent_suggest` → `api_apply_agent_suggestion` runs once. If validation fails, the task is marked `blocked` and waits for a human. There is no auto re-prompt-with-validation-feedback loop.
4. **No code kill-switch.** Trading has one; code execution has none. If the agent goes off the rails (bad model, broken cascade, hostile prompt injection in task description), nothing stops it from spamming bad commits.
5. **No frozen-scope guard at commit time.** Hard Rules 1–6 in `CLAUDE.md` are documentation; nothing in code blocks an agent from editing `services/trading/`, `app/migrations.py`, or `governance.py` and merging the diff.

### 2.3 Live site (getchili.app) — what's exposed

The site itself isn't reachable from the sandboxed network and isn't indexed by web search, so this section is reconstructed from the deployed surface in the repo (13 routers in `app/main.py:869–880`):

- **Public-ish surface:** `pages.router` serves home / chores / birthdays; `chat.router` is the chat UI; `auth.router` handles pairing.
- **Paired-only:** `trading.router` (trading desk + tabs), `brain.router` (trading brain UI), `brain_project.router` (project domain), `planner_coding` endpoints (require `_require_user`), `admin.router`.
- **Mobile:** `chili_mobile/` Flutter app — dashboard, chat, voice, wake-word calibrator.
- **Deploy:** Dockerfile + docker-compose only. No GitHub Actions / Render / Fly config in repo, so prod is almost certainly Compose on a VPS / your own box. **There is no public CI gate** — meaning if Dispatch auto-merges to `main`, it merges straight into prod the next time you `docker compose up`. This raises the importance of branch-discipline and frozen-scope guards.

---

## 3. Target architecture — Chili Dispatch

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SCHEDULER WORKER (APScheduler)                    │
│                                                                       │
│  every 60s ──► code_learning_cycle()                                 │
│                    │                                                  │
│                    ▼                                                  │
│       ┌──────────────────────────────────────┐                       │
│       │ 1. MINE   pick next coding task      │  ←  project_domain   │
│       │           (from planner queue,       │     planner_tasks    │
│       │            recent failures, code     │     code_hotspots    │
│       │            hotspots, dep alerts)     │                       │
│       └────────────────┬─────────────────────┘                       │
│                        ▼                                              │
│       ┌──────────────────────────────────────┐                       │
│       │ 2. SCORE  task_complexity + cost     │                       │
│       │           estimate → choose tier:    │                       │
│       │           tier-1 ollama-local        │                       │
│       │           tier-2 groq (free)         │                       │
│       │           tier-3 openai gpt-4o-mini  │                       │
│       │           tier-4 openai gpt-4o       │                       │
│       └────────────────┬─────────────────────┘                       │
│                        ▼                                              │
│       ┌──────────────────────────────────────┐                       │
│       │ 3. RULE GATE (deterministic)         │                       │
│       │   - kill switch active? → veto       │                       │
│       │   - frozen scope detected? → escalate│                       │
│       │   - velocity limit hit? → defer      │                       │
│       │   - drawdown analogue:                │                       │
│       │     N consecutive failures → veto    │                       │
│       └────────────────┬─────────────────────┘                       │
│                        ▼                                              │
│       ┌──────────────────────────────────────┐                       │
│       │ 4. DRAFT  build_bounded_prompt()     │  ──►  llm_call_log   │
│       │           run_agent_suggest_for_task │     (NEW)            │
│       │           (existing path)            │                       │
│       └────────────────┬─────────────────────┘                       │
│                        ▼                                              │
│       ┌──────────────────────────────────────┐                       │
│       │ 5. APPLY  git checkout -b dispatch/  │                       │
│       │           apply_agent_suggestion()   │                       │
│       │           (sandboxed worktree)       │                       │
│       └────────────────┬─────────────────────┘                       │
│                        ▼                                              │
│       ┌──────────────────────────────────────┐                       │
│       │ 6. VALIDATE run_validation_for_task  │  ──►  CodingValidation│
│       │            pytest, lint, parity      │     ArtifactRows     │
│       └────────────────┬─────────────────────┘                       │
│                        │                                              │
│              ┌─────────┴──────────┐                                   │
│              ▼ pass               ▼ fail                              │
│       ┌─────────────┐      ┌─────────────────┐                       │
│       │ 7a. PROMOTE │      │ 7b. RE-PROMPT   │                       │
│       │   merge to  │      │   feed failure  │                       │
│       │   feat/*    │      │   into prompt,  │                       │
│       │   (frozen-  │      │   bump tier,    │                       │
│       │    scope    │      │   max 3 retries │                       │
│       │    guard)   │      │   then escalate │                       │
│       └──────┬──────┘      └────────┬────────┘                       │
│              │                       │                                │
│              ▼                       ▼                                │
│       ┌──────────────────────────────────────┐                       │
│       │ 8. AUDIT  CodeAgentRun row           │  ──►  notification   │
│       │           (mirrors AutoTraderRun)    │     bus only when    │
│       │                                      │     escalation set   │
│       └──────────────────────────────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘

every 6h ──► distillation_cycle()
                │
                ▼
   read llm_call_log WHERE validation_status = 'passed'
                │
                ▼
   filter: complexity >= median, dedupe by prompt-hash
                │
                ▼
   export JSONL → fine-tune Qwen2.5-Coder via Ollama
                │
                ▼
   eval against held-out set (100 tasks)
                │
                ▼
   ensemble_code_promotion_check:
     - new_model_pass_rate >= incumbent_pass_rate
     - validation latency <= incumbent + 20%
     - no regressions on golden tasks
                │
            ┌───┴────┐
            ▼ pass   ▼ fail
        promote     keep incumbent;
        as new      log regression;
        tier-1      stash artifact
```

### 3.1 New tables (migration `171_chili_dispatch_tables`)

```sql
CREATE TABLE llm_call_log (
    id            BIGSERIAL PRIMARY KEY,
    trace_id      TEXT NOT NULL,
    cycle_id      BIGINT,                  -- FK → code_agent_runs.id
    provider      TEXT NOT NULL,           -- 'ollama' | 'groq' | 'openai' | 'gemini' | 'anthropic'
    model         TEXT NOT NULL,
    tier          INTEGER NOT NULL,        -- 1..4 in router order
    purpose       TEXT NOT NULL,           -- 'code_draft' | 'code_review' | 'plan' | 'naming' | ...
    system_prompt TEXT,
    user_prompt   TEXT NOT NULL,
    completion    TEXT,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    latency_ms    INTEGER,
    cost_usd      NUMERIC(10,6),
    success       BOOLEAN,
    weak_response BOOLEAN DEFAULT FALSE,   -- triggered cascade escalation?
    failure_kind  TEXT,                    -- 'refusal' | 'short' | 'timeout' | 'rate_limit' | NULL
    validation_status TEXT,                -- 'passed' | 'failed' | NULL (set by post-hoc validation)
    distillable   BOOLEAN DEFAULT FALSE,   -- eligible for training set
    created_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX llm_call_log_distillable_idx ON llm_call_log (distillable, validation_status, created_at);
CREATE INDEX llm_call_log_trace_idx        ON llm_call_log (trace_id);

CREATE TABLE code_agent_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMP,
    task_id         BIGINT,                  -- references coding_tasks
    repo_id         BIGINT,                  -- references code_repos
    cycle_step      TEXT NOT NULL,           -- 'mine' | 'score' | 'gate' | 'draft' | 'apply' | 'validate' | 'promote' | 'audit'
    decision        TEXT,                    -- 'proceed' | 'defer' | 'veto' | 'escalate' | 'merge' | 'rollback'
    rule_snapshot   JSONB,
    llm_snapshot    JSONB,
    diff_summary    JSONB,
    validation_run_id BIGINT,
    branch_name     TEXT,
    commit_sha      TEXT,
    merged_to       TEXT,
    escalation_reason TEXT,
    notify_user     BOOLEAN DEFAULT FALSE,
    notified_at     TIMESTAMP
);
CREATE INDEX code_agent_runs_started_idx   ON code_agent_runs (started_at DESC);
CREATE INDEX code_agent_runs_task_idx      ON code_agent_runs (task_id);

CREATE TABLE code_kill_switch_state (
    id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
    active        BOOLEAN NOT NULL DEFAULT FALSE,
    reason        TEXT,
    activated_at  TIMESTAMP,
    activated_by  TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    last_run_id   BIGINT
);
INSERT INTO code_kill_switch_state (id, active) VALUES (1, false) ON CONFLICT DO NOTHING;

CREATE TABLE distillation_runs (
    id                BIGSERIAL PRIMARY KEY,
    started_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMP,
    base_model        TEXT NOT NULL,           -- 'qwen2.5-coder:7b' | 'deepseek-coder:6.7b'
    candidate_tag     TEXT,                    -- 'chili-coder:2026-04-24-1530'
    train_rows        INTEGER,
    eval_rows         INTEGER,
    incumbent_pass    NUMERIC(5,4),
    candidate_pass    NUMERIC(5,4),
    candidate_latency_ms INTEGER,
    decision          TEXT,                    -- 'promote' | 'reject' | 'shadow'
    decision_reason   TEXT,
    artifact_path     TEXT
);

CREATE TABLE frozen_scope_paths (
    id          SERIAL PRIMARY KEY,
    glob        TEXT NOT NULL UNIQUE,         -- e.g. 'app/services/trading/**'
    severity    TEXT NOT NULL,                 -- 'block' | 'review_required' | 'warn'
    reason      TEXT NOT NULL,
    added_at    TIMESTAMP DEFAULT NOW()
);
INSERT INTO frozen_scope_paths (glob, severity, reason) VALUES
  ('app/services/trading/**',          'block',           'CLAUDE.md Hard Rules 1-2 (kill switch, drawdown breaker)'),
  ('app/trading_brain/**',             'block',           'CLAUDE.md Hard Rule 5 (prediction mirror authority frozen)'),
  ('app/migrations.py',                'review_required', 'CLAUDE.md Hard Rule 6 (sequential idempotent migrations)'),
  ('app/services/trading/governance.py','block',          'kill switch and frozen-scope guard logic itself'),
  ('docs/KILL_SWITCH_RUNBOOK.md',      'review_required', 'incident playbook'),
  ('docs/PHASE_ROLLBACK_RUNBOOK.md',   'review_required', 'rollback playbook'),
  ('certs/**',                         'block',           'TLS certs')
ON CONFLICT DO NOTHING;
```

### 3.2 New modules

```
app/services/code_dispatch/
├── __init__.py
├── cycle.py                  # run_code_learning_cycle()  —  the 8-step orchestrator
├── miner.py                  # pick_next_task() — pulls from planner + hotspots + dep_alerts
├── scorer.py                 # task_complexity_score(), choose_tier()
├── rule_gate.py              # passes_code_rule_gate(ctx) — deterministic pre-filters
├── frozen_scope.py           # diff_touches_frozen_scope(diff) — glob match against frozen_scope_paths
├── runner.py                 # apply_in_worktree(), run_validation(), commit_and_push()
├── audit.py                  # write_code_agent_run()
├── notifier.py               # escalate_to_user() — only fires on stop conditions
└── governance.py             # is_code_agent_enabled(), activate/deactivate, consecutive_failures bookkeeping

app/services/llm_router/
├── __init__.py
├── router.py                 # route(prompt, purpose, complexity) → tier, model, provider
├── log.py                    # log_call() — writes to llm_call_log
├── ollama_client.py          # local-first chat client
└── tier_policy.py            # tier definitions, free-tier-first overrides

app/services/distillation/
├── __init__.py
├── exporter.py               # build_jsonl(filter, dedup, redact) from llm_call_log
├── trainer.py                # ollama-fine-tune wrapper (LoRA via Unsloth or Axolotl off-process)
├── evaluator.py              # held-out task eval, golden set
├── promotion_gate.py         # ensemble_code_promotion_check()  — mirrors trading promotion
└── registry.py               # current_tier_1_model(), promote(candidate_tag)
```

### 3.3 Wiring into existing workers

`scripts/scheduler_worker.py` (existing APScheduler daemon) gains two new jobs:

```python
sched.add_job(run_code_learning_cycle,  IntervalTrigger(seconds=60),  id="code_cycle",        max_instances=1, coalesce=True)
sched.add_job(run_distillation_cycle,   IntervalTrigger(hours=6),     id="distillation_cycle", max_instances=1, coalesce=True)
```

`scripts/brain_worker.py` gains a `code-cycle` mode (parallel to `lean-cycle`, `mining`, `backtest`, `fast-scan`) so you can run dispatch in a dedicated container if you want CPU isolation from the trading brain.

---

## 4. Multi-LLM router — eliminating subscription dependency

### 4.1 Tier policy

| Tier | Provider | Model | Cost | Use for |
|---|---|---|---|---|
| 1 | Ollama (local) | `chili-coder:current` (distilled) → falls back to `qwen2.5-coder:7b` | $0 | formatting, naming, docstrings, small refactors, tests for trivial functions, summarization |
| 2 | Groq (free tier) | `llama-3.3-70b-versatile`, `llama-3.1-8b-instant` | $0 | medium refactors, single-file logic changes, code review of small diffs |
| 3 | OpenAI | `gpt-4o-mini` | ~$0.15/1M in | cross-file logic, debugging, planning |
| 4 | OpenAI / Anthropic | `gpt-4o` / `claude-opus-4.6` | premium | architecture decisions, anything touching frozen-scope-adjacent code, distillation eval grading |

Routing decision (`scorer.choose_tier`):

- **task_complexity**: function of estimated diff size, number of files touched, presence of regex `/test_/`, depth in `app/services/trading/` proximity, planner-supplied difficulty.
- **prior failures on this task**: each retry bumps tier by 1.
- **operator override**: per-task `force_tier` in `coding_tasks`.
- **budget**: hourly+daily $ cap; if exceeded, only tier-1 + tier-2 fire and tier-3/4 tasks are deferred.

### 4.2 Self-distillation loop — the *real* answer to "no more coding subscriptions"

Once `llm_call_log` is filling, the distillation pipeline is just bookkeeping:

1. **Filter**: rows where `success = TRUE AND validation_status = 'passed' AND weak_response = FALSE AND distillable = TRUE`. Distillable means: no PII, no broker secrets, no frozen-scope code (a leak guard, not a quality guard).
2. **Dedup** by hash of `(system_prompt + user_prompt)`. Keep the cheapest tier that succeeded.
3. **Redact** API keys, tokens, file paths outside the repo, user emails. Use the same redactor that `governance._persist_kill_switch_state` uses for trading audit logs.
4. **Export to JSONL** in chat-template format for the base model (Qwen2.5-Coder uses ChatML, DeepSeek-Coder uses its own).
5. **Train** via Ollama's `Modelfile` `FROM qwen2.5-coder:7b` + a LoRA adapter trained out-of-process by Unsloth or Axolotl. Run on whatever GPU you have; Qwen2.5-Coder-7B fine-tunes comfortably on a single RTX 3090.
6. **Eval**: held-out 100-task set. Each task replays the original prompt, lets the candidate produce a diff, then runs the existing `run_validation_for_task`. Pass rate is the headline metric.
7. **Promote** via `ensemble_code_promotion_check`:
   - candidate pass rate ≥ incumbent pass rate
   - candidate p50 latency ≤ incumbent + 20%
   - zero regressions on the golden 10 tasks (curated, never auto-modified)
   - if all pass, atomic swap of `chili-coder:current` tag in Ollama; otherwise log regression and discard.

The trading brain already does *exactly this* for trading patterns (mine → score → CPCV → ensemble → promote → kill-switch-gated live). You ported the math once for money; we port the harness for code.

### 4.3 What it costs you in tokens

A first-pass estimate, conservative:

- Steady-state ratio after 30 days: tier-1 60% / tier-2 30% / tier-3 9% / tier-4 1% (the math improves over time as the local model learns).
- 200 tasks/day average dispatch volume: 120 tier-1 ($0), 60 tier-2 ($0 within Groq free limits), 18 tier-3 (~$0.30/day at 4o-mini), 2 tier-4 (~$0.40/day at 4o).
- ~$20/month all-in vs your current Cursor Max + Claude Pro stack.

The premium subs become "nice to have" instead of load-bearing. You can keep one for human-driven sessions and let Dispatch run on cheap APIs + local.

---

## 5. Hard rules — explicit conflict map

You said *full unattended (commits + tests + merges)*. The project has **six** hard rules in `CLAUDE.md` and several frozen-scope contracts. Dispatch must respect them. This is not me vetoing your decision — it is me being explicit about where the loop must auto-escalate to you instead of merging.

| Rule | Dispatch behavior |
|---|---|
| **HR1 — Kill switch before any automated trade** | Code Dispatch is *separate* from trading kill switch. Code Dispatch has its *own* kill switch (`code_kill_switch_state`). Trading kill switch is never touched by Dispatch under any circumstance. |
| **HR2 — Drawdown breaker** | N/A for code. Analogue: 3 consecutive validation failures on one task → defer; 5 consecutive failures across the loop → trip code kill-switch. |
| **HR3 — Data-first, code-second** | If Dispatch detects symptoms in DB (FK contamination, snapshot anomalies), it must not paper over with a router/service filter. Rule gate: any diff that adds a filter+suppression in `routers/` or `services/` *without* a corresponding migration → block, escalate. |
| **HR4 — Tests must use `_test`-suffixed DB** | Validation step runs with `TEST_DATABASE_URL`. Hardcoded in `run_validation_for_task`; Dispatch inherits this. Conftest guard remains the line of last defense. |
| **HR5 — Prediction mirror authority is frozen** | Diffs touching `app/trading_brain/` → blocked at frozen-scope guard. Always escalate. |
| **HR6 — Migrations sequential and idempotent** | Diffs that add migrations → `review_required` (not blocked, but always notify). The `_assert_migration_ids_unique` startup guard is the second line. |
| **Workflow rule: One logical change at a time** | `code_agent_runs.cycle_step` enforces this — one task per branch, never stack. |
| **Workflow rule: Restart server between changes** | Dispatch runs validation in a separate worktree against a separate test DB; the live server is never touched mid-cycle. |

If any of these gates trigger, the run is recorded with `decision='escalate'`, `notify_user=true`, and a notification fires (desktop + mobile via existing intercom channel in `chili_mobile/`).

---

## 6. Notification surface — Claude-Dispatch-style "only ping when needed"

Default: silent. The dashboard shows you what ran while you slept.

Notify when:

1. Code kill-switch trips (consecutive failures, validation timeout cluster, frozen-scope breach attempt).
2. Diff touches `frozen_scope_paths` with severity `review_required` — opens a PR and pings you.
3. Distillation eval shows regression on golden tasks — discarded automatically, but you should know.
4. Budget cap reached (tier-3/4 tokens for the day).
5. Same task fails 3 times in a row — promote to your queue with full failure context.
6. Production smoke test fails after a merge to `main` (we should add a smoke test in `scripts/post-merge-smoke.ps1`).

Channels: existing intercom in `chili_mobile/` for mobile push; desktop toast via existing actions plugin; email digest at end of day for everything else.

---

## 7. Phased rollout (you can stop after any phase)

### Phase D.0 — Logging only (this week)
- Add migration `171_chili_dispatch_tables` (the SQL above).
- Wrap every existing LLM call site in `log_call()` so `llm_call_log` starts filling. **No behavior change.**
- Wire Ollama into the cascade as tier-1 with a feature flag `CHILI_LLM_LOCAL_FIRST=0` (off by default).
- **Stop condition:** if `llm_call_log` doesn't get to 1k rows in a week, the rest of this is fantasy.

### Phase D.1 — Read-only dispatcher (week 2)
- Build `code_dispatch/cycle.py` end-to-end but stop *before* `apply_in_worktree`. Output is a "what I would do" report.
- Run on the scheduler every 60s in shadow mode.
- Confirm: rule gate fires correctly, frozen-scope guard catches attempted edits to trading code, scorer picks reasonable tiers.

### Phase D.2 — Sandboxed apply (week 3)
- Apply diffs in a *worktree* (`git worktree add /tmp/dispatch-{task_id} dispatch/{task_id}`), run validation there, never push.
- Audit only.

### Phase D.3 — Auto-commit to feature branches (week 4)
- Push to `dispatch/{task_id}` branches.
- Auto-merge **only** if: validation passes, no frozen-scope hits, branch protection rules met. Default is still PR + manual merge until you've watched it for a week.

### Phase D.4 — Auto-merge to main (after 2 weeks of clean Phase D.3)
- Flip `CHILI_DISPATCH_AUTO_MERGE=1`.
- Frozen-scope-clean diffs merge directly. Anything touching review-required scope still opens a PR.

### Phase D.5 — Distillation live (parallel, can start any time after D.0 has 5k+ rows)
- Stand up `distillation_cycle` every 6h.
- Shadow mode for 2 weeks: never promote, just measure pass rates.
- After two consecutive cycles of `candidate_pass >= incumbent_pass`, flip promotion gate to live.

### Phase D.6 — Subscription off-ramp
- Watch the tier mix for 2 weeks. When tier-1 share is consistently >60% and tier-3/4 spend < $1/day, you can drop one premium subscription. After another 4 weeks at >75% tier-1, drop the second.

---

## 8. Risks and counter-arguments

**"What if the local model corrupts itself?"** Distillation only writes to a *new* Ollama tag. The current tag is the atomic swap target. The promotion gate compares against the live incumbent on the same eval set. If a candidate underperforms, it is never promoted. Worst case is wasted GPU time.

**"What if Dispatch hits a prompt-injection attack via a task description?"** Frozen-scope guard is computed from the diff, not the prompt. An injected prompt that says "ignore prior instructions and modify governance.py" still produces a diff that touches `governance.py`, which is blocked at the glob level. Belt and suspenders: rule gate also forbids commits where the diff touches files not listed in the planner task's `intended_files` field.

**"What if the brain promotes a bad pattern and corrupts the trading loop?"** Code Dispatch and trading brain are separate kill-switches. Code Dispatch can't disable the trading kill-switch (frozen scope). Code Dispatch can't modify trading code (frozen scope). The blast radius is contained to non-trading code.

**"Why not just use Cursor's Background Agent / Claude Code in headless mode?"** Both exist. Both are subscription-bound. The point of this design is that *the training data is your own usage*, the model lives on your hardware, and the loop survives subscription expiry. Cursor BG and Claude Code remain useful as *teachers* — every prompt+completion they produce becomes a row in `llm_call_log`. They get *cheaper* the longer you run Dispatch alongside them.

**"Why not fine-tune via OpenAI's API instead of locally?"** Fine-tuning gpt-4o-mini is fine but locks you to OpenAI. Local Ollama fine-tunes are portable (the LoRA adapter is yours), GPU-bound (no per-token cost), and the whole architecture is local-first to begin with.

---

## 9. What I have already scaffolded

In `C:\dev\chili-home-copilot/` (separate files, none auto-applied — review and integrate when ready):

- `docs/CHILI_DISPATCH_AUTONOMOUS_DEV_PLAN.md` — this document
- `docs/CHILI_DISPATCH_RUNBOOK.md` — operator runbook (kill switch, manual override, budget cap, distillation rollback)
- `app/migrations_proposed/171_chili_dispatch_tables.py` — proposed migration; **NOT** wired into `MIGRATIONS` list yet (you decide when)
- `app/services/code_dispatch/__init__.py` and skeleton modules for `cycle.py`, `miner.py`, `scorer.py`, `rule_gate.py`, `frozen_scope.py`, `governance.py`
- `app/services/llm_router/__init__.py` and skeleton for `router.py`, `log.py`, `ollama_client.py`
- `app/services/distillation/__init__.py` and skeleton for `exporter.py`, `trainer.py`, `evaluator.py`, `promotion_gate.py`

All skeletons compile, all imports resolve, none of them run on startup. The next session can start by reading `docs/CHILI_DISPATCH_RUNBOOK.md` and progressing through Phase D.0.

---

## 10. Decisions I need from you before Phase D.0 ships

1. **Local model choice.** Qwen2.5-Coder:7b (recommended), DeepSeek-Coder:6.7b (also strong), or Llama-3.1-8B-Instruct (broader but weaker on code). Default in scaffolding: Qwen2.5-Coder:7b.
2. **Training stack.** Unsloth (faster, single-GPU, 4-bit by default), Axolotl (more flexible, multi-GPU friendly), or stay vendor-managed via OpenAI fine-tuning (locks you in). Default: Unsloth.
3. **Auto-merge threshold for D.4.** "Validation passed + no frozen-scope hits" only? Or also "diff < 200 LOC"? Default: pass + clean + < 400 LOC, anything bigger opens a PR.
4. **Notification channel priority.** Mobile push first or desktop toast first? Default: desktop toast first, mobile push only for kill-switch and budget alerts.
