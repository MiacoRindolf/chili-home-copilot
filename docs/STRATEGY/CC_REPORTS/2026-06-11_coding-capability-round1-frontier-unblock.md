# CC_REPORT: coding-capability-round1-frontier-unblock

Operator-directed session (not the queued phase-5i NEXT_TASK): "check CHILI's
coding capability + desktop autopilot thoroughly, then recursively enhance it
toward Fable 5-class so the $200/mo Claude subscription can be cut."

## Audit verdict (multi-agent, 6 deep-readers + synthesis, claims spot-verified)

CHILI today is a genuinely good safety envelope (worktrees, leases, approval
gates, audit rows) around an engine that does not exist:

- The autonomous dispatch loop is OFF in prod (`CHILI_DISPATCH_ENABLED=0`,
  zero code-purpose LLM calls since 2026-05-27).
- The frontier tier was quadruple-dead: no key in .env, flag off,
  `high_stakes` DB policies veto the override (all 3 code purposes seeded
  high_stakes=TRUE), and `code_dispatch_plan` routed via 'tree' which ignores
  model overrides and drops repo context.
- EVERY autopilot implementation run crashed at validate:
  `run_validation` called `run_ast_syntax(worktree, changed_files)` against a
  single-arg signature → TypeError, swallowed, run marked failed.
- Even without the crash, "validation passed" was vacuous:
  `subprocess_safe_env` stripped TEST_DATABASE_URL so pytest skip-passed at
  the conftest guard.
- The dispatch frozen-scope guard was dead: pre-check runs on
  `intended_files` which the miner can't populate → a diff touching
  `app/services/trading/*` would commit+push unchecked.
- De-facto code generator: free-tier Llama-3.3-70B → gpt-4o-mini, capped at
  1500–3000 max_tokens, ILIKE-substring retrieval, no tool loop.

## What shipped (all merged to main)

- #617 `71c0f1e` — frontier code tier: make it actually reachable.
  high_stakes carve-out (blocks downgrades, not upgrades), claude* added to
  the temperature-omit list, `_stream_tier_frontier` in chat_stream, mig 305
  (code_dispatch_plan tree→augmented, idempotent), new single knob
  `chili_code_gen_max_tokens` (default 16384) replacing the 1500/3000 caps.
  17/17 tests.
- #618 `8ac87f4` — autopilot validation: fixed the every-run TypeError;
  `run_ast_syntax` scoped to changed files (worktree-escape-safe);
  TEST_DATABASE_URL passthrough fail-closed to `_test`-suffixed postgres DBs
  only; honest `tests_executed`/`tests_selected` metadata. 9 new tests + 111
  adjacent green.
- #619 `01d15cd` — dispatch lane post-apply frozen-scope gate on git truth
  (`git status --porcelain -uall`, union with claimed files), refusal BEFORE
  commit/push, escalates as blocked_scope with notify. 6 new tests (two
  caught real bugs in my first implementation: NotADirectoryError on missing
  cwd; untracked-dir collapse hiding frozen paths).

## Activation done live (no-dark-flags)

- `.env`: `CHILI_CODE_FRONTIER_ENABLED=1`, `FRONTIER_API_KEY`(=paid OpenAI),
  `FRONTIER_BASE_URL=https://api.openai.com/v1`, `FRONTIER_MODEL=gpt-5.5`.
- Live DB: `code_dispatch_plan` routing_strategy tree→augmented (same change
  mig 305 makes; migration no-ops at next startup).
- **Live end-to-end proof from host:** `gateway_chat(purpose=
  "code_dispatch_edit")` → `trying frontier provider=openai model=gpt-5.5` →
  correct reply. The code-generation brain is now gpt-5.5 when the new code
  runs.

## Verification

- pytest: 17 + 9 + 6 new tests pass; 111 adjacent (coding validator safety,
  project autonomy service, validation audit, code dispatch sandboxed) pass.
- verify-migration-ids: PASS (295 migrations).
- Groq key is ALIVE again (HTTP 200; the June-4 dead-key issue is resolved).
- OpenAI key alive (HTTP 200).

## Deferred / next rounds (the "recursive" queue, ranked by the synthesis)

1. **Deploy**: rebuild image from main ≥`01d15cd`, recreate web +
   scheduler containers AFTER market close (running containers predate all
   three PRs; .env applies at recreate). Then flip `CHILI_DISPATCH_ENABLED=1`
   + set `CHILI_DISPATCH_TASK_STATUSES` to real readiness states (default
   'ready_for_dispatch' is a state no transition produces).
2. **Rank 3 leapfrog**: embed a real agent engine (Claude Agent SDK headless,
   pay-per-token) as the autopilot execution engine inside the existing
   envelope (worktree+leases+approval+SSE already built). This is the actual
   Fable-5-class move.
3. **Rank 5 UI**: desktop Autopilot diff viewer (patch text already reaches
   the client and is thrown away), consume the existing SSE endpoint instead
   of 5s polling, auto-start planning.
4. Rank 8/9/10: tokenized retrieval + ripgrep tool; exact string-replace
   edits + kill the 260/600-line truncations; defuse the learning-loop traps
   (pattern_miner fabricated 0.85 confidence → NULL template loop).
5. Dead-code purge (llm_router/, 2,000-line scorecard block, 6,148 lines dead
   Dart, hardcoded demo diffs).

## Honest framing for the goal

A harness cannot out-code its base model; the path to "Fable 5-adjacent at
~10–25% of $200/mo" is frontier-API-per-token inside CHILI's envelope +
the iterative read→edit→test→repair loop (rank 3), not more heuristics on
free models. Do NOT grind readiness scores or toy benchmarks; the metric
that matters: real tasks completed end-to-end per week, and how often the
operator still reaches for Claude Code.
