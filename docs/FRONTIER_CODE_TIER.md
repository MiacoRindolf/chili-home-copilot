# Frontier code-generation tier

**Status:** opt-in, inert by default. Shipped on branch `frontier-code-routing`.

## Why this exists

CHILI's autonomous coding harness is strong — worktree isolation,
anti-hallucination diff validation, a 5-iteration test-repair loop, a self-review
gate, and call-graph/multi-hop retrieval. But the *brain* inside that harness has
been a non-frontier model: the default cascade generates code with
**Llama-3.3-70B** (Groq) → **gpt-4o-mini** (OpenAI) → **gemini-2.0-flash**. No
self-graded benchmark can make that exceed Claude Opus 4.8 / Codex 5.5 on raw
coding capability, because raw capability is bounded by the base model.

This tier closes that gap directly: it lets CHILI run its **full coding harness on
a frontier brain**. Frontier generation + CHILI's verification/domain advantages is
the one configuration that can genuinely beat a frontier model *used bare* on this
repo.

## What it does

When enabled, the gateway routes CHILI's **code-generation purposes** to a frontier
model instead of the local cascade:

| Purpose | Routed when frontier on? |
| --- | --- |
| `code_dispatch_plan` | yes |
| `code_dispatch_edit` | yes |
| `code_dispatch_create` | yes |
| `code_dispatch_diagnose` / `code_dispatch_pr_repair` | yes (prefix `code_dispatch`) |
| `code_review` | yes |
| `code_search` | **no** (retrieval-only, high volume, little gain) |
| everything else (chat, trading, planner, …) | **no** |

The frontier tier is a **top-of-cascade** step in `openai_client.chat()`. It is only
tried when the gateway explicitly requests the frontier model via `model_override`.
**Any frontier failure — auth, rate-limit, timeout, error — transparently falls back
to the existing local cascade.** Enabling it can never make the code path worse than
it is today.

It reuses the generic OpenAI-compatible `_call_provider`. Anthropic exposes an
OpenAI-compatible endpoint, so Claude Opus 4.8 is reachable with **no new SDK**. Any
OpenAI-compatible frontier (e.g. OpenAI's strongest coding model) works by pointing
`frontier_base_url` / `frontier_model` at it.

## How to enable

### Option A — Claude Opus 4.8 (Anthropic)

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...        # your Anthropic key
CHILI_CODE_FRONTIER_ENABLED=1
# defaults already point at Claude Opus 4.8 via Anthropic's OpenAI-compat endpoint:
#   FRONTIER_BASE_URL=https://api.anthropic.com/v1
#   FRONTIER_MODEL=claude-opus-4-8
```

### Option B — a frontier OpenAI coding model (uses the key you already have)

`.env` already has `PAID_OPENAI_API_KEY`, so this works immediately:

```bash
# .env
CHILI_CODE_FRONTIER_ENABLED=1
FRONTIER_API_KEY=${PAID_OPENAI_API_KEY}          # or paste the key
FRONTIER_BASE_URL=https://api.openai.com/v1
FRONTIER_MODEL=<openai-frontier-coding-model>    # e.g. the strongest available
```

Then restart the workers that run code generation:

```bash
docker compose up -d --force-recreate chili
```

### Per-purpose override (advanced)

The pre-existing `chili_llm_purpose_model_overrides_json` still wins over this
auto-route. To pin only edits to the frontier model and leave planning local:

```bash
CHILI_LLM_PURPOSE_MODEL_OVERRIDES_JSON={"code_dispatch_edit":"claude-opus-4-8"}
```

## Safety properties

- **Inert by default.** With the flag off or no key, `chat()` is byte-identical to
  today's cascade. Verified by `tests/test_frontier_code_routing.py`.
- **Graceful fallback.** Frontier failure → local cascade. The code path never
  hard-fails because a frontier key is missing/exhausted.
- **High-stakes purposes are never auto-routed** (the gateway's existing
  high-stakes guard is respected).
- **Repeated auth failures short-circuit** via the existing `_mark_auth_failed`
  cache, so a bad key doesn't add latency to every call.

## Cost

Frontier models cost materially more per token than the local cascade. This tier is
deliberately scoped to *generation* purposes (plan/edit/create/diagnose/repair) plus
review, and excludes high-volume retrieval (`code_search`). The material/replay cache
in `code_brain/agent.py` and the gateway cache still apply, so repeated identical
plans/edits don't re-bill. Monitor spend via the existing `llm_call_log` /
`estimated_cost_usd` columns.

## How to verify it's live

After enabling, run a coding task and check the gateway/cascade log for the frontier
tier:

```
trying frontier provider=api.anthropic.com model=claude-opus-4-8
llm_reply model=claude-opus-4-8 tokens=...
```

and `llm_call_log.provider='anthropic'` rows for `purpose IN ('code_dispatch_edit', …)`.
