# CC_REPORT: f-phase-e-revert-and-bracket-writer-crash-fix

## Outcome

Two emergency fixes:

1. **Phase E reverted** (`git revert c8aec21`). All Phase E source
   removed; migration 234 (additive `crypto_broker_zero_qty_streak`
   column) intentionally retained for forward-compatibility. The
   ORM column is also retained.
2. **`place_missing_stop` IndexError crash loop fixed** (ADA/SOL).
   Two layered defences:
   * Crypto-path refusal: ALL `-USD` tickers are SKIPPED with
     `reason='venue_unsupported_crypto_path'` BEFORE any broker
     call. The Robinhood equity primitive
     `rh.orders.order(symbol='ADA', ...)` crashes inside
     `get_instruments_by_symbols('ADA')[0]` because Robinhood crypto
     bases have no equity instrument record. Listed-vs-unlisted is
     irrelevant; the equity API is wrong for ALL crypto.
   * Exception cooldown: any exception inside the
     `try/except adapter.place_stop_loss_sell_order` block arms a
     5-min cooldown (settings-tunable via
     `CHILI_BRACKET_WRITER_EXCEPTION_COOLDOWN_SECS`). Subsequent
     sweeps within the window SKIP early with
     `reason='in_exception_cooldown'` instead of re-firing every
     60s.

Both layers ship together so even if a future code path bypasses the
crypto-refuse, the cooldown bounds the blast radius to one crash
per 5 minutes.

## Per-step status

### Step 1 — Revert Phase E (commit `1497c1e`) — SHIPPED
* `git revert c8aec21 --no-edit` — 7 files changed, 596 deletions.
  Removes `run_crypto_stale_trade_close`, the test file, the
  scheduler wiring, and Phase E settings.
* Manually retained:
  * `_migration_234_crypto_broker_zero_qty_streak` in
    `app/migrations.py` (function + registry entry). Docstring
    rewritten to explain the post-revert retention.
  * `Trade.crypto_broker_zero_qty_streak` ORM column.

  Both are purely additive (default 0) and reverting them would
  desync the production `schema_version` registry. Future
  structurally-correct work can reuse the column without a fresh
  migration ID.
* Phase A's `_RECONCILE_ARTIFACT_EXIT_REASONS` is back to its
  Phase-A-only shape (the two crypto exit reasons removed by the
  revert). Acceptable because no producer of those reasons survives
  in code.
* Acceptance criterion 5: `grep -rE
  "run_crypto_stale_trade_close" app/ tests/` returns zero source
  matches (only stale `.pyc` files). Pinned as a test
  (`test_phase_e_source_removed`) so a re-introduction flips red.

### Step 2 — IndexError root cause analysis — COMPLETE
The audit fingerprint (ADA/SOL crash every 60s) traces to:

* `bracket_writer_g2.place_missing_stop` ⟶
* `adapter.place_stop_loss_sell_order` (the
  `RobinhoodSpotAdapter`) ⟶
* `broker_service.place_sell_stop_loss_order` ⟶
* `rh.orders.order(symbol=ticker, stopPrice=...)` ⟶
* (inside `robin_stocks`) `get_instruments_by_symbols('ADA')[0]`.

Robinhood's equity instruments endpoint returns `[]` for crypto
bases, so the `[0]` indexing crashes with
`IndexError: list index out of range`. The existing prefilter at
line 976+ only filtered UNSUPPORTED crypto bases (off the
`ROBINHOOD_SUPPORTED_CRYPTO_BASES` whitelist); ADA and SOL are ON
the whitelist (Robinhood does TRADE them) but the equity API is the
wrong primitive regardless.

### Step 3 — Patch + cooldown — SHIPPED
`bracket_writer_g2.py` splice (+71 lines, AST clean):

* New module-level helpers `_arm_exception_cooldown` /
  `_is_in_exception_cooldown` / `_exception_cooldown_secs` parallel
  to the existing FIX 52 reject-cooldown infrastructure. State is
  in-process (`_intent_exception_cooldown: dict[int, float]`).
  Cooldown duration reads at call-time so env overrides take effect
  on next sweep without a restart.
* Crypto prefilter at lines 975+ extended: refuse ALL `-USD`
  tickers, not just unsupported bases. Explanatory comment cites the
  ADA/SOL audit + the equity-vs-crypto primitive mismatch.
* New cooldown gate inserted BEFORE the FIX 52 reject-cooldown gate
  (so a code-side crash short-circuits without re-evaluating prior
  reject state).
* `except Exception` block now calls `_arm_exception_cooldown` AND
  threads the cooldown duration into the audit event's `extra` JSON
  + the structured warning so ops can grep the engagement.
* New setting `chili_bracket_writer_exception_cooldown_secs`
  default 300, env-overridable via
  `CHILI_BRACKET_WRITER_EXCEPTION_COOLDOWN_SECS`.

### Step 4 — Tests — SHIPPED (9 tests)
`tests/test_bracket_writer_place_missing_stop_resilience.py`:

**Crypto-path refusal (4 tests):**

1. `test_ada_usd_crypto_ticker_skipped_without_broker_call` — ADA
   skipped with `venue_unsupported_crypto_path`; adapter NEVER
   called.
2. `test_sol_usd_crypto_ticker_skipped_same_path` — SOL same path.
3. `test_zec_usd_unlisted_crypto_also_skipped` — ZEC (the
   originally-known unlisted-crypto case from the May 4 audit) is
   still skipped under the broader new prefilter.
4. `test_equity_ticker_does_not_hit_crypto_skip` — AAPL passes the
   prefilter (suffix-scoped, not blanket).

**Exception cooldown (4 tests):**

5. `test_exception_arms_cooldown_and_returns_clean_reason` —
   adapter raises IndexError → returns `place_failed` + cooldown
   armed.
6. `test_subsequent_call_during_cooldown_skips_without_broker_call`
   — second call within window returns `in_exception_cooldown`
   without touching adapter.
7. `test_exception_cooldown_expires_cleanly` — past-due `until`
   drops the dict entry.
8. `test_exception_cooldown_secs_reads_settings` — env override
   applies.

**Phase E removal (1 test):**

9. `test_phase_e_source_removed` — `crypto_reconcile` module is
   gone AND
   `bracket_reconciliation_service.run_crypto_stale_trade_close` is
   gone.

### Step 5 — CC report + commit + NEXT_TASK DONE — IN PROGRESS

## Surprises / deviations

1. **Phase A's frozenset reverted.** `git revert c8aec21` removed
   the two crypto exit reasons added to
   `_RECONCILE_ARTIFACT_EXIT_REASONS` in `pdt_guard.py`. Acceptable
   because no surviving code path emits those reasons — the
   producer (Phase E sweep) is gone. If a future structurally-
   correct crypto-reconcile reintroduces those reasons, it must
   also re-extend the frozenset.
2. **Migration 234 retention required manual re-add.** The revert
   wiped the migration function + registry entry. Per brief
   ("Migration 234 ... should remain"), I re-added them as a
   follow-up edit so deployed DBs that already applied this
   migration keep a consistent `schema_version` row. Documented
   the post-revert retention reason in the migration's docstring.
3. **Crypto refuse is broader than the brief specified.** Brief
   suggested patching the IndexError site downstream
   (`cost_bases[0]` / `executions[0]`). I went broader at the
   prefilter because the IndexError is inside `robin_stocks`'
   third-party code (`get_instruments_by_symbols('ADA')[0]`) which
   we don't own. Patching the call site (refusing all crypto from
   this entry) is the surgical fix; downstream guard-coding
   third-party SDK internals is more brittle.

## Open questions (carried from brief)

1. **The IndexError site itself.** Now sealed off at the prefilter.
   Future crypto stop-loss work needs the actual crypto primitive
   (`rh.crypto.order_*`), out of scope here.
2. **Auth-cache liveness** (Anomaly 1 in the architectural
   rebuild) — separate brief.
3. **Crypto exit_monitor deterministic close** (Anomaly 4) —
   separate brief, mitigated for now.
4. **Missing bracket_intents on 10 crypto trades** — separate Phase
   3 of the rebuild.

## Verification

* Revert commit `1497c1e` shipped; mig 234 + ORM column retained
  via follow-up.
* `wc -l bracket_writer_g2.py` 1444 → 1515 (+71); AST clean.
* `_exception_cooldown_secs()` returns 300 by default;
  env-tunable.
* 9/9 tests PASS.
* `grep -rE "run_crypto_stale_trade_close" app/` returns zero
  source matches.
* Splice pattern used for `bracket_writer_g2.py`. Edit tool used
  for the small additions in `migrations.py`, `config.py`,
  `models/trading.py` (each well under the 100-line splice
  threshold for the surface being touched).

## Operator-side after CC ships

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker`.
3. **Watch ADA's bracket_writer activity for 10 min.** Expected
   pattern: one `g2_place_missing_stop_submitting`-equivalent log
   line, then either:
   * a `[bracket_writer_g2] place_missing_stop SKIPPED ... reason=
     venue_unsupported_crypto_path` warning — the new prefilter
     working (preferred path); OR
   * a clean broker-error rejection with a meaningful string (NOT
     "list index out of range") + a cooldown-armed log line. The
     next sweep within 5 min should SKIP with
     `reason=in_exception_cooldown`.
4. Optionally remove Phase E disable flags from `.env`. The code
   is gone; the flags are no-ops.
5. Confirm trade 1810 status hasn't reverted (the Phase E false-
   cancel was already restored by the operator within 22 min of
   the incident; the revert can't re-cancel it).

## Rollback plan

`git revert` of THIS commit re-introduces both Phase E (bad) AND
re-introduces the IndexError crash loop (also bad). Don't roll
back; if a regression surfaces, surgically patch forward.

If a specific test or behaviour needs a knob:
* `CHILI_BRACKET_WRITER_EXCEPTION_COOLDOWN_SECS=0` disables the
  exception cooldown without code revert. The crypto-prefilter
  refusal is unconditional — to bypass for testing, comment out the
  4-line guard in a hotfix branch.

## What's NEXT after this ships

* Per the architectural rebuild brief
  (`f-crypto-reconcile-architectural-rebuild`): Phase 1 (auth
  liveness + typed result) is the next major piece, scheduled for
  fresh-start tomorrow.
* The wipeout-cascade chain summary table from the Phase E CC
  report (now reverted) needs revising — Phase E is no longer in
  the chain. Phases A/B/C/D remain in place; the crypto-side
  stays uncovered until the rebuild ships.
* Phase A's frozenset extension for the two crypto reasons can be
  re-added when the rebuild's correct producer ships.
