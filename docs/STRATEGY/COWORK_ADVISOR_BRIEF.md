# Cowork Advisor Brief

This brief is curated by Cowork (the strategy/architect side of the
collaboration) for Claude Code (the executor) to read at the start of
every session, immediately after `CLAUDE.md` and `PROTOCOL.md`.

It captures what's NOT in the repo: project lore, recurring failure
patterns, operator preferences, and currently-armed state. Reading
the live code answers "what does it do." Reading this answers "what
should I avoid, and why."

Last updated: 2026-05-09.

---

## 1. Operator preferences (binding)

- **Action over narration.** Don't write trailing summaries the operator can read in the diff. End responses where the work ends.
- **Take initiative.** When tools cover the task (Read/Write/Edit/Bash), do the work. Only hand back to the operator for things outside the sandbox (Docker on host, PowerShell on host, broker UI clicks).
- **Cowork is not live during sessions.** When you (CC) are running, Cowork is not watching. There's no real-time chat. Communication is asynchronous via the filesystem (NEXT_TASK.md, CC_REPORTS, COWORK_REVIEWS, plan-gate consultation files — see §6).
- **Don't reflexively apologize or self-abase on mistakes.** Acknowledge, fix, move on. The operator wants steady honest helpfulness, not theatre.

---

## 2. Hard hazards in this codebase (do these wrong and you break prod)

### 2.1 Edit tool silently truncates large files

The Edit tool can leave a file truncated mid-line with the same approximate byte count. **Trigger size is around 2000 lines but smaller files have hit it too.** Symptom: `wc -l` differs from `git show HEAD | wc -l`, brace-depth check finds unbalanced braces, file ends mid-statement.

**Mitigations:**
- For files >500 lines: prefer `Write` (full file overwrite) over `Edit`.
- After every Edit on a non-trivial file: run `wc -l` AND `git diff --stat` AND parse the file (Python `ast.parse` for .py, PowerShell `[System.Management.Automation.Language.Parser]::ParseFile` for .ps1).
- Before any container restart: scan all files modified this session against `git show HEAD:<path> | wc -l` to catch silent truncation. Restoring from HEAD via `git checkout HEAD -- <path>` is your friend.

Past incidents this hit: `app/migrations.py` (mig 072 ended mid-line), `_claude_session_daemon.ps1` (lost ~30 lines silently, recovered from git HEAD), 13 other files in May 2026.

### 2.2 PowerShell Out-File destroys .env

`Out-File -Encoding utf8` on Windows PS 5.1 adds a BOM. `-NoNewline` strips line terminators. Either one alone corrupts `.env` irrecoverably and breaks pydantic-settings parsing — every container restart-loops.

**Mitigations:**
- Never use `Out-File` on `.env` or any file whose content must round-trip exactly.
- For in-place byte mutation: `[System.IO.File]::ReadAllBytes` → mutate → `[System.IO.File]::WriteAllBytes` with `[System.Text.Encoding]::ASCII.GetBytes`.
- Have a SHA256-validating recovery script handy: `scripts/d-env-rebuild.py` (forensic two-pass split with name-boundary detection).

Hit on 2026-05-09 trying to flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`. Took hours to recover.

### 2.3 Connection leaks via missing rollback before close (FIX 46)

SQLAlchemy `session.close()` does NOT end implicit read transactions. Pattern `try: ...; finally: db.close()` leaks idle-in-transaction sessions until they pile up (62-69 per scanner pass) and Postgres runs out of slots.

**Mitigations:**
- Every DB session ending: `try: ...; finally: db.rollback(); db.close()`.
- When touching any service that opens DB sessions: grep for `finally:\s*db\.close()` and patch all matches.
- Verify post-fix with `pg_stat_activity` query — should see idle-in-tx counts drop sharply after restart.

### 2.4 Test DB safety (`_test`-suffix mandatory)

`tests/conftest.py` hard-fails if `TEST_DATABASE_URL` doesn't end in `_test`. The fixture TRUNCATES tables. Bypassing this guard by editing conftest is forbidden — it's there because it caught a near-miss against prod.

### 2.5 Migration IDs are sequential and idempotent

Add `_migration_NNN_*()` to `app/migrations.py`, register in the `MIGRATIONS` list. Never reuse IDs. Always check for existence before `ALTER` (the migration may run multiple times during dev). The startup guard `_assert_migration_ids_unique` will fail-stop the app on duplicates.

Run `.\scripts\verify-migration-ids.ps1` before committing if you added a migration.

### 2.6 Don't hardcode fallback values for missing measurements

Examples to avoid:
```python
win_rate = stats.get("win_rate") or 0.5  # ❌ "or 0.5" is a magic constant
ev = compute_ev(...) if data else 0.0      # ❌ silent default
```

These corrupted EWMA blends in the past (the WR-corruption incident: 11 NaN + 11 out-of-range rows traced to `or 0.5`). Instead: compute dynamically from observable data (population mean, Bayesian posterior with a sensible prior), or propagate `None` and decide at the caller. If you must default, log a warning at the default site so the bug is visible.

---

## 3. Trading hard rules (CLAUDE.md restates these — don't forget)

1. Kill switch before any automated trade. `ensemble_promotion_check` must pass.
2. Drawdown breaker before sizing.
3. Data-first, code-second. If symptoms look like FK / linkage corruption, fix the DB + add a migration. Don't paper over with a router/service filter.
4. Tests must use `_test`-suffixed DB.
5. Prediction mirror authority is FROZEN (Hard Rule 5). Phases 3-8 of `app/trading_brain/` cannot be eroded by side edits. Phase changes need new phase + design + tests + soak + rollout doc.
6. Migrations sequential and idempotent.

The autotrader's 7-stage gate chain (kill switch / drawdown breaker / rule floor / LLM / cost-gate / cap-check / bracket writer) is the capital-protection layer. **Promotion-eval mistakes don't lose money directly** — they pollute the patterns that generate alerts. We can be less conservative on promotion eval than on capital gates.

---

## 4. Trading brain — three-lane architecture

The brain processes events through three lanes:

1. **Reconcile pass** — broker truth syncs every 2min. Authoritative for live fills.
2. **Work-ledger** — async events (mine / cpcv_gate / promote / demote / regime_ledger / pattern_stats) flow through handlers. Empty ledger usually means handler import failure (silent no-op for 6 days in May 2026 — known hazard).
3. **Scheduler-batch** — APScheduler jobs (every 5/15/30min). Batch ops over snapshots.

When debugging "why isn't X happening," map symptoms to the right lane:
- "Realized P&L wrong" → reconcile pass
- "Patterns not promoting/demoting" → work-ledger handlers
- "Imminent alerts not firing" → scheduler-batch (`pattern_imminent_scanner`)

Misreading: don't blame the reconcile pass for handler-import bugs.

---

## 5. Currently-armed state (as of 2026-05-09)

- **Coinbase Phase 6 paper-soak: LIVE.** `CHILI_COINBASE_AUTOTRADER_LIVE=1` with `$150` max exposure, runs through 2026-05-11. Don't touch broker_selector, cost_aware_gate, or bracket_writer_g2 without strong reason — soak is in flight.
- **f-promotion-pipeline-rebalance: Phase 1 done.** Pattern 585 protected from auto-demote by sample-size floor + CPCV-passing escape. Phases 2-6 in QUEUED.
- **Eligible promoted-pattern roster: 3** (1011, 1016, 585). Drove the rebalance initiative.
- **Robinhood byte-identical parity gate is HARD.** Phase 3 of f-promotion-pipeline-rebalance must not break it. If parity test fails, STOP the chain.

---

## 6. The plan-gate consultation protocol

Phase 3 onwards uses a consultation gate to catch design errors before coding begins. CC's session prompt will instruct:

1. Read CLAUDE.md, PROTOCOL.md, this brief, NEXT_TASK.md, and the QUEUED brief — in that order.
2. Develop your implementation plan covering: file paths, migration ID, test cases, edge cases, deviations from the brief.
3. Write the plan to `scripts/_claude_session_consult/$env:CHILI_SESSION_ID/plan.request.md`.
4. Poll for `plan.response.md` in the same directory every 30s, up to 2h.
5. The response will be one of:
   - `APPROVED` — proceed with the plan exactly as written.
   - `REVISE: <feedback>` — rewrite the plan addressing the feedback, then re-submit (overwrite request file). Loop.
   - `ABORT: <reason>` — write a CC_REPORT explaining the abort and exit non-zero (code 7).
6. After APPROVED, implement → test → commit → push → write CC_REPORT → update NEXT_TASK.

Cowork (me) responds to plan requests when the operator pings me with the session ID. The chain stalls if neither operator nor Cowork is reachable — that's by design; better a stall than wrong code.

**For pre-Phase-3 sessions (no consult dir):** ignore §6 and proceed under the standard protocol.

---

## 7. Workflow checklist for every CC session

At session start:
- [ ] Read CLAUDE.md (project rules), PROTOCOL.md, this brief, NEXT_TASK.md, the QUEUED brief
- [ ] Check `git log --oneline -5` to know what shipped recently
- [ ] If `$env:CHILI_SESSION_ID` is set, you're in a session-daemon run with the plan-gate active — follow §6

At session end:
- [ ] All edits done? Run truncation scan on every file you touched: `wc -l` vs `git show HEAD:<path> | wc -l`
- [ ] Tests pass under `TEST_DATABASE_URL=...chili_test`
- [ ] AST parse clean on every modified .py / .ps1
- [ ] CC_REPORT written at `docs/STRATEGY/CC_REPORTS/<date>_<slug>.md`
- [ ] NEXT_TASK.md status updated (PHASE_N_DONE or DONE)
- [ ] `git add` + `git commit` + `git push`
- [ ] If you discovered a new failure pattern that future-you should know: append a §2 entry to this brief in your commit

---

## 8. When to push back

You (CC) are the executor and have judgment. If the brief asks you to do something that conflicts with this advisor brief or CLAUDE.md, FLAG IT and either:
- Use the plan-gate to ask Cowork (recommended for design-level conflicts)
- Add a clear note in the CC_REPORT and proceed with the safer interpretation
- Stop and write a clarification request to NEXT_TASK before proceeding

The operator explicitly values pushback over compliance when there's a real conflict. "Flag the conflict in one sentence, ask if unclear, then proceed with the user's explicit authorization."
