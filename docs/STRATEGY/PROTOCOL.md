# Cowork ↔ Claude Code Strategy Protocol

This file defines how a Cowork-driven strategy session hands work to a Claude Code execution session, and how Claude Code reports back. **Claude Code: read this on every session start, then read `NEXT_TASK.md`.**

## Roles

- **Cowork (planner/strategist).** Talks to the operator. Writes plans, evaluates tradeoffs, reviews Claude Code's output, decides priority. Has cross-session memory. Does NOT execute large code changes itself.
- **Claude Code (executor).** Reads the next task brief, implements it on the host with direct shell/git/docker/psql access. Commits work. Writes a completion report. Does NOT make architectural decisions outside the brief — flags them back instead.
- **Operator (user).** Decides direction with Cowork. Runs `claude` to execute. Reviews diffs/results.

## Files in this directory

```
docs/STRATEGY/
├── PROTOCOL.md          # This file. Read first by Claude Code each session.
├── CURRENT_PLAN.md      # The active initiative — what we're building, why, success criteria.
├── NEXT_TASK.md         # The single concrete task for the next Claude Code run.
│                        #   Marked DONE in-place when complete (don't delete).
├── CC_REPORTS/          # Claude Code completion reports, one file per task.
│   └── YYYY-MM-DD_<slug>.md
└── COWORK_REVIEWS/      # Cowork's review of each CC report, mirroring filename.
    └── YYYY-MM-DD_<slug>.md
```

## The loop

**Strategy step (Cowork session)**
1. Operator and Cowork discuss direction.
2. Cowork updates `CURRENT_PLAN.md` if the broader initiative shifted.
3. Cowork writes `NEXT_TASK.md` — one concrete task, with explicit:
   - Goal (what success looks like)
   - Constraints (what NOT to touch, e.g., safety belts, frozen contracts)
   - Brain integration points (which existing modules to reuse instead of rewriting)
   - Out-of-scope items (so Claude Code doesn't drift)
   - Success criteria (how Claude Code knows it's done)
4. Operator types `claude` in the project directory. That's it.

**Execution step (Claude Code session)**
1. On launch, Claude Code reads (in order):
   - `CLAUDE.md` (project rules)
   - `docs/STRATEGY/PROTOCOL.md` (this file)
   - `docs/STRATEGY/CURRENT_PLAN.md` (initiative context)
   - `docs/STRATEGY/NEXT_TASK.md` (today's task)
2. If `NEXT_TASK.md` is marked `STATUS: DONE`, Claude Code says so and waits for further instruction.
3. Otherwise Claude Code:
   - Plans (one short paragraph or bullets) and asks the operator for go-ahead if scope feels off.
   - Implements.
   - Tests / verifies (smoke or pytest as appropriate).
   - Commits with a clear message referencing the task slug.
   - Writes `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_<slug>.md` describing: what shipped, what soak/test results showed, what surprised, what's deferred. Format below.
   - Marks `NEXT_TASK.md` `STATUS: DONE` (does not delete it; the file is overwritten on the next strategy step).

**Review step (Cowork session)**
1. Cowork reads the latest CC_REPORT.
2. Cowork writes the matching review in `COWORK_REVIEWS/`: what's good, what's concerning from algo-trader and dev-architect lenses, what's the next thing.
3. Cowork updates `CURRENT_PLAN.md` if the report changed the picture.
4. Loop back to strategy step.

## NEXT_TASK.md format

```markdown
# NEXT_TASK: <slug>

STATUS: PENDING   <!-- or DONE when Claude Code finishes -->

## Goal
One paragraph. What does success look like?

## Why now
What did the last review reveal that makes this the right next move?

## Brain integration (reuse, don't rewrite)
- Module path → public function to call
- Module path → public function to call

## Constraints / do not touch
- Frozen contracts (e.g., live placement safety belts)
- Known good behaviors not to regress

## Out of scope
- Things that are tempting but belong to a later phase

## Success criteria
- Something measurable. "30-min paper soak shows N exits with realized P/L recorded"
- Or: "all tests pass + commit pushed + report written"

## Rollback plan
How to undo if something goes wrong mid-deploy.
```

## CC_REPORT format

```markdown
# CC_REPORT: <slug>

## What shipped
- Commit hash + one-line summary
- Files touched (count)
- Migrations added

## Verification
- Smoke results
- Test results (pass/fail)
- Live system observations

## Surprises / deviations
- Anything I had to do that wasn't in the brief
- Anything I deferred or recommend Cowork rethink

## Deferred
- Items I noted but explicitly didn't do, with reason

## Open questions for Cowork
- Things I want a strategy decision on before next task
```

## Hard rules

1. **Claude Code never modifies the live-placement safety belts.** See `docs/FAST_PATH_HANDOFF.md` for the full contract. Three flags + 8 belts. Cowork can authorize changes; Claude Code cannot self-authorize.
2. **Default mode stays paper.** Compose default `CHILI_FAST_PATH_MODE=paper`. Don't flip in code.
3. **No new magic numbers.** If Claude Code finds itself typing a hardcoded threshold, it should stop and either (a) call an existing brain function, or (b) flag it back to Cowork in the report's Open Questions section so Cowork can decide where the dynamic value should come from.
4. **No `git push --force` to main.** Standard rule.
5. **Tests use `_test`-suffixed DB.** See `CLAUDE.md` Hard Rule 4.
6. **Commit boundaries respect phases.** One task = one logical commit (or a tight series). Don't bundle unrelated changes.

## Why this protocol

- **Operator types `claude` and walks away.** Friction-free execution.
- **Cowork's strategy work is durable** — written to repo, survives sessions, becomes part of project history.
- **Reviews are written, not transient.** When something goes wrong six months from now we can read the chain that led there.
- **Claude Code stays in execution mode.** It's at its best implementing, not architecting. Strategy decisions go through Cowork where the operator can hold a real conversation.
