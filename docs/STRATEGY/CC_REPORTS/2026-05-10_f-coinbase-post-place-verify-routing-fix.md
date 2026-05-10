# CC_REPORT: f-coinbase-post-place-verify-routing-fix

Brief: `docs/STRATEGY/QUEUED/f-coinbase-post-place-verify-routing-fix.md`
Session: `coinbase-post-place-verify-routing-fix-2026-05-10`
Plan-gate: APPROVED (autonomous Cowork, operator pre-authorization
cited) — `scripts/_claude_session_consult/coinbase-post-place-verify-routing-fix-2026-05-10/plan.response.md`.

## What shipped

Three checkpoint commits on `main`:

| Commit | Summary |
|---|---|
| `21ce9ee` | wip(brain): coinbase get_order_status normalizes state vocabulary |
| `7def71b` | feat(brain): venue-route post-place verify + Coinbase orphan recovery |
| `c8a3ff3` | test(brain): coinbase post-place verify coverage + orphan recovery |

Net: 2 production files + 1 new test file. **288 insertions + 530 new
lines of tests**. No migrations.

### `app/services/trading/venue/coinbase_spot.py` (+92 lines)

New `CoinbaseSpotAdapter.get_order_status(order_id) -> dict` method
right after the existing `get_order` (line 519). Returns
`{"ok": True, "state": "<normalized>", "raw": {...}}` on success and
`{"ok": False, "error": "<reason>", "state": None}` on
adapter-disabled / 404 / rate-limit / transport error. Normalizes
Coinbase Advanced Trade order statuses (PENDING / OPEN / FILLED /
CANCELLED / EXPIRED / FAILED / REJECTED, plus US `CANCELED` spelling)
into the Robinhood-compatible verify vocabulary so the writer's
verdict logic does not have to fork by venue. **No magic-fallback
values** — adapter never fabricates a `confirmed`/`resting` state
when the broker call fails.

### `app/services/trading/bracket_writer_g2.py` (+196 / -2 lines)

Three additive changes inside `place_missing_stop`:

1. **`_verify_via_coinbase` helper** — module-level mirror of
   `broker_service.verify_order_landed`. Polls
   `adapter.get_order_status` on the same 6 × 0.5s cadence and maps
   states to the `(verdict, observed_state)` tuple contract. Resting
   states: `{confirmed, queued, partially_filled, filled}`. Rejected
   states: `{rejected, cancelled, failed}`. Timeout → `("unknown",
   last_observed_or_None)`.

2. **Venue routing at the post-place verify call site** — the former
   single-line call to `broker_service.verify_order_landed` is now a
   branch on `_bs_lower`: Coinbase goes through `_verify_via_coinbase`,
   Robinhood falls through to the original
   `broker_service.verify_order_landed` (byte-identical to the prior
   behaviour for RH).

3. **`_try_adopt_unverified_coinbase_order` pre-place hook** —
   Coinbase only. Looks up the most recent
   `g2_place_missing_stop_unverified` event row for the intent in
   `trading_execution_events` (24-hour lookback). Extracts the
   recorded `new_stop_order_id` and asks Coinbase if it's still
   resting via `get_order_status`. If yes: transitions the intent to
   `CONFIRMED_AT_BROKER`, emits a
   `g2_place_missing_stop_orphan_recovered` event, and returns
   `WriterAction(ok=True, reason="orphan_recovered",
   new_stop_order_id=prev_oid)` — skipping the duplicate place. If no
   prior unverified event / lookup fails / order is no longer
   resting: falls through to normal placement. Best-effort failure
   semantics throughout (never blocks a fresh attempt on transient
   errors).

This recovers the 4 stranded Coinbase orders (b3c14ef6 AERGO-USD,
545eeffe 1INCH-USD, d1b91a9c ACX-USD, b13e8058 RARE-USD) on the next
sweep without placing duplicate stops at Coinbase.

### `tests/test_coinbase_post_place_verify.py` (NEW, 530 lines)

22 tests organized into 9 sections covering the design points from
the plan: venue routing for both broker_sources, state-vocabulary
mapping, transport-error → unverified, orphan-recovery adopt/fallthrough,
and helper-level units for `_verify_via_coinbase` and
`get_order_status`. All 22 pass.

## Verification

### Truncation scan (post-commit)

```
=== bracket_writer_g2.py ===
1797 lines on disk, 1797 lines in HEAD       — match
=== coinbase_spot.py ===
1450 lines on disk, 1450 lines in HEAD       — match
=== test file ===
530 lines
=== AST ===
ALL AST OK
=== git diff stat (HEAD~3..HEAD-1, excluding test commit) ===
 app/services/trading/bracket_writer_g2.py   | 198 +++++++++++++++++++++-
 app/services/trading/venue/coinbase_spot.py |  92 +++++++++++++
 2 files changed, 288 insertions(+), 2 deletions(-)
```

### Test results

```
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_coinbase_post_place_verify.py -v -p no:asyncio

============================= 22 passed in 1.23s ==============================
```

**Pre-existing pytest-asyncio quirk**: `pytest-asyncio 0.23.3` is
incompatible with `pytest 9.0.2` at collection time (`AttributeError:
'Package' object has no attribute 'obj'`) for every test file in this
repo, not just the new one. Workaround: `-p no:asyncio`. This is
outside the scope of this brief; flagging for future cleanup.

Related-test regression run (`tests/test_bracket_writer_venue_routing.py`,
`tests/test_coinbase_tick_size_precision.py`,
`tests/test_coinbase_stop_primitive.py`,
`tests/test_coinbase_bracket_coverage.py`,
`tests/test_bracket_writer_g2.py`) launched in parallel — result
posted to PR-thread when complete; no behavioural change expected
since RH path is byte-identical and Coinbase place-side primitives
are untouched.

## Surprises / deviations

1. **Pytest-asyncio incompatibility** (pre-existing, not introduced
   here). Worked around with `-p no:asyncio`. Worth a follow-up
   task: pin `pytest-asyncio>=0.24` or `pytest<9.0`.

2. **Orphan-recovery placement decision**. The plan proposed two
   sites for the hook; chose to insert it AFTER `adapter =
   adapter_factory(broker_source)` and AFTER the cooldown gates
   but BEFORE the FIX 55 covered-by-existing-sell pre-flight. This
   honours all existing safety belts (cooldowns) AND avoids
   running the `held_for_sells` lookup when we're about to adopt
   an existing stop. No behavioural difference vs the plan's
   approximate placement.

3. **Coinbase 404 normalization**: I added a check for both
   `"not found"` substrings and `"404"` in the exception message
   text, since the Coinbase SDK's exact error format depends on
   the underlying HTTP layer (urllib3 vs httpx). Defensive; doesn't
   change the brief's contract.

## Deferred

- **Pytest-asyncio fix** (see Surprises §1). Not in scope; needs a
  separate brief.
- **Live recovery verification of the 4 stranded orders.** Cannot be
  smoke-tested from CC without a live deploy. The orphan-recovery
  path will run on the very next broker-sync sweep after deploy. If
  it fails (e.g. the orders are no longer at Coinbase because the
  positions exited), the writer falls through to normal placement
  — no regression vs the pre-fix behaviour.

## ⚠ DEPLOY BLOCKER (carried forward from plan-gate response)

**Do NOT deploy this fix yet.** Two production files remain truncated
on disk from the 2026-05-10 19:42Z bracket-coverage-fix-v2 session:

- `app/services/trading/stop_engine.py` — 1302 lines (HEAD: 1316).
  AST fails at line 1299: `logger.info(` never closed.
- `app/services/trading/bracket_reconciliation_service.py` — 2276
  lines (HEAD: 2577). AST fails at line 2270: `{` never closed.

This task does not touch either file, so the in-session work
proceeded normally. But:

> `docker compose up -d --force-recreate <workers>` will crash chili,
> autotrader-worker, scheduler-worker, brain-worker, and
> broker-sync-worker on import, causing a total exit-monitor outage
> on 9 NAKED Coinbase positions (~$2,700 exposure).

**Operator must restore both files** (`git checkout HEAD -- <path>` or
manual edit) and confirm via `wc -l` + `python -c "import ast;
ast.parse(open(F).read())"` **before** running the deploy command.
Verify with:

```powershell
wc -l app/services/trading/stop_engine.py
wc -l app/services/trading/bracket_reconciliation_service.py
conda run -n chili-env python -c "import ast; ast.parse(open('app/services/trading/stop_engine.py').read()); ast.parse(open('app/services/trading/bracket_reconciliation_service.py').read()); print('OK')"
```

Once those report `1316` / `2577` and `OK`, then:

```powershell
docker compose up -d --force-recreate chili broker-sync-worker autotrader-worker scheduler-worker brain-worker
```

## Open questions for Cowork

1. Worth adding a one-shot operator script that lists open Coinbase
   stop orders and matches them to bracket intents? The orphan
   recovery in this fix handles intents that already produced
   `g2_place_missing_stop_unverified` events, but if there are stops
   at Coinbase from sessions BEFORE g2_event logging existed, those
   would still need manual reconciliation. (Not urgent; the 4 known
   stranded orders are all post-g2_event.)
2. The `chili_bracket_writer_g2_place_missing_stop` flag already
   gates the whole code path including recovery; you opted out of a
   separate flag for orphan recovery. Confirm post-deploy this is
   still the right call after watching the first 1-2 sweeps.

## Files touched

- `app/services/trading/venue/coinbase_spot.py` (1358 → 1450)
- `app/services/trading/bracket_writer_g2.py` (1603 → 1797)
- `tests/test_coinbase_post_place_verify.py` (NEW, 530)

No migrations.

## Hard constraint compliance — final check

| Constraint | Status |
|---|---|
| `bracket_writer_g2.py` + `coinbase_spot.py` only | ✓ (plus new test file) |
| Edit-tool truncation discipline | ✓ — wc -l + AST after every Edit, all clean |
| Phase 6 LIVE soak — additive only | ✓ — RH path byte-identical, no existing logic removed |
| No magic-fallback values | ✓ — adapter never fabricates a state on broker error |
| Plan-gate active | ✓ — APPROVED before any code change |
