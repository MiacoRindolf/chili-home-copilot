---
status: completed
title: LLM cost reduction — phases A (guardrails), B (cheapest-first + cache), C (polish)
parent_plan: none
phase_id: llm_cost_reduction_abc
created: 2026-04-17
frozen_at: 2026-04-17
completed_at: 2026-04-17
rollout_mode_default: off
phase_a_status: completed
phase_b_status: completed
phase_c_status: completed
---

# LLM cost reduction — phases A, B, C

## Context

Today's 429 storm (OpenAI `insufficient_quota`) exposed three structural
wastes in `app/openai_client.py` + `app/services/llm_caller.py` + all
downstream brain-agent callers:

1. Transient-retry logic is applied to **non-transient** errors
   (`insufficient_quota`, `invalid_api_key`, `model_not_found`).
2. The **streaming → non-streaming double cascade** reruns all 4 tiers
   on silent empty streams **and** on permanent errors.
3. The **paid tier (OpenAI) is tried before the free tier (Groq)** when
   both are configured.
4. No **response cache** exists for idempotent brain-agent /
   analyze-context calls, so the same prompt → same output path fires
   every cycle / page load.
5. `_is_weak_response` triggers **escalation on valid short JSON
   replies** from services that legitimately return < 60 chars.
6. Per-provider daily-token accounting exists **only for Groq**.
7. `pattern_adjustment_advisor.get_adjustment()` is LLM-called per
   monitor tick per open position with **no input-change gate**.
8. `project_brain/agents/*.py` share large repeated prompt boilerplate,
   inflating **input token** cost (not call count, but per-call $).

No item below changes **user-visible behaviour** on the happy path. All
savings come from eliminating pointless / duplicate / escalation-happy
calls.

## Objective

Ship three phases, in order, each independently verifiable and
reversible. Success = measurable reduction in OpenAI + Groq daily call
volume (and 429 events) with **identical** or better reply quality on:

- `/api/trading/analyze/stream`
- `/api/trading/smart-pick/stream`
- Chat (`/api/chat/stream` family)
- Project-brain scheduled agent cycles
- Pattern-position monitor ticks
- Learning-cycle report (when enabled)

## Non-negotiables

1. **No prompt rewrites that change semantics** — only deduplication
   of identical shared snippets into a single loaded prompt file.
2. **Caching is opt-in per call site.** User-chat, personality, and
   wellness paths **must not** cache LLM replies.
3. **Retry policy changes never silently swallow** a new exception
   type. Every decision to bail early must log a structured line so
   the operator can see *why* a retry was skipped.
4. **Tier ordering changes stay behind a settings flag** so the user
   can flip back without a code change.
5. **No change to `/api/trading/scan/status` frozen contract.**
6. **No schema changes.** All caches are process-local
   (LRU + TTL), not persisted.

## Non-goals (this plan)

- Switching providers, adopting LiteLLM, or introducing a new LLM
  router abstraction.
- Moving any brain-agent work to local Ollama.
- Any change to the planner / Ollama stack.
- Rewriting `chat_stream` into async.

## Todos (YAML source of truth)

```yaml
todos:
  # ── Phase A: guardrails, zero-risk ──
  - id: a1_classify_permanent_errors
    phase: A
    status: completed
    content: >
      In openai_client: introduce _is_permanent_openai_error(exc) that
      detects insufficient_quota / invalid_api_key / model_not_found /
      account_disabled from APIStatusError body. _call_provider and
      _stream_provider must NOT retry when permanent. chat() and
      chat_stream() must NOT cascade to next tier on permanent errors
      that are clearly account-wide (insufficient_quota / account_disabled).
      Keep cascading for model-specific errors (model_not_found only
      skips that tier).
  - id: a2_disable_sdk_internal_retry
    phase: A
    status: completed
    content: >
      Pass max_retries=0 to OpenAI(...) in _call_provider and
      _stream_provider so the SDK does not amplify our own retry loop.
      Our app-level retry remains the single source of truth.
  - id: a3_gate_nonstream_fallback
    phase: A
    status: completed
    content: >
      In chat_stream, only call the non-streaming chat() fallback when
      ALL tier stream attempts produced zero tokens AND zero raised
      exceptions (silent empty). If any tier raised a permanent error
      OR a RateLimitError already surfaced, skip the non-streaming
      fallback. Emit one log line explaining why.
  - id: a4_tighten_weak_response
    phase: A
    status: completed
    content: >
      Add strict_escalation: bool = True kwarg to chat() and
      chat_stream(). Default True (current behavior for chat UI).
      llm_caller.call_llm sets strict_escalation=False so short JSON
      structured replies (e.g. {"action":"hold"}) from
      pattern_adjustment_advisor / project_brain/agents / trade_plan_extractor
      no longer trigger paid-tier escalation.
      _is_weak_response gains a matching flag; when False, only
      refusal/empty count as weak.
  - id: a5_tests_phase_a
    phase: A
    status: completed
    content: >
      Unit tests for _is_permanent_openai_error, non-stream fallback
      gate, and strict_escalation=False. Preserve existing
      test_chat_stream_tries_secondary_when_primary_yields_no_tokens
      and test_chat_stream_non_streaming_fallback_when_all_streams_empty.
  - id: a6_docker_verify_phase_a
    phase: A
    status: completed
    content: >
      docker compose build + up -d --force-recreate chili brain-worker
      scheduler-worker. Open AI analyze on AAPL 1d three times;
      confirm single Groq stream per click, zero OpenAI calls unless
      Groq escalates. Check /api/trading/scan/status frozen contract
      byte-equal.

  # ── Phase B: cheapest-tier-first + content cache ──
  - id: b1_flag_llm_free_tier_first
    phase: B
    status: completed
    content: >
      Add settings.llm_free_tier_first: bool = True (env
      LLM_FREE_TIER_FIRST). When True AND both OPENAI_API_KEY and
      LLM_API_KEY are set, chat() / chat_stream() reorder to
      Groq primary → Groq secondary → OpenAI official → Gemini premium.
      When False, keep today's order. Log the chosen order once at
      import.
  - id: b2_escalation_preserves_quality
    phase: B
    status: completed
    content: >
      With free-tier-first on, the weak-response escalation path still
      fires. Verify via existing _is_weak_response: if Groq reply is
      weak AND OPENAI_API_KEY is configured, escalate to OpenAI
      official (not Gemini). Add a test that covers this escalation.
  - id: b3_call_llm_content_hash_cache
    phase: B
    status: completed
    content: >
      In app/services/llm_caller.py: add an in-process LRU+TTL cache
      keyed by sha256("{model}|{max_tokens}|{system}|{user_json}").
      maxsize=256, default_ttl=600s. Add cacheable: bool = False
      kwarg (opt-in). Provide get/put with jitter so parallel
      workers do not all miss at the same moment.
  - id: b4_opt_in_cacheable_call_sites
    phase: B
    status: completed
    content: >
      Flip cacheable=True ONLY on deterministic prompts:
        - app/services/trading/trade_plan_extractor.py
        - app/services/trading/pattern_adjustment_advisor.py
        - app/services/trading/web_pattern_researcher.py
        - app/services/trading/learning_cycle_report.py
        - app/services/project_brain/web_research.py
        - app/services/project_brain/agents/*.py (all 10)
        - app/services/code_brain/reviewer.py
      Do NOT flip on: chat routers, personality, memory, wellness,
      brain_assistant (user-facing conversational).
  - id: b5_cache_metrics
    phase: B
    status: completed
    content: >
      Expose /api/trading/brain/llm-cache-stats returning
      {hits, misses, size, evictions, hit_rate}. Reset counters on
      process start. Log a daily summary line via brain_worker
      heartbeat cycle.
  - id: b6_tests_phase_b
    phase: B
    status: completed
    content: >
      Unit tests:
        - cache hit returns same reply, miss increments counter
        - different max_tokens → different cache key
        - ttl expiry produces miss
        - cacheable=False (default) never caches
        - free-tier-first reorder path; escalation on weak still
          hits OpenAI, not Gemini
  - id: b7_docker_verify_phase_b
    phase: B
    status: completed
    content: >
      Recreate containers. Run a full learning cycle (trigger via
      /api/trading/brain/cycle/run). Confirm:
        - llm-cache-stats hit_rate > 0 on 2nd cycle within TTL
        - OpenAI call count drops vs baseline (compare docker logs
          chili --since 30m grep api.openai.com count before/after)
        - Groq call count roughly unchanged or lower (cache hits)
        - No regressions in scan_status contract test.

  # ── Phase C: polish + per-provider budget + prompt dedupe ──
  - id: c1_analyze_stream_sse_cache
    phase: C
    status: completed
    content: >
      In app/routers/trading.py api_analyze_stream: add a short TTL
      cache keyed by (user_id, ticker, interval, sha256(ai_context)).
      TTL: 90s for 1d, 30s for 15m, 10s for 1m/5m. On hit, replay the
      cached full text as SSE token-by-token (chunked 20-40 chars)
      so the UX stays identical. Annotations and trade_plan_levels
      re-extracted from cached text, not re-called.
  - id: c2_per_provider_daily_budget
    phase: C
    status: completed
    content: >
      Extend _track_tokens / _near_daily_limit in openai_client to
      per-provider buckets keyed by base_url host. Skip the tier when
      its bucket is within N% of its daily cap. Configurable via
      settings.openai_daily_token_limit (default 0 = unlimited) and
      settings.premium_daily_token_limit. Groq bucket keeps existing
      behaviour.
  - id: c3_pattern_adjustment_input_gate
    phase: C
    status: completed
    content: >
      In app/services/trading/pattern_adjustment_advisor.get_adjustment:
      cache the last AdjustmentRecommendation by
      (trade_id, hash(rounded inputs)). Inputs rounded to coarse
      buckets: current_price ±0.25%, health_score ±0.05,
      pnl_pct ±0.5pp. TTL 5 min. Cache hit returns previous rec
      without calling the LLM. Safety rails still apply on return.
  - id: c4_shared_agent_prompt_snippets
    phase: C
    status: completed
    content: >
      Extract the repeated severity legend + JSON schema scaffolding
      used across app/services/project_brain/agents/*.py into
      app/prompts/agent_shared.txt. Load once at import. Each agent
      appends its own task-specific block. Verify no agent's output
      parsing breaks (all 10 agent JSON schemas unchanged).
  - id: c5_tests_phase_c
    phase: C
    status: completed
    content: >
      Tests:
        - analyze_stream cache hit replays as SSE
        - per-provider budget blocks tier at threshold
        - pattern_adjustment input gate reuses rec when inputs
          round to same bucket
        - agent_shared.txt loaded; one agent end-to-end roundtrip
          still returns valid JSON
  - id: c6_docker_verify_phase_c
    phase: C
    status: completed
    content: >
      Recreate containers. Open Analyze on AAPL 1d twice in 90s →
      second call must be instant (cache hit). Inspect a
      pattern-position monitor tick log — LLM call count should drop
      on steady-state positions. Confirm scan_status contract clean.
  - id: c7_docs_and_closeout
    phase: C
    status: completed
    content: >
      docs/LLM_COST_REDUCTION_ROLLOUT.md summarizing:
        - the four retry/escalation rules
        - cacheable call-site list
        - per-provider token budgets
        - monitoring: /api/trading/brain/llm-cache-stats
        - rollback commands per phase
      Closeout section on this plan with measured before/after
      OpenAI call counts.
```

## Execution order (strict)

Per `chili-workflow-phases.mdc`: one phase, freeze, execute, verify,
closeout, next phase. **Do not** start Phase B before Phase A is
docker-soaked clean. **Do not** start Phase C before Phase B shows
measurable cache hit rate > 0 in staging.

Inside each phase, todos run in listed order. Tests before docker
verify; docker verify before marking phase complete.

## Verification gates (per phase)

### Phase A
- `pytest tests/test_openai_client_stream.py -v` green.
- New unit tests for permanent-error detection + non-stream gate
  + strict_escalation green.
- `docker compose logs chili --since 15m | rg 'api\.openai\.com'`
  count during one AI analyze call ≤ 3 (was up to ~20 on quota days).
- `/api/trading/scan/status` frozen-contract test unchanged.
- Manual: `/api/trading/analyze/stream?ticker=AAPL&interval=1d` streams
  a full reply on the happy path.

### Phase B
- All Phase A gates still green.
- New cache unit tests green.
- `/api/trading/brain/llm-cache-stats` exposes the 5 fields.
- After 2 consecutive learning cycles within TTL, `hit_rate >= 0.3`
  (conservative lower bound — real number likely higher).
- OpenAI call count on a full cycle ≤ 50% of pre-Phase-B baseline
  when Groq is healthy.

### Phase C
- All Phase A + B gates still green.
- Analyze stream second-click-within-TTL returns in < 200ms end-to-end
  (cache hit, SSE replay).
- Pattern-position monitor log shows `llm_skip=input_unchanged` on
  steady positions.
- Agents' JSON outputs validate against existing schemas (no parse
  errors regression).

## Release-blocker scan (per phase)

Phase A:

```powershell
docker compose logs chili --since 30m 2>&1 |
  Select-String "insufficient_quota" |
  Where-Object { $_.Line -match "attempt=[23]" }
```

Non-empty output = Phase A regressed (we retried a permanent error).

Phase B:

```powershell
docker compose logs chili --since 30m 2>&1 |
  Select-String "llm_cache_stats.*hit_rate=0\.0" |
  Select-Object -Last 5
```

Persistent 0.0 hit_rate on hot paths = cache not wired into the flipped
call sites.

Phase C:

```powershell
docker compose logs chili --since 30m 2>&1 |
  Select-String "analyze_stream.*cache=replay"
```

Should appear at least once after two same-ticker analyze clicks in
90s.

## Rollback (per phase)

- **Phase A.** Revert commits; no schema / config changes to undo.
- **Phase B.** Set `LLM_FREE_TIER_FIRST=false` (env) and recreate
  services. The cache is process-local — container restart clears it.
  Revert code for deeper rollback.
- **Phase C.** Set `settings.openai_daily_token_limit=0`,
  `premium_daily_token_limit=0`; remove the per-route analyze cache
  by setting a feature flag (add `ANALYZE_STREAM_CACHE_ENABLED` in
  this phase) to false.

## Config surface introduced

| Env var | Default | Phase |
|---|---|---|
| `LLM_FREE_TIER_FIRST` | `true` | B |
| `OPENAI_DAILY_TOKEN_LIMIT` | `0` (unlimited) | C |
| `PREMIUM_DAILY_TOKEN_LIMIT` | `0` (unlimited) | C |
| `ANALYZE_STREAM_CACHE_ENABLED` | `true` | C |

No new secrets. No migrations.

## Out-of-scope reminders

- Do not touch prediction-mirror / scan_status contract code.
- Do not mutate any `.plan.md` under `.cursor/plans/` besides this one.
- Do not batch all phases into one commit; one phase = one
  reviewable change set.

## Closeout (filled on completion)

Phase A closeout: _pending_
Phase B closeout: _pending_
Phase C closeout: _pending_

Measured before/after:
- OpenAI POSTs per AI analyze click: _baseline_ → _after_
- Groq POSTs per learning cycle: _baseline_ → _after_
- 429 events per 24h: _baseline_ → _after_
- llm-cache-stats hit_rate (24h): _after_
