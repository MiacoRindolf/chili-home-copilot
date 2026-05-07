# NEXT_TASK: f-thread-housekeeping-followups-2026-05-07

STATUS: DONE

## Goal

Close the three optional follow-ups surfaced in the prior `f-thread-cleanup-2026-05-07` review so housekeeping is done end-to-end and `git status` returns to a state that reflects only real work-in-progress. Three locally-scoped subtasks bundled in one brief:

1. **Fix the `test_create_watchlist_item` fixture bug.** Hardcoded `user_id=1` against `tests/conftest.py`'s per-test users-table truncate. Pre-existing across many sessions; blocks `pytest tests/test_trading.py` from running clean. ~30 LOC.

2. **Add `.gitattributes` to normalize line endings + renormalize.** The 1,720-file CRLF mount-artifact noise (Windows host CRLF vs git's LF view) makes `git status --short` unusable for spotting real work. One-line `.gitattributes` + `git add --renormalize .` permanently closes it.

3. **Gitignore `docs/AUDITS/`.** Twelve untracked audit docs that are clearly operator-scratch — ad-hoc audit reports that drive specific briefs and then either get superseded or absorbed into formal CC reports. Best fit: untracked-by-default; operator can `git add -f` to share any specific audit. Same disposition for the other operator-scratch files at repo root (`HARDCODED_MAGIC_NUMBERS_AUDIT.txt`, `docs/AUDIT_PROMPT.md`, `docs/FAST_PATH_CLAUDE_CODE_PROMPT.md`).

After this lands, `git status --short` shows only legitimate in-progress work.

## Why now

The user wants the housekeeping fully done. Each item is small enough on its own to not justify a brief; bundled, they're one round-trip. None of them touch live trading code; lowest-risk cleanup batch on the queue.

## Scope boundary

**In scope:**
- Edit `tests/test_trading.py::test_create_watchlist_item` (and any sibling tests in the same class with the same hardcoded-user_id pattern; check `grep -n "user_id=1\|user_id = 1" tests/test_trading.py`).
- Create `.gitattributes` at repo root with line-ending normalization rules.
- Run `git add --renormalize .` to apply the new rules to all already-tracked files.
- Edit `.gitignore` to add `docs/AUDITS/` and the three repo-root operator-scratch files.

**Out of scope:**
- Any change to production trading code.
- Any DB migration.
- Any change to existing `.gitattributes` directives if the file already exists (verify first with `ls .gitattributes`).
- Any change to docs/STRATEGY/COWORK_REVIEWS/ tracking — that's Cowork's working backlog, separate from operator-scratch.

## Path

### Step 1 — Test fixture fix

```bash
grep -n "test_create_watchlist_item\|user_id=1\|user_id = 1" tests/test_trading.py
```

Find the test and any siblings with the same anti-pattern. Fix shape: replace `user_id=1` (and any other hardcoded user-table FK) with a fixture-created user. Pattern from elsewhere in the same file (look for `paired_client` or `db` fixture usages):

```python
def test_create_watchlist_item(db):
    # OLD: hardcoded user_id=1 fails when conftest truncates users
    # NEW: create a user via the existing User model and use its id
    from app.models.core import User  # adjust import path to match the file
    user = User(email="watchlist-test@example.com", display_name="WL Test")
    db.add(user); db.commit(); db.refresh(user)
    
    item = WatchlistItem(user_id=user.id, ticker="AAPL", note="test")
    db.add(item); db.commit(); db.refresh(item)
    
    assert item.id is not None
    assert item.user_id == user.id
    # ... rest of existing assertions
```

If the test already accepts a `paired_client` fixture (which seeds a user), use that user's id instead of creating a new one. Read the existing test before deciding which pattern fits.

Run after:

```powershell
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_trading.py -v --tb=short
```

Should run clean (or fail on a different unrelated issue, in which case surface to the operator and stop — the brief is scoped to the watchlist fixture only).

### Step 2 — `.gitattributes` CRLF normalize

Check first:

```bash
ls -la .gitattributes
```

If it exists, read it and only ADD the line-ending directive (don't overwrite anything). If it doesn't, create it.

Content:

```
# Line endings: store as LF in git, convert to platform-native on checkout.
# Closes the 1,720-file CRLF mount-artifact noise that comes from the
# Windows host (CRLF on disk) vs git's LF-internal view. Without this,
# every file shows as modified in `git status` on the Linux workspace
# mount, even though no content has changed.
* text=auto eol=lf

# Shell scripts must stay LF even on Windows checkouts (bash + Docker
# CMD interpretation requires it).
*.sh text eol=lf
*.bash text eol=lf

# PowerShell scripts on Windows are fine with CRLF; let git auto-detect.
*.ps1 text

# Binary types — never normalize.
*.png binary
*.jpg binary
*.jpeg binary
*.gif binary
*.ico binary
*.pdf binary
*.zip binary
*.tar binary
*.gz binary
*.7z binary
*.db binary
*.sqlite binary
```

Apply:

```bash
git add .gitattributes
git add --renormalize .
git status --short | head -20
```

The `--renormalize` will stage all the line-ending-only changes. This is expected to be ~1,720 files. Commit them in their OWN commit (separate from the test fixture and gitignore changes) so the diff is unambiguously "no semantic content, just line endings":

```bash
git commit -m "chore(repo): normalize line endings via .gitattributes

Adds * text=auto eol=lf to keep git's stored view as LF while letting
checkouts on Windows produce CRLF natively. Closes the ~1,720-file
'modified' noise in git status that comes from the Linux Docker bind
mount seeing CRLF-on-disk vs git's LF-internal view.

This commit's diff is large (every text file in the repo) but contains
zero semantic changes. Verified via 'git diff --ignore-all-space HEAD~1'
showing no content delta.

(f-thread-housekeeping-followups-2026-05-07 step 2)"
```

Then verify:

```bash
git diff --ignore-all-space HEAD~1 HEAD | wc -l
```

Should be 0 (or very small if any file had genuine content changes mixed in — investigate any non-zero result before pushing).

### Step 3 — Gitignore operator-scratch

Read `.gitignore`:

```bash
cat .gitignore
```

Append (don't overwrite):

```
# Operator audit working-set (pre-CC scratch; not intended for git).
# To share a specific audit, use 'git add -f docs/AUDITS/<file>'.
docs/AUDITS/

# Operator-scratch at repo root.
HARDCODED_MAGIC_NUMBERS_AUDIT.txt
docs/AUDIT_PROMPT.md
docs/FAST_PATH_CLAUDE_CODE_PROMPT.md

# Cowork's working backlog of strategy reviews — keep tracked normally
# (this is part of the Cowork ↔ CC protocol, not operator scratch).
# (No ignore line for docs/STRATEGY/COWORK_REVIEWS — intentional.)
```

Verify the previously-untracked files now show as ignored:

```bash
git check-ignore -v docs/AUDITS/2026-05-06_chili-app-closure-leak.md
git check-ignore -v HARDCODED_MAGIC_NUMBERS_AUDIT.txt
```

Both should report a match. `git status --short` should no longer list those files.

If any of the four target files are CURRENTLY TRACKED (check `git ls-files docs/AUDITS/ HARDCODED_MAGIC_NUMBERS_AUDIT.txt docs/AUDIT_PROMPT.md docs/FAST_PATH_CLAUDE_CODE_PROMPT.md`), un-track without deleting:

```bash
git rm --cached <each-tracked-file>
```

Don't `rm` the actual files — operator's working copies stay.

### Step 4 — Commit Step 1 + Step 3 together; Step 2 separately

```bash
# Commit 1: the small fixes (test fixture + gitignore)
git add tests/test_trading.py .gitignore
git commit -m "chore: fix watchlist test fixture + gitignore operator-scratch

- tests/test_trading.py::test_create_watchlist_item: replaced hardcoded
  user_id=1 with a fixture-created user. Pre-existing bug across many
  sessions; conftest.py per-test truncate of users table caused
  FK violation. Surfaced in f-thread-cleanup-2026-05-07 review.
- .gitignore: docs/AUDITS/ + 3 operator-scratch files at repo root.
  These are operator audit working-set; share specific items via
  'git add -f' when needed.

(f-thread-housekeeping-followups-2026-05-07 steps 1 + 3)"

# Commit 2: the renormalize (already done above)
# Already committed as a separate commit per Step 2.

# Commit 3: CC report + NEXT_TASK status
git add docs/STRATEGY/CC_REPORTS/2026-05-07_f-thread-housekeeping-followups-2026-05-07.md docs/STRATEGY/NEXT_TASK.md
git commit -m "docs(strategy): f-thread-housekeeping-followups-2026-05-07 CC report + mark NEXT_TASK done"
```

Three commits total: small fixes, renormalize, docs. Renormalize commit is the only large diff; the other two are surgical.

## Constraints / do not touch

- **No new magic numbers.** This brief introduces no new thresholds. The 1,720-file CRLF count is descriptive (it's the count of mount-artifact files at brief-write time), not a configurable threshold.
- **No production trading code.** Don't touch `app/services/trading/*` or `app/services/broker_service.py`.
- **No `git push --force` to main.** Standard PROTOCOL Hard Rule.
- **The renormalize commit must be its own commit.** Mixing the renormalize diff with semantic changes would make the diff unreadable. Two separate `git commit` calls.
- **Don't add anything to `.gitattributes` beyond what's specified.** No bespoke per-file rules; let `text=auto` do its job.
- **Don't `git rm` actual files.** Use `git rm --cached` for tracked-but-now-ignored files; the working-copy stays for the operator.

## Success criteria

1. **Three commits, all pushed:**
   - `chore: fix watchlist test fixture + gitignore operator-scratch (f-thread-housekeeping-followups-2026-05-07 steps 1 + 3)`
   - `chore(repo): normalize line endings via .gitattributes (...step 2)`
   - `docs(strategy): f-thread-housekeeping-followups-2026-05-07 CC report + mark NEXT_TASK done`
2. **`git status --short` is clean.** After the three commits, the only entries should be live runtime artifacts (`brain_worker.log`, `data/ticker_cache/crypto_top.json`) and any operator work-in-progress that's actually in-progress. No CRLF mount churn, no `docs/AUDITS/*`, no `.commit_msg*.txt`.
3. **Test fixture passes.** `pytest tests/test_trading.py::TestTradingModels::test_create_watchlist_item -v` (or whatever the test class name actually is) returns green.
4. **Renormalize diff is byte-equivalent.** `git diff --ignore-all-space HEAD~2 HEAD~1 | wc -l` returns 0.
5. **Gitignored files no longer appear.** `git status --short | grep -E "AUDITS|HARDCODED|AUDIT_PROMPT|FAST_PATH"` returns nothing.
6. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-07_f-thread-housekeeping-followups-2026-05-07.md` per PROTOCOL format. Include:
   - Per-step status (3 steps).
   - Whether any test sibling beyond `test_create_watchlist_item` had the same hardcoded-user pattern.
   - Renormalize file count (expected ~1,720).
   - Confirmation that `docs/STRATEGY/COWORK_REVIEWS/*` was NOT gitignored (it should remain tracked per the Cowork ↔ CC protocol).
   - Magic-number audit (zero new literals).

## Rollback plan

- **Step 1 (fixture):** `git revert <commit>` — test goes back to FK-violation state. No live-money impact.
- **Step 2 (renormalize):** `git revert <commit>` — line endings flip back. No semantic regression. If the renormalize itself broke something (rare; should only happen if a test asserts on raw bytes including line endings), the revert is clean.
- **Step 3 (gitignore):** edit `.gitignore` to remove the entries; the working-copy files reappear as `??` in `git status`. Operator can `git add` selectively to track them.

## Verification commands (for the executor + the operator)

```powershell
# 1. Test passes
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_trading.py::TestTradingModels::test_create_watchlist_item -v

# 2. .gitattributes has the directive
grep "text=auto" .gitattributes

# 3. Renormalize commit is byte-equivalent
git diff --ignore-all-space HEAD~2 HEAD~1 | wc -l
# Expected: 0

# 4. Gitignore covers the targets
git check-ignore docs/AUDITS/2026-05-06_chili-app-closure-leak.md
git check-ignore HARDCODED_MAGIC_NUMBERS_AUDIT.txt

# 5. git status is clean
git status --short
# Expected: only brain_worker.log, data/ticker_cache/crypto_top.json (runtime),
# possibly docs/STRATEGY/COWORK_REVIEWS/* (Cowork's review backlog).
```

## Open questions for Cowork (surface in CC report — likely none)

If the renormalize diff turns out to have non-zero `--ignore-all-space` content, that's a real surprise — a file in the repo has trailing-whitespace-only changes or BOM differences that aren't pure line endings. Surface for operator review before committing.

If any test in `test_trading.py` beyond `test_create_watchlist_item` is also broken by a hardcoded `user_id` and the fix isn't trivially the same shape, surface that too rather than fixing in this brief.

## Forward pointer

After this lands, the strategy thread is fully closed. No housekeeping debt remaining. The next NEXT_TASK is whatever the operator queues fresh — Phase 2 of the position-identity refactor when the 2026-05-11 soak completes, or any new bug class.
