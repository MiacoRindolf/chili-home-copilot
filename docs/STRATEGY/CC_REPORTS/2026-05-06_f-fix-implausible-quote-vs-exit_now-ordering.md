# CC_REPORT: f-fix-implausible-quote-vs-exit_now-ordering

## Outcome

Crypto-and-options fix shipped. The Phase 0 audit found options had the same bug shape as crypto (different return-type, same ordering); the brief explicitly authorized expanding to options if the audit found it, so I did. Crypto Case 5 xfail removed. New prefix/contract pin tests for both lanes.

## What shipped

One commit (pending). Files: 4.

- `app/services/trading/crypto/exit_monitor.py` — new `_refused_quote = (...)` gate on the `fresh_monitor_exit_meta` consultation. Match by `reason.startswith("no_trigger:implausible_quote")` because the existing tuple `(should_exit, reason)` carries the refusal in the string. Only the implausible-quote refusal blocks consultation — `no_trigger` and `no_quote` still consult the LLM advisory (those aren't refusals to trust the feed).
- `app/services/trading/options/exit_monitor.py` — `_evaluate_exit_triggers` return type widened from `Optional[str]` to `tuple[Optional[str], bool]`. The bool is `abstained_implausible`. Single-caller refactor; no signature blast radius beyond the lane. Call site gates `fresh_monitor_exit_meta` consultation on `not abstained_implausible`.
- `tests/test_crypto_exit_monitor_pattern_exit_now.py` — Case 5 xfail removed; assertion now passes. Added `test_evaluate_exit_triggers_implausible_quote_prefix` (helper-level, sub-millisecond) pinning the prefix-match contract Phase 1 relies on. Added `test_case5b_no_trigger_plus_fresh_exit_now_still_closes` regression — proves the new gate doesn't extend to ordinary `no_trigger` (Case 1 + Case 5b together cover both sides of the discriminator).
- `tests/test_options_exit_monitor_pattern_exit_now.py` — added 3 tests: `test_evaluate_exit_triggers_implausible_quote_returns_abstained_true`, `test_evaluate_exit_triggers_normal_path_returns_abstained_false`, `test_options_call_site_gates_monitor_on_abstained_implausible`. Updated `test_case4_native_dte_trigger_wins` source-text guard to accept either the original `if not reason:` form OR the new `if not reason and not abstained_implausible:` form.

## Per-phase status

### Phase 0 — Lane audit — SHIPPED
- **Equity (`auto_trader_monitor.py:337-410`)**: confirmed no implausible-quote guard. Quote fetched via `adapter.get_quote_price`; `if not px or px <= 0: continue` skips on no-quote; otherwise raw price compare. A bogus quote like `$0.0003` for an equity at entry $50 would directly trigger `hit_stop=True` and force-sell at the bad price — that's a different vulnerability (NO guard at all) and out of scope per brief. Worth a separate brief `f-equity-lane-implausible-quote-guard` later.
- **Options (`options/exit_monitor.py:161-206`)**: confirmed implausible-quote guard EXISTS (Round-15, 2026-04-30) but returned `None` for both "no trigger fired" and "abstained implausible." Call site at line 329 receives `None` and falls through to `fresh_monitor_exit_meta(...)` — same ordering bug as crypto, just hidden inside a different return shape. **Brief Phase 0 said: "if either equity or options actually does have the same shape, expand Phase 1's fix to cover them too."** I did.

### Phase 1 — The fix — SHIPPED (BOTH LANES)
- **Crypto** (~5 lines): `_refused_quote` boolean computed from prefix match, gates the monitor consultation. Inline comment explains the no-hardcoded-fallback rationale.
- **Options** (~3 lines per call site + return-type widen): `_evaluate_exit_triggers` now returns `(reason, abstained_implausible)`. The call site gates `fresh_monitor_exit_meta` on `not abstained_implausible`. Single caller, type-safe, explicit.

The brief Out-of-Scope mentioned "Refactoring `_evaluate_exit_triggers` to return a structured refusal type instead of a string prefix. Cleaner but blast-radius increases; pick this up only if a future brief needs the same contract in another lane." For options, "the future" arrived in the same brief — but the blast radius is one caller. For crypto, kept the prefix match as the brief specified.

### Phase 2 — Tests — SHIPPED
- Crypto: 8 tests (was 6 + xfail; becomes 5 case + 1 source-guard + 1 contract-prefix + 1 case-5b regression). Case 5 xfail flipped to PASS via marker removal + the fix.
- Options: 11 tests (was 8; added 3 lane-fix-aware tests + tightened test_case4 guard).

### Phase 3 — Verify deployment — DEFERRED to operator-side per brief
The brief Phase 3 is a runtime watch ("Watch `trading_stop_decisions` for the next `DATA_IMPLAUSIBLE` row on TRUMP-USD; verify NO `[crypto_exit] CLOSED` for same trade with same-cycle `pattern_exit_now`"). Listed under "operator-side after CC ships" — not part of this CC's responsibility.

## Verification

- Options test suite: **11/11 PASS in 0.93s** (the 3 new tests + 8 prior tests, with the test_case4 source-text guard updated to match either gate form).
- Crypto test suite: **8/8 PASS in 344.74s** including Case 5 (the bug-fix case — was xfail, now passes naturally), `test_evaluate_exit_triggers_implausible_quote_prefix` (helper-level contract pin), and Case 5b (regression-protection: confirms ordinary `no_trigger` + fresh `exit_now` still closes — the gate's scope is surgical).
- Equity test suite (`tests/test_auto_trader_monitor.py::*monitor_decision*`): unchanged behaviour expected — this brief touched zero equity-lane source. Confirmed by `git diff`.

## Surprises / deviations

1. **Options had the bug too.** The brief author's pre-audit suggested options' `bid<=0 AND mark<=0 → continue` short-circuit was protective, but that only fires when the quote is FULLY missing. When the quote has bid > 0 and mark > 0 but values are implausible (e.g., bid=$0.001, entry=$0.50), `_evaluate_exit_triggers` returns None (refusing), but the call site treats None as "no trigger fired" and falls through to the monitor consultation. Once `pattern_exit_now` is set, the bid > 0 check at line 362 passes and the limit-sell goes through at the bad bid. Identical exposure to crypto, different surface.

2. **Options needed a return-type change.** Crypto's `(bool, str)` carries the refusal in the string — prefix match works. Options' `Optional[str]` collapses "abstained" and "no trigger" into a single `None`. To distinguish them, I had to widen the return type to a 2-tuple. Brief Out-of-Scope warned against this kind of refactor "unless a future brief needs the same contract in another lane" — that's exactly what happened, in this brief, due to the Phase 0 audit finding. Single-caller refactor; tested; clean.

3. **Source-text guard regression.** `test_case4_native_dte_trigger_wins` matched the literal string `"if not reason:"` and broke when I tightened the gate to `"if not reason and not abstained_implausible:"`. Updated the assertion to accept either form. The semantic invariant (native DTE/premium/stop triggers win on tie) is unchanged because `not reason` is still required.

## Open questions for Cowork

1. **Equity-lane vulnerability (still open)**: `auto_trader_monitor.tick_auto_trader_monitor` has no implausible-quote guard at all. A bogus equity quote (e.g., $0.50 for a $50 stock) would force a stop-loss sell at the bad price. Different bug class than this brief's ordering issue; deserves its own `f-equity-lane-implausible-quote-guard` brief. Pre-existing; NOT regressed by this work.

2. **Should the implausibility threshold (0.1x to 10x) live in `_exit_monitor_common.py`?** Today crypto's threshold is hardcoded inside `crypto/exit_monitor.py::_evaluate_exit_triggers` and options' is inside `options/exit_monitor.py::_evaluate_exit_triggers`. If equity grows the same guard (Open Q #1), three lanes would each have the same magic number. Not worth pre-factoring today; flag for the equity-lane brief.

3. **Should the `_refused_quote` prefix-match in crypto be promoted to a shared helper alongside the options' `abstained_implausible` flag?** Today they're parallel implementations. A shared `should_consult_monitor_after_refusal(reason: Optional[str], abstained: bool) -> bool` helper in `_exit_monitor_common.py` would unify them. Brief Open Q #1 says "don't pre-factor; promote when a second lane needs it" — both lanes now need it. Concrete enough to factor in a follow-up.

## Cookbook update

- **The "implausible quote refused" state is fundamentally different from "no trigger fired"**. The first means the lane doesn't trust its own input; the second means it trusts the input and finds nothing actionable. Functions returning a single `Optional[str]` collapse them, which makes correct downstream gating impossible. Future trigger-evaluator functions should return a discriminated shape (tuple, dataclass, or sentinel-prefixed string) when they can refuse on data quality.

## Stale uncommitted work (carry-forward)

Same disposition as prior CC reports — operator-tracked uncommitted scratch (`.commit_msg_*.txt`, `docs/AUDITS/*`, `app/models/trading.py` event listener, `.env.example` flags, `brain_worker.log`, `data/ticker_cache/crypto_top.json`) was untouched by this session.
