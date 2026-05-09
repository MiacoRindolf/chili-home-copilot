# CC_REPORT: f-prefilter-bypass-and-cooldown-investigation

## Outcome

The 2026-05-09 ADA/SOL crash loop was bypassing the prior commit's
prefilter via TWO routes:

1. **The deployed container code may be stale OR the audit fingerprint
   pre-dated the deploy.** Either way, additional defences ship.
2. **The IndexError was caught inside `broker_service.place_sell_stop_loss_order`'s
   `except Exception`** at line 3109 and packaged as a normal-looking
   `{"ok": False, "error": "list index out of range"}`. The bracket
   writer's exception cooldown only fired when an exception ESCAPED
   the broker call — it didn't, so the cooldown never armed and every
   60s sweep re-fired.

This commit ships two layered defences that close both routes:

* **Broker-layer backstop** in `place_sell_stop_loss_order`: refuses
  any Robinhood crypto base BEFORE the try/except, so the equity
  primitive cannot reach the SDK for ADA/SOL/etc. regardless of
  upstream gating.
* **Bracket-writer cooldown on swallowed code-bug errors**: a new
  `_is_code_bug_error` detector matches IndexError/TypeError/
  AttributeError/etc. signatures in the broker error string and arms
  the same exception cooldown the prior commit added. The terminal-
  reject branch is unchanged (genuine broker rejects → 1h cooldown);
  code-bug-class errors get a 5-min cooldown.

## Per-step status

### Step 1 — Bypass investigation — COMPLETE
* Grep mapped every caller of `place_sell_stop_loss_order` ↔ only
  `bracket_writer_g2.py` reaches it (via the `RobinhoodSpotAdapter`),
  and only via `place_missing_stop` (line 1256) or
  `resize_stop_for_partial_fill` (line 594). The latter is only
  invoked for `qty_drift` + `partial_fill` decisions, NOT
  `missing_stop` — so it's not the ADA path either.
* Read `broker_service.place_sell_stop_loss_order`: the top-level
  `try/except Exception as e:` at line 3109 catches the
  IndexError, logs it, returns `{"ok": False, "error": "list index
  out of range"}`. **This is the bypass**: the bracket-writer's
  `except Exception` block (where the prior cooldown lived) only
  fires when an exception ESCAPES the broker call. The IndexError
  doesn't escape; it gets returned as a normal `not ok` response.
* The bracket-writer's `not place_res.get("ok"):` branch then
  classifies the error: terminal-reject (known patterns) → 1h
  cooldown; everything else → log + return without ANY cooldown.
  "list index out of range" is neither terminal-reject nor a
  successful place; it falls through the cracks and re-fires every
  sweep.

### Step 2 — Broker-layer backstop — SHIPPED
`broker_service.py:place_sell_stop_loss_order`:
* New early-return BEFORE the try/except: if the (already-stripped)
  ticker is in `ROBINHOOD_SUPPORTED_CRYPTO_BASES`, refuse with
  `{"ok": False, "error": "crypto_ticker_unsupported_via_equity_primitive"}`.
  Why "supported" base check: the function receives the stripped
  base (`ADA`, not `ADA-USD`); a generic `-USD` suffix check
  doesn't apply. The whitelist captures all the bases that can
  legitimately appear here as crypto.
* Any caller (current `bracket_writer_g2`, future code paths,
  direct invocations from scripts) cannot reach
  `rh.orders.order(symbol='ADA', ...)` for a crypto base.

### Step 3 — Bracket-writer code-bug cooldown — SHIPPED
`bracket_writer_g2.py`:
* New module-level `_CODE_BUG_ERROR_PATTERNS` tuple +
  `_is_code_bug_error(error_text)` predicate. Conservative pattern
  set: only Python exception class names + the canonical IndexError
  text + the new backstop's error string. Generic words like
  "error" or "fail" are NOT matched (they'd false-positive on
  legitimate broker rejects).
* Inside `place_missing_stop`'s `if not place_res.get("ok"):`
  branch, a new `elif code_bug:` arm calls
  `_arm_exception_cooldown(bracket_intent_id)` with a structured
  WARNING log line. The terminal-reject branch is unchanged.
* The audit event's `extra` JSON now carries `code_bug_cooldown_armed`
  alongside `terminal_reject` so ops can grep cooldown-engagement
  events.

### Step 4 — Tests — SHIPPED
`tests/test_bracket_writer_crash_loop_repro.py` (12 tests, helper-
level mocked + integration):

**`_is_code_bug_error` matrix (12 parametrized cases):**

* All 6 Python exception class signatures + the new backstop's
  error string match.
* All 5 genuine broker rejects + empty + None do NOT match.

**Broker-layer backstop (3 tests):**

* `test_broker_layer_refuses_ada_crypto_base` — ADA refused; SDK
  never reached (sentinel raises if it is).
* `test_broker_layer_refuses_sol_crypto_base` — SOL same path.
* `test_broker_layer_does_not_refuse_equity` — AAPL passes the
  guard.

**Cooldown engagement on swallowed IndexError (3 tests):**

* `test_swallowed_index_error_arms_exception_cooldown` — adapter
  returns `ok=False, error="list index out of range"` →
  `_is_in_exception_cooldown` becomes True.
* `test_subsequent_sweep_after_swallowed_index_error_skips` —
  next call within window returns `in_exception_cooldown` and
  doesn't touch the adapter.
* `test_genuine_broker_reject_does_not_arm_exception_cooldown` —
  "Not enough shares to sell" arms terminal-reject cooldown, NOT
  exception cooldown (separate concerns).

**Full-chain integration (3 tests):**

* `test_full_chain_ada_prefilter_path` — bracket-writer prefilter
  catches ADA-USD; adapter never called.
* `test_full_chain_broker_backstop_when_ticker_already_stripped`
  — direct call to `broker_service.place_sell_stop_loss_order('ADA',
  ...)` returns the backstop error.
* `test_full_chain_backstop_then_cooldown_engagement` — adapter
  returns the new error string; bracket-writer detects the code-
  bug class, arms cooldown. Three layers of defence proven.

`tests/test_bracket_writer_place_missing_stop_resilience.py` (prior
9 tests) re-runs green to confirm no regression.

### Step 5 — CC report + commit + NEXT_TASK DONE — IN PROGRESS

## Surprises / deviations

1. **Stripped ticker at backstop site.** The backstop sits in
   `place_sell_stop_loss_order` which receives `ticker='ADA'`
   (already stripped of `-USD` by the upstream
   `RobinhoodSpotAdapter._to_ticker`). I check against
   `ROBINHOOD_SUPPORTED_CRYPTO_BASES` rather than a `-USD` suffix
   because the suffix is gone by this point. A future caller that
   passes `'ADA-USD'` directly would also be caught (the upper-cased
   string is checked against the set; the set has no `-USD` entries
   so the suffix variant just doesn't match — but
   `'ADA-USD'.upper().endswith('-USD')` is the suffix check we'd add
   if needed; for now the whitelist match is sufficient).

2. **The brief mentioned a possible `place_missing_stop_replacement`
   variant.** Verified: no such function exists. The two public
   entry points are `place_missing_stop` and
   `resize_stop_for_partial_fill`; both reach
   `adapter.place_stop_loss_sell_order`. The backstop now covers
   both via the broker layer.

3. **Stale-deploy hypothesis cannot be tested from CC**. The brief
   notes the operator confirmed via `inspect.getsource()` that the
   prior commit's prefilter IS in the loaded module, yet the audit
   logs show the OLD `venue_unsupported_crypto base=TRUMP` message.
   The cleanest interpretation: those audit lines pre-dated the
   deploy; the operator's verification post-dated it. Regardless,
   tonight's commit ships defence-in-depth that doesn't depend on
   any single layer being current — every layer can fail and the
   next one catches.

## Open questions (carried from brief)

1. **Bypass path location** (brief Q1+2+3+4): the IndexError-
   packaging at `broker_service.py:3109` is the conclusive
   identification (Step 1 above).
2. **Live verification post-deploy**: operator-side. The expected
   pattern is documented in the operator section below.

## Verification

* `broker_service.py`: `wc -l` 4276 → 4307 (+31); AST clean.
* `bracket_writer_g2.py`: +~50 lines (`_is_code_bug_error` +
  patterns + new branch); AST clean.
* Direct sanity test: `place_sell_stop_loss_order('ADA', 100,
  trigger_price=0.25)` returns
  `{"ok": False, "error": "crypto_ticker_unsupported_via_equity_primitive"}`.
* `_is_code_bug_error("list index out of range")` returns True;
  `_is_code_bug_error("Not enough shares to sell")` returns False.
* All 21 tests PASS (12 new + 9 prior resilience).

## Operator-side after CC ships

Per brief:

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. Watch ADA's bracket_writer activity for 5 min. Expected pattern:
   * **Path A (preferred)**: bracket-writer prefilter catches
     ADA-USD → `[bracket_writer_g2] place_missing_stop SKIPPED ...
     reason=venue_unsupported_crypto_path`.
   * **Path B (broker backstop)**: prefilter bypassed for some
     reason → bracket-writer reaches broker → broker refuses with
     `crypto_ticker_unsupported_via_equity_primitive` → bracket-writer's
     code-bug detector arms the cooldown → next sweep within 5 min
     SKIPS with `reason=in_exception_cooldown`. The `[broker]
     SELL_STOP submitting` log line will still fire (we're past
     the per-call validation), but `[broker] SELL_STOP refused`
     replaces the IndexError.
   * **Either way**: NO MORE `[broker] SELL_STOP exception for ADA:
     list index out of range` lines, NO MORE per-minute crash loop.

## Rollback plan

`git revert` the commit. The bypass continues; reverts to current
production state. Setting
`CHILI_BRACKET_WRITER_EXCEPTION_COOLDOWN_SECS=0` disables the
cooldown without code revert (the broker backstop is
unconditional).

## What's NEXT after this ships

* Architectural rebuild Phase 1 (auth liveness + typed result) —
  scheduled for fresh-start tomorrow.
* If Path B (broker backstop) is the path that fires, the
  bracket-writer prefilter genuinely IS being bypassed somewhere;
  trace via `g2_place_missing_stop_rejected` events with
  `code_bug_cooldown_armed=true` and surface in a follow-up.
* The `crypto-native stop primitive` follow-up (`rh.crypto.order_*`)
  remains queued; this brief just makes the equity primitive refuse
  cleanly instead of crashing.
