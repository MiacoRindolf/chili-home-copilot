# Postmortem: the autonomous coding loop's resurrection night (2026-06-11)

**Status:** resolved; the dispatch loop produced its first fully-passing
autonomous change (task 39 → PR #648). Keep for institutional memory.

## What was broken (each verified live, each fixed with a tested PR)

1. **Frontier tier quadruple-dead** (#617): high_stakes DB policies vetoed
   the override; `code_dispatch_plan` used tree routing which ignores model
   overrides; claude-* temperature rejection; no streaming tier.
2. **Validation crashed every run** (#618): `run_ast_syntax(worktree,
   changed_files)` vs single-arg signature → TypeError swallowed; pytest
   skip-passed because the sandbox env stripped `TEST_DATABASE_URL`.
3. **Frozen-scope guard was dead** (#619): the pre-check ran on
   `intended_files` the miner can't populate; no post-apply check existed.
4. **Watcher watched a state nothing produces** (#625):
   `ready_for_dispatch` is not a real `coding_readiness_state`;
   `code_brain_events` had ZERO rows ever. Real states: `brief_ready` etc.
5. **Re-enqueue spam loop** (#626): terminal draft failures re-enqueued the
   same task every 30s; dedupe now suppresses until the task row changes.
6. **Small-model edit mechanics** (#622/#623/#641/#643): local-first
   qwen2.5-coder:7b tier; SEARCH/REPLACE blocks instead of unified-diff
   guessing; fuzzy/unicode (em-dash!) matching mapped back to the exact
   original span; REPLACE re-indented to file truth.
7. **Validation measured the baseline, not the change** (#644): repo-wide
   pytest collect inherited pre-existing main breakage; now scoped to
   changed files.
8. **Blanket `git worktree prune` destroyed other agents' worktrees**
   (#627): host-side worktree paths are invisible in-container; cleanup is
   now scoped to the task's own registration.

## Operating lessons

- The dispatch worktree base is the LOCAL `main` ref of /workspace.
- Container swaps orphan in-flight events (claimed, never processed);
  `reap_stuck_claims` unclaims them after 30 min.
- One-off race-free run: `docker run --rm` with
  `CHILI_DISPATCH_TASK_STATUSES=<private state>`.
