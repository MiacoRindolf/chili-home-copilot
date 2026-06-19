# CC_REPORT: momentum-pyramid-wiring

## What shipped

The live_runner add-lifecycle for the RISK-NEUTRAL confirmation-pyramid (a single,
risk-neutral add to an already-winning momentum runner). Wired on top of the existing
safety core (commit 42b7e46): GUARD #1 (`risk_anchor_usd` clamp on the #769 circuit) +
the 4 default-OFF config knobs + the invariant gate test.

Files touched (4):
- `app/services/trading/momentum_neural/paper_execution.py` — two NEW pure helpers
  (`pyramid_add_decision`, `pyramid_blend_on_fill`) — the ONE source of truth shared by
  live + replay + tests.
- `app/services/trading/momentum_neural/live_runner.py` — the add-decision block in the
  TRAILING branch + the C1b clamp call-site wiring.
- `app/services/trading/momentum_neural/replay_v2.py` — the faithful A/B mirror + the
  GUARD-#1 loss-side clamp on the breach.
- `tests/test_momentum_pyramid.py` — tests B–E + the positive full add-lifecycle test.

No migrations.

## Verification

- `tests/test_momentum_pyramid.py`: **39 passed** (16 original invariant-gate + 23 new
  B–E + happy-path/idempotency end-to-end).
- AST: all 4 edited files `py_compile` + `ast.parse` clean; pytest `--collect-only`
  resolves all imports (39 collected).
- Flag-OFF byte-identical PROVEN end-to-end: `test_B_parity_off_full_tick...` drives a
  real `tick_live_session` on a held TRAILING winner with the flag OFF → no add, no pos
  delta, no `pyramid_*` keys, and the C1b circuit was called with `risk_anchor_usd=None`.
- The 8 failures in `tests/test_momentum_live_runner.py` are PRE-EXISTING (proven by
  restoring the pristine HEAD `live_runner.py` and reproducing the identical
  `skipped: venue_broker_not_connected` — no live Coinbase creds in this env). My new
  tests patch `_venue_broker_connected` so they actually reach the add path.
- `tests/test_ofi_exhaustion_lock.py`: 44 passed (shared `paper_execution.py` untouched
  in behavior).

## Key change sites

- C1b clamp: `live_runner.py:4217` — `max_loss_circuit_decision(..., risk_anchor_usd=le.get("pyramid_risk_anchor_usd"))`.
- Add-decision block: `live_runner.py:4760` (inside `if st == STATE_LIVE_TRAILING:`,
  AFTER the cushion-trail/OFI-lock/v2-ladder ratchet, BEFORE the stop-breach block;
  whole block gated on `chili_momentum_pyramid_enabled`, fully fall-through so the exit
  always runs the same tick).
- Pure helpers: `paper_execution.py:405` (`pyramid_add_decision`), `:496` (`pyramid_blend_on_fill`).
- Replay mirror: `replay_v2.py:768` (add) + `:849` (GUARD-#1 loss-side clamp).

## Safety properties (how the exit can never be blocked/delayed/loosened)

1. The add block sits AFTER the trail ratchet and BEFORE the stop-breach, and is fully
   fall-through (no early return) — so the freshly-ratcheted stop-breach always runs the
   same tick.
2. `pos` is mutated ONLY on a CONFIRMED poll fill (PHASE 1), never on submit.
3. INVARIANT-A: `pyramid_blend_on_fill` ratchets `stop = max(stop, a1)` and ASSERTS
   `s1 >= stop_px` — the stop can only tighten.
4. GUARD #4: the add is refused (NEVER the exit) whenever a new entry would be — routed
   through the same `runner_boundary_risk_ok` → `evaluate_proposed_momentum_automation`
   admission (kill-switch, per-broker + global daily-loss registry, governance inhibit,
   position cap, aggregate crypto risk). On refusal: emit `live_pyramid_add_blocked`, skip.
5. Idempotency: `pyramid_order_id` (in-flight) + `pyramid_add_count` cap block re-submits;
   a partial add blends ONLY the filled qty.
6. `original_quantity` GROWS on the add (so `scale_out_quantity` de-risks the enlarged
   position; the `can_split` dust guard re-runs against the new size at scale time).
   `entry_sizing["stop_distance"]` (d0) is NOT overwritten — it stays the C1b basis.

## Surprises / deviations

- **Equity-first → crypto deferred**: the add fires for EQUITY only. Crypto (`-USD`) is
  deferred because its L2/OFI ring is only partially populated in the scheduler process
  (`_live_ofi_microprice` returns None for many crypto), so the OFI confirmation can't be
  trusted to fire an extra BUY. Documented inline; revisit when crypto L2 coverage lands.
- **Refactored to two pure helpers** (vs. a single inline block) so the live path and the
  replay A/B share ONE predicate + blend — matches the codebase convention
  (`max_loss_circuit_decision`, `ofi_exhaustion_lock`, `scale_out_quantity` are all pure)
  and makes B–E deterministic without brittle full-tick mocking.
- The 8 pre-existing `test_momentum_live_runner.py` failures (broker-not-connected) are an
  env condition, not a regression — proven against pristine HEAD.

## Deferred

- Crypto pyramid (needs full crypto L2/OFI coverage first).
- The replay A/B run itself (operator runs it).

## Open questions for Cowork

- None blocking. The flag stays DEFAULT-OFF (`chili_momentum_pyramid_enabled=False`) per
  the safety-core design; flip after the replay A/B + paper soak prove net-positive.
