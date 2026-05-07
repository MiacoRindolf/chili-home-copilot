# NEXT_TASK: f-thread-housekeeping-tail-2026-05-07

STATUS: DONE

## Goal

Close the two actionable follow-ups surfaced in the prior `f-thread-housekeeping-followups-2026-05-07` review:

1. **Sweep the rest of `tests/test_trading.py`** for the `user_id=1` hardcoded-FK anti-pattern. 47 hits across 4 service-layer test classes (`TestWatchlistService` 12, `TestTradeService` ~16, `TestJournalService` ~7, `TestTradeStats` ~5+). Same fix shape as the prior brief's `TestTradingModels` step. After this lands, `pytest tests/test_trading.py -v` runs green for everything below the API-layer classes.

2. **Commit the `docs/STRATEGY/COWORK_REVIEWS/*` backlog.** The Cowork ↔ CC protocol expects these tracked alongside `CC_REPORTS/`, but the operator hasn't been `git add`-ing them. Sweep all the untracked review files into one docs commit so the strategy thread's full audit trail is in git history.

The third follow-up from the review (working-tree disk view sync) is operator-side — the working-tree-vs-HEAD CRLF lag is resolved by the operator re-syncing the mount or `git checkout-index -f -a` from the Windows host. Not in scope here.

## Why now

The user wants the housekeeping fully done. Both items are small, scoped, and unblock real value: green test suite + complete review audit trail in git. Bundling beats two separate small briefs.

## Scope boundary

**In scope:**
- Edit `tests/test_trading.py` — apply the same `_seed_user(db, name)` helper pattern from the prior brief to the 4 service-layer test classes (`TestWatchlistService`, `TestTradeService`, `TestJournalService`, `TestTradeStats`). Replace each hardcoded `user_id=1` with a fixture-created user's id.
- `git add docs/STRATEGY/COWORK_REVIEWS/*` to track the review backlog.

**Out of scope:**
- API-layer test classes (`TestTradingPageAPI`, `TestWatchlistAPI`, `TestTradesAPI`, `TestJournalAPI`, `TestMarketDataAPI`, `TestInsightsAPI`, `TestPortfolioAPI` — line 345 onward in `tests/test_trading.py`). These use the `paired_client` fixture which already seeds a user; they're a different pattern. Confirm by reading one before assuming.
- Any test file beyond `tests/test_trading.py`.
- Working-tree CRLF disk-view sync (operator-side host-mount refresh).
- Any production code changes.
- `tests/conftest.py` modifications. The `_seed_user` helper lives in the test file, not the shared conftest.

## Path

### Step 1 — Apply `_seed_user` helper to the 4 service-layer classes

The helper already exists from the prior brief (`tests/test_trading.py` should have a module-level `_seed_user(db, name)` function added by commit `aa5fd0c`). Verify it's present:

```bash
grep -n "_seed_user" tests/test_trading.py | head -5
```

If it's there: just call it. If it isn't (unlikely; the prior commit added it for `TestTradingModels`): create it once at module level.

For each class, update the test methods. Pattern shape (TestWatchlistService example):

```python
class TestWatchlistService:
    def test_add_to_watchlist(self, db):
        user = _seed_user(db, "watchlist-add-test")
        item = ts.add_to_watchlist(db, user_id=user.id, ticker="AAPL")
        # ... rest of existing assertions
```

Each test method gets its own `_seed_user` call with a unique name suffix tied to the test name. The per-test truncate in `tests/conftest.py` ensures clean state between tests.

For methods that need MULTIPLE distinct users (e.g., `TestJournalService::test_journal_user_isolation` if it exists, or any test that asserts cross-user isolation), seed multiple users:

```python
user_a = _seed_user(db, "journal-isolation-a")
user_b = _seed_user(db, "journal-isolation-b")
# ... assert content for user_a.id doesn't bleed into user_b.id
```

Read the existing test before deciding if the user-isolation case applies; some tests at line 302 reference "User 1 note" suggesting a single-user scope, but the test name will tell you.

**Don't introduce new test classes or new fixtures.** This is a literal swap: `user_id=1` → `user_id=user.id`. Same structure.

Run after each class is done:

```powershell
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_trading.py::TestWatchlistService -v --tb=short
pytest tests/test_trading.py::TestTradeService -v --tb=short
pytest tests/test_trading.py::TestJournalService -v --tb=short
pytest tests/test_trading.py::TestTradeStats -v --tb=short
```

Each class should run green. If any test fails on a real (non-fixture) bug, surface it to the operator and stop — the brief is scoped to the FK fix only, not test-logic fixes.

Final pass:

```powershell
pytest tests/test_trading.py -v --tb=short
```

Whole file should run clean OR fail only on API-layer classes (which weren't touched by this brief). Document either outcome in the CC report.

### Step 2 — Commit the COWORK_REVIEWS backlog

```bash
ls docs/STRATEGY/COWORK_REVIEWS/
```

Should show all the Cowork reviews from this thread (and possibly older ones). All untracked. Stage them:

```bash
git add docs/STRATEGY/COWORK_REVIEWS/
git status --short docs/STRATEGY/COWORK_REVIEWS/
```

Verify no surprises (e.g., a half-written draft file). If any file's content looks like an incomplete draft, surface to the operator before committing.

### Step 3 — Commit and push

```bash
# Commit 1: test sweep
git add tests/test_trading.py
git commit -m "test(trading): sweep remaining user_id=1 hardcodes across service classes

Closes the per-class FK-violation pattern surfaced in the prior brief's
TestTradingModels fix. Applies _seed_user(db, name) to:

- TestWatchlistService (12 hits)
- TestTradeService (~16 hits)
- TestJournalService (~7 hits)
- TestTradeStats (~5+ hits)

API-layer classes (TestTradingPageAPI onward) use the paired_client
fixture and are out of scope for this brief.

Verified: pytest tests/test_trading.py -v -> service-layer classes green.

(f-thread-housekeeping-tail-2026-05-07 step 1)"

# Commit 2: track Cowork review backlog
git add docs/STRATEGY/COWORK_REVIEWS/
git commit -m "docs(strategy): commit Cowork review backlog

The Cowork <-> CC protocol expects COWORK_REVIEWS/* tracked alongside
CC_REPORTS/*. The operator had been leaving them untracked; this commit
stages the backlog so the strategy thread's full audit trail is in git
history.

(f-thread-housekeeping-tail-2026-05-07 step 2)"

# Commit 3: docs
git add docs/STRATEGY/CC_REPORTS/2026-05-07_f-thread-housekeeping-tail-2026-05-07.md docs/STRATEGY/NEXT_TASK.md
git commit -m "docs(strategy): f-thread-housekeeping-tail-2026-05-07 CC report + mark NEXT_TASK done"
```

Three commits: test fix, review backlog, docs.

## Constraints / do not touch

- **No new magic numbers.** No numerical thresholds anywhere in this brief.
- **No production trading code.** Tests only.
- **No `tests/conftest.py` edits.** The shared fixture surface is stable; the helper lives in `tests/test_trading.py`.
- **No API-layer test refactors.** They use a different fixture pattern; out of scope.
- **No `git push --force` to main.** Standard PROTOCOL Hard Rule.

## Success criteria

1. **Three commits, all pushed:**
   - `test(trading): sweep remaining user_id=1 hardcodes across service classes`
   - `docs(strategy): commit Cowork review backlog`
   - `docs(strategy): f-thread-housekeeping-tail-2026-05-07 CC report + mark NEXT_TASK done`
2. **`pytest tests/test_trading.py -v`** — all 4 service-layer classes (`TestWatchlistService`, `TestTradeService`, `TestJournalService`, `TestTradeStats`) green. API-layer classes either also green (bonus) or fail on unrelated reasons documented in the CC report.
3. **`git ls-files docs/STRATEGY/COWORK_REVIEWS/ | wc -l`** ≥ 5 (this thread alone added 5+ reviews).
4. **`grep -nE "user_id\s*=\s*1\b" tests/test_trading.py`** returns zero hits in the 4 target classes (or only hits in API-layer classes / docstrings / commented-out lines).
5. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-07_f-thread-housekeeping-tail-2026-05-07.md` per PROTOCOL format. Include:
   - Per-class hit count fixed.
   - Whether any test failed on a real (non-fixture) bug — surface those, don't auto-fix.
   - Final `git ls-files docs/STRATEGY/COWORK_REVIEWS/` count.
   - Magic-number audit (zero new literals).

## Rollback plan

- **Step 1 (test sweep):** `git revert <commit>` — tests go back to FK-violation state. No production impact.
- **Step 2 (review backlog):** `git revert <commit>` — review files un-tracked again, working copies preserved. No production impact.

## Verification commands (for the executor + the operator)

```powershell
# Test sweep verification
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_trading.py::TestWatchlistService tests/test_trading.py::TestTradeService tests/test_trading.py::TestJournalService tests/test_trading.py::TestTradeStats -v

# Review backlog tracked
git ls-files docs/STRATEGY/COWORK_REVIEWS/ | wc -l

# Anti-pattern resolved in service-layer classes
grep -nE "user_id\s*=\s*1\b" tests/test_trading.py | grep -E "^[0-9]+:" | awk -F: '{if ($1 < 320) print}'
# Expected: zero hits below line 320 (the API-layer boundary).
```

## Open questions for Cowork (surface in CC report)

1. **API-layer classes** (line 345+). If any of these classes also have hardcoded `user_id` patterns under a different shape (e.g., `paired_client.user.id` vs `1`), surface them. Same fix shape; could ship as a tiny follow-up.
2. **`docs/STRATEGY/COWORK_REVIEWS/*` content audit.** Skim each review for any half-written draft or sensitive content that shouldn't go into git history. If anything looks off, surface to operator before committing.

## Forward pointer

After this lands, the strategy thread is fully closed for the third and last time. No actionable follow-ups remaining; only the working-tree disk-view sync (operator-side mount refresh) and that's not a thing CC executes.
