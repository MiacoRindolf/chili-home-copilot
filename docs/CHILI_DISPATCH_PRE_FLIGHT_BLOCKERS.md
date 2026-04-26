# CHILI Dispatch — Pre-flight blockers

Findings from the second-pair-of-eyes review of the design. These are the
items that must be resolved BEFORE Phase D.0 (logging-only) can ship to
running scheduler workers, and definitely before Phase D.4 (auto-merge
to main).

## Verified-not-an-issue (after testing)

- **fnmatch `**` recursion.** Reviewer claimed `fnmatch.fnmatch('app/services/trading/sub/file.py', 'app/services/trading/**')` returns `False`. Tested on CPython 3.11: returns `True`. fnmatch's `*` matches across path separators by design, so `**` is effectively `*` for our purposes. The frozen-scope guard at `app/services/code_dispatch/frozen_scope.py` is correct, but a regression test in `tests/test_frozen_scope_guard.py` is still required before D.0 ships, to lock the behavior in.

## BLOCKER 1 — Validation oracle poisoning (HIGH)

**Risk.** The distillation pipeline filters on `validation_status='passed'`. A model that learns to write tests like `def test_x(): assert True` will get 100% pass rate and become the new tier-1. The local model then trains on its own bad-but-passing output and drifts.

**Mitigation, must be in place before D.5:**

1. Validation must include a **mutation-test pass** for any newly-added test files: temporarily flip an assertion in the diff's *production* code; the new test must catch it. If the test still passes after mutation, the test is empty and the run is rejected (`distillable=false`, `decision='reject'`).
2. The 100-task **golden eval set** (`app/services/distillation/golden_tasks.json`) is hand-curated, version-controlled, and code-reviewed annually. It is never auto-generated, never expanded by Dispatch, and never used as training data.
3. Mark golden tasks as `"is_golden": true` and add a database constraint: `distillation_runs.candidate_pass` is computed *only* from golden tasks; the broader 100-row eval is for latency, not pass-rate.
4. Rotate the held-out eval set quarterly. Keep three eval rotations and log which was used.

## BLOCKER 2 — Token-burn attack via failure loop (HIGH)

**Risk.** `scorer.choose_tier` bumps tier on each retry. A maliciously-crafted task description (or a poisoned planner row) that consistently produces a refusal will escalate from tier-1 to tier-4, costing premium tokens on every cycle. Multiple such tasks can drain the daily budget.

**Mitigation, must be in place before D.2:**

1. Add `max_tier_spend_per_task` env (default `$2.00`). `rule_gate.passes_code_rule_gate` rejects tasks whose cumulative `cost_usd` from prior runs exceeds this.
2. Add a per-task velocity cap: a single `task_id` cannot retry more than 3 times in any 24h window, regardless of `prior_failure_count`.
3. Detect the "always-refuses" signature: if `weak_response=true` for the same task across two consecutive tiers, jump straight to escalation, not to tier-3. The retry budget cannot be a money pump.

## BLOCKER 3 — Validation hang locks the scheduler (HIGH)

**Risk.** `runner.apply_suggestion_in_worktree` calls `coding_service.run_validation_at_path` with no timeout. A pytest fixture that hangs (or a network call inside a test) will block the cycle. After 60s the next cycle starts and stacks up.

**Mitigation, must be in place before D.2:**

1. Wrap validation in a hard timeout (default 5 min). On timeout: kill the subprocess tree, increment `code_kill_switch_state.consecutive_failures`, audit `decision='timeout'`.
2. Three timeouts in any 10-cycle window auto-trips the code kill switch.
3. APScheduler job already passes `max_instances=1, coalesce=True`; verify this is set when the job is added.

## BLOCKER 4 — Indirect frozen-scope bypass (MEDIUM)

**Risk.** A diff that does not directly modify a frozen path could still indirectly affect it: e.g. adding `exec(open('app/services/trading/governance.py').read() + '\n_kill_switch = False')` to a non-frozen file, or creating a symlink, or modifying `sys.path` to shadow a frozen module.

**Mitigation, must be in place before D.4:**

1. Static AST scan of every diff hunk: flag any `exec`, `eval`, `__import__`, `importlib.import_module`, `sys.path` mutation, `os.symlink`, file-open against frozen paths. Match → `severity='review_required'`.
2. Reject diffs that introduce new files at the top level (e.g. `loader.py`) without a corresponding planner-task `intended_files` allowlist match.
3. Add a smoke test that imports `app.services.trading.governance` and asserts `is_kill_switch_active() in {True, False}` after every merge.

## BLOCKER 5 — Distillation eval set leakage (MEDIUM)

**Risk.** Repeatedly comparing candidates against the same eval set causes that set to gradually leak into training data (model learns to recognize evaluation prompts).

**Mitigation, before D.5:**

1. Three eval set rotations, used round-robin. `distillation_runs.eval_set_id` records which one.
2. The eval rotation a candidate uses must be different from any rotation the *current incumbent* was promoted on.
3. Quarterly review: discard any rotation that has been used by ≥3 distillation runs.

## BLOCKER 6 — Tier-1 model swap mid-cycle (MEDIUM)

**Risk.** `router.route_chat` reads `CHILI_LOCAL_CODER_MODEL` env on each provider call. If distillation atomically promotes a new tag mid-cycle, in-flight calls switch underneath us, contaminating the audit trail.

**Mitigation, before D.5:**

1. Snapshot the active tier-1 model at cycle start (in `code_agent_runs.llm_snapshot`). All subsequent calls within the cycle use the snapshot, not the current env.
2. Promotion writes to `distillation_runs.candidate_tag` and updates a registry row in `code_kill_switch_state` (or a new `model_registry` table). Code reads the registry, not the env.

## BLOCKER 7 — Failed audit-row insertions silently swallow escalations (LOW)

**Risk.** `cycle.py` opens an audit row before deciding; if the DB is unreachable, `run_id=None` and `audit.close_run` becomes a no-op. The escalation path (notify user) is conditional on `run_id`. Failures get swallowed.

**Mitigation, before D.0:**

1. If `audit.open_run` returns `None`, log at `CRITICAL`, write a fallback row to a local JSONL file `logs/dispatch_emergency.jsonl`, and trip the kill switch — DB connectivity is a precondition.

## Pre-flight checklist (before flipping `CHILI_DISPATCH_ENABLED=1`)

- [ ] Migration 171 reviewed and integrated into `app/migrations.py` MIGRATIONS list
- [ ] `tests/test_frozen_scope_guard.py` covers nested paths, symlink-shaped paths, Windows backslash normalization
- [ ] `tests/test_rule_gate.py` covers per-task spend cap, velocity cap, weak-response twice rule
- [ ] Validation timeout wrapper merged
- [ ] Indirect-bypass AST scan merged in `frozen_scope.py`
- [ ] Tier-1 model snapshot in `code_agent_runs.llm_snapshot` on cycle open
- [ ] Audit-row failure path writes to local emergency log + kill switch
- [ ] Golden eval set (≥10 tasks) hand-curated, code-reviewed, marked `is_golden=true`
- [ ] Three eval-set rotations seeded
- [ ] Mutation-test pass added to validation harness
- [ ] Production smoke test added to `scripts/post-merge-smoke.ps1`
- [ ] Operator can activate code kill switch from the brain UI (not just CLI)
- [ ] Two consecutive shadow runs without escalation
