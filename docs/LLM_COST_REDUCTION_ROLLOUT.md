# LLM cost reduction rollout

Phases A (guardrails), B (cheapest-tier-first + cache), and C (polish) were
landed together on 2026-04-17 as a single, verifiable set of behaviour
changes for `app/openai_client.py`, `app/services/llm_caller.py`,
`app/routers/trading.py`, and the 10 project-brain agents.

This document is the operator reference — **why** the changes exist,
**how** to observe them, and **how** to roll back if something regresses.

---

## 1. Retry / escalation rules (Phase A)

| # | Rule | Where | Flag |
|---|------|-------|------|
| 1 | Permanent errors never retry and never cascade via the non-stream fallback. Detected codes: `insufficient_quota`, `invalid_api_key`, `model_not_found`, `account_disabled`, `account_deactivated`, `billing_hard_limit_reached`; also 401 / 404. | `_is_permanent_openai_error()` in `app/openai_client.py` | — |
| 2 | OpenAI SDK internal retry is disabled (`max_retries=0`). Our 3-attempt app-level loop is the single source of truth. | `_call_provider` / `_stream_provider` | — |
| 3 | `chat_stream` only falls back to non-streaming `chat()` when **every** tier yielded zero tokens and raised zero errors. Any permanent error or transient 429 short-circuits the fallback. | `chat_stream()` | — |
| 4 | `strict_escalation=False` disables the length-based weak-response heuristic. `llm_caller.call_llm` opts out so short deterministic JSON (e.g. `{"action":"hold"}`) no longer escalates to the paid tier. | `_is_weak_response` | — |

---

## 2. Cacheable call sites (Phase B)

`app/services/llm_caller.call_llm(..., cacheable=True)` opts into an
in-process LRU+TTL cache keyed by
`sha256(model | max_tokens | system_prompt | messages_json)`.

**Flipped on (deterministic prompts):**

| File | Trace id(s) |
|------|-------------|
| `app/services/trading/trade_plan_extractor.py` | `trade-plan-{ticker}` |
| `app/services/trading/pattern_adjustment_advisor.py` | `pattern-adjust-{ticker}` |
| `app/services/trading/web_pattern_researcher.py` | `web_pattern_extract` |
| `app/services/trading/learning_cycle_report.py` | `learning-cycle-report` |
| `app/services/project_brain/web_research.py` | per-caller |
| `app/services/project_brain/agents/product_owner.py` | `po-gaps`, `po-upgrade-options`, `po-questions`, `po-synthesize`, `po-recommendations` |
| `app/services/project_brain/agents/project_manager.py` | `pm-breakdown`, `pm-findings` |
| `app/services/project_brain/agents/architect.py` | `arch-findings` |
| `app/services/project_brain/agents/backend_engineer.py` | `be-patterns`, `be-findings` |
| `app/services/project_brain/agents/frontend_engineer.py` | `fe-patterns`, `fe-findings` |
| `app/services/project_brain/agents/ux_designer.py` | `ux-heuristics`, `ux-a11y` |
| `app/services/project_brain/agents/qa_engineer.py` | `qa-testgen`, `qa-codebugs` |
| `app/services/project_brain/agents/devops_engineer.py` | `devops-patterns`, `devops-findings` |
| `app/services/project_brain/agents/security_engineer.py` | `sec-deps`, `sec-code`, `sec-api` |
| `app/services/project_brain/agents/ai_engineer.py` | `ai-prompts`, `ai-bench`, `ai-findings` |
| `app/services/code_brain/reviewer.py` | `code-reviewer` |

**Left off (non-deterministic / user-facing):** user chat streams,
personality, memory, wellness, `brain_assistant`, `code_brain/search`,
`playwright_runner`, `position_plan_generator`, `patterns` router, and
`reasoning_brain/user_model` (which doesn't use `call_llm`).

---

## 3. Per-provider token budgets (Phase C, c2)

Daily token buckets are tracked per provider host in
`app/openai_client.py`. A tier is skipped for the rest of the day once
its bucket reaches its configured cap.

| Provider | Env var | Default |
|----------|---------|---------|
| Groq | (hard-coded) | `_DAILY_TOKEN_LIMIT_GROQ = 85000` (≈ 85% of free 100K/day) |
| OpenAI official | `OPENAI_DAILY_TOKEN_LIMIT` | `0` (unlimited) |
| Gemini premium | `PREMIUM_DAILY_TOKEN_LIMIT` | `0` (unlimited) |

Observe with:

```powershell
conda run -n chili-env python -c "from app.openai_client import get_daily_token_usage; import json; print(json.dumps(get_daily_token_usage(), indent=2))"
```

---

## 4. Analyze-stream SSE cache (Phase C, c1)

`GET /api/trading/analyze/stream` caches the full LLM body by
`sha256(user_id | ticker | interval | ai_context | user_msg)`.
TTL by interval: `1m/5m=10s`, `15m/30m=30s`, `1h=45s`, `1d=90s`,
default `45s`. On hit the cached text is replayed as 32-char SSE chunks
so the UX is identical. Annotations and trade-plan levels are
re-extracted from cached text, not re-called.

---

## 5. Monitoring endpoints

- `GET /api/trading/brain/llm-cache-stats` — shared LRU+TTL cache hits /
  misses / size / hit_rate / evictions.

Ad-hoc introspection (no dedicated endpoint — use the Python shell):

- `app.routers.trading._analyze_stream_cache_stats()` — SSE cache
- `app.openai_client.get_daily_token_usage()` — per-provider day buckets
- `app.services.trading.pattern_adjustment_advisor.get_input_gate_stats()` — monitor input gate

---

## 6. Rollback

Each phase is independently reversible.

### Rollback all

```powershell
git revert <phase-a-b-c-commit>
docker compose up -d --force-recreate chili brain-worker scheduler-worker
```

### Rollback just the cascade reorder (Phase B, b1)

```
LLM_FREE_TIER_FIRST=false
docker compose up -d --force-recreate chili brain-worker scheduler-worker
```

### Rollback just the content cache (Phase B, b3)

```
LLM_CACHE_MAX_ENTRIES=0
docker compose up -d --force-recreate chili brain-worker scheduler-worker
```

### Rollback just the analyze-stream SSE cache (Phase C, c1)

Set `_ANALYZE_STREAM_CACHE_MAX = 0` in `app/routers/trading.py` and
redeploy, or revert the `_analyze_stream_*` block.

---

## 7. Release-blocker grep (still applies)

The prediction-mirror blocker from
`docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md` is unaffected:

```powershell
.\scripts\check_chili_prediction_ops_release_blocker.ps1
```

Empty output = pass.

---

## 8. Measured baseline

Before Phase A+B+C, a single `AI analyze` click on AAPL with
`PREMIUM_API_KEY` mis-pointed at a quota-exhausted OpenAI account
generated **~72 POSTs** to `api.openai.com` (3 retries × 2 SDK retries ×
4 tiers × 2 stream-to-chat cascades + duplication across the analyze
streaming path). Post-landing, the same click with permanent errors
produces **exactly 1 request per tier per click** (app-level retries
suppressed by permanent-error classification) and **zero non-stream
fallbacks** when any tier already raised.

After the next full steady-state day, operators should record:

- `/api/trading/brain/llm-cache-stats` `hit_rate`
- 30-min `docker compose logs chili` count of `api.openai.com` POSTs
- 30-min `docker compose logs chili` count of `api.groq.com` POSTs

and update this doc with before/after numbers.
