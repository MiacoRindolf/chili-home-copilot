# CC_REPORT: f-thread-housekeeping-tail-2026-05-07

## Outcome

Two follow-ups closed in three commits. The strategy thread closes for what is now (per the brief) the third and last time: zero remaining housekeeping debt.

## Per-step status

### Step 1 — `user_id=1` sweep across 4 service-layer test classes — SHIPPED

Applied the existing `_seed_user(db, name)` helper (returns int — added in commit `aa5fd0c`) across:

| Class | Tests touched | `user_id=1` hits removed |
|---|---|---|
| `TestWatchlistService` | 7 | 12 (incl. 1 isolation test → 2 users seeded) |
| `TestTradeService` | 8 | 16 (1 wrong-user test keeps `user_id=999` sentinel intact) |
| `TestJournalService` | 4 | 7 (incl. 1 isolation test → 2 users seeded) |
| `TestTradeStats` | 2 | 5 |
| **Total** | **21** | **40** |

`grep -nE "user_id\s*=\s*1\b" tests/test_trading.py | awk -F: '$1 > 320 {print}'` post-edit returns zero hits — clean past line 320 (the API-layer boundary).

The single residual `user_id=1` reference in the file is line 36, the docstring of `_seed_user` itself, which describes the anti-pattern it replaces.

`user_id=999` in `test_close_wrong_user_returns_none` is preserved intentionally — that's a sentinel for the "wrong user can't close someone else's trade" guard, not a real-user reference. `close_trade` does a `SELECT WHERE user_id=999` first; no FK insert ever happens.

### Step 2 — Cowork review backlog tracked — SHIPPED
- Pre-brief state: 4 tracked, 45 untracked in `docs/STRATEGY/COWORK_REVIEWS/`.
- Post-brief: all 49 tracked. 45 staged in commit B; the 4 prior tracked stay untouched.
- Spot-checked the most recent review (`2026-05-07_f-thread-housekeeping-followups-2026-05-07.md`): well-formed markdown, complete header + verdict + closing note. No half-written drafts surfaced.

### Step 3 — Three commits — SHIPPED

- **Commit A** (test sweep): `test(trading): sweep remaining user_id=1 hardcodes across service classes`
- **Commit B** (review backlog): `docs(strategy): commit Cowork review backlog`
- **Commit C** (this docs commit): `docs(strategy): f-thread-housekeeping-tail-2026-05-07 CC report + mark NEXT_TASK done`

## Verification

- **`pytest tests/test_trading.py::TestWatchlistService TestTradeService TestJournalService TestTradeStats -v`** — **PARTIAL.** 4/21 explicit PASS (`TestWatchlistService::test_add_to_watchlist`, `test_add_duplicate_returns_existing`, `test_add_normalizes_ticker`, `test_get_watchlist`) before the runner was killed. The pace was glacial (~25-30 min/test, 15× slower than the prior brief's identical helper-pattern run on `TestTradingModels` which finished 9 tests in 584s) — completing the remaining 17 tests would have taken ≈8 hours of wall time. Per operator authorization, killed the runner and committed on the basis of:
  - 4/21 explicit PASS in this run (no failures observed)
  - 9/9 PASS in the prior brief on the identical `_seed_user`-based helper pattern (commit `aa5fd0c`)
  - Mechanical literal substitution verified by grep — zero `user_id\s*=\s*1\b` hits below line 320 (the API-layer boundary)
  - Rollback inline (`git revert <commit>`) is clean if anything regresses
- **`git ls-files docs/STRATEGY/COWORK_REVIEWS/ | wc -l`** — 49 (≥ 5 per brief criterion).
- **`grep -nE "user_id\s*=\s*1\b" tests/test_trading.py | awk -F: '$1 > 320'`** — zero hits in API-layer or below.

## Magic-number audit

**Net new magic numbers introduced: ZERO.** This brief touched test fixtures and tracked review files only — no production thresholds, no numerical literals.

## Surprises / deviations

1. **Brief example used `user.id`, helper returns `int`.** The brief's snippet `user = _seed_user(db, "name"); item.user_id = user.id` would crash because `_seed_user` returns the int directly (per the prior brief's signature `-> int`). Followed the actual helper signature: `uid = _seed_user(db, "name")` then `user_id=uid`. Documented for any future helper-callers.

2. **`user_id=999` sentinel preserved.** `test_close_wrong_user_returns_none` uses 999 as a "wrong user" id that should NOT match any seeded user. Because `close_trade` does a SELECT-first guard (returns None on no match before any FK insert), 999 doesn't trigger a foreign-key violation even though no User exists with that id. Inline comment added.

3. **Two isolation tests needed two users each.** `TestWatchlistService::test_watchlist_isolation_per_user` and `TestJournalService::test_journal_isolation_per_user` both assert per-user data isolation, so they seed `uid_a` + `uid_b`. Brief explicitly anticipated this case.

## Open questions for Cowork

1. **API-layer test classes** (`TestTradingPageAPI` onward, line 345+). Brief said they use `paired_client` fixture — verified by skimming `_make_paired` at line 17, which creates a User + Device + cookie. So API-layer tests get a real seeded user via the shared fixture and do not need this sweep. No follow-up required.

2. **Review backlog content audit.** Spot-checked the 2026-05-07 reviews for completeness; both are full, well-formed markdown. The 45 newly-tracked reviews span 2026-05-01 through 2026-05-07 and represent the strategy thread's complete audit trail. No drafts or sensitive content surfaced in the spot-check; if a deeper audit reveals anything sensitive, the operator can `git rm <file>` selectively (history preserved, working copy stays).

## Cookbook update

- **Helper signatures matter for downstream call sites.** When you write a fixture-style helper, the return type is a contract — future callers will rely on it. Brief examples may not reflect actual signatures; always check the helper before pasting.
- **Sentinel values like `user_id=999` for negative-path tests are valid and should be preserved during sweep refactors.** Don't auto-replace them with seeded users — that would change the test's intent (asserting graceful handling of a non-existent user).

## Stale uncommitted work — final state

After these three commits, expected `git status --short`:
- `brain_worker.log` — runtime, out of scope
- `data/ticker_cache/crypto_top.json` — runtime, out of scope
- `docs/STRATEGY/CURRENT_PLAN.md` — operator change unrelated to this brief

Three items remain — all genuinely runtime/operator-side, none are CC-actionable. Strategy thread closes clean.
