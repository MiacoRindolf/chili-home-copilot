# PATH B wired — a partial exit under a resting full-quantity deadman stop

DATE: 2026-09-06
BRANCH: `feat/path-b-partial-wiring` (from `origin/main` @ `9383324b2`)
COMMITS: `dfb7e999f` marker · `1423182bf` replay mock PATCH · `b906419a6` broker-agnostic
gate · `4c84afa1b` the service step and its three call sites

## Why this was done

The operator's instruction, verbatim: *"mali yan… dapat wala nang RH… dapat lahat nang kaya
sa RH is dapat nagagawa sa alpaca… wala dapat faithfulness and strat sa broker… ayusin
mo"* — no per-broker strategy; anything Robinhood can do must be doable on Alpaca. Then:
*"ikabit mo na yung Path B."*

The measurement that made it concrete (clean gate-15 baseline, 69 symbol-days replayed in
both execution families, `scratchpad/broker_shape_cost.py`):

| | Robinhood | Alpaca |
|---|---:|---:|
| partial fills taken | 341 | 2 |
| `alpaca_scale_out_suppressed_for_deadman` | — | 228 |
| `tranche_oco_skipped_extended_hours` | — | 223 |

`live_partial_exit_filled` has been ZERO on the live Alpaca lane since 2026-08-01. The
eight worst names cost about $1,380 against the same strategy with partials available.

The cause was not strategy. It was one term in the SCALING_OUT decision:

```
scaling = bool(can_split and not pos.get("partial_taken")
    and normalize_execution_family(sess.execution_family) not in ALPACA_EXECUTION_FAMILIES)
```

The same first-target touch sold a tranche on Robinhood and flattened the WHOLE position
on Alpaca. `paper_execution.py` calls that split a *parity contract* shared with the paper
runner, so a family gate on it was a per-broker strategy by definition.

## What actually differs between the venues

One thing, and it is real: a resting stop for the whole position consumes Alpaca's
`qty_available`, so the tranche sell is refused by the venue. That is a FEASIBILITY
question, not a strategy question, and it is the question the code asks now
(`alpaca_partial_tranche_sellable`):

| answer | tranche | meaning |
|---|---|---|
| `tranche_already_reserved` | sell | an OCO or a PATH B limit already carved f out |
| `no_resting_deadman` | sell | nothing holds the shares |
| `deadman_leaves_tranche_free` | sell | the stop rests for ≤ Q − f |
| `deadman_holds_tranche` | PATH B | the full-qty stop consumes it |
| `quantities_unreadable` | whole | fail closed |

Every refusal now emits `alpaca_partial_tranche_unsellable` with the three quantities, so
the suppression is never silent again.

## PATH B

Shrink the resting stop from Q to R = Q − f with one qty PATCH, then sell f.
`replace_order_qty` (#1276) has had a working live probe (200–254 ms) and ZERO production
callers since 2026-09-01, because two adversarial reviews found five holes in wiring it
without a durable marker — two of which kill the position or every exit path.

Between the PATCH and the sell the f shares carry no stop. That window IS the design
problem, so every step is durable on the action claim BEFORE the broker is touched, and
every phase advance is a compare-and-swap through `path_b_partial.advance_phase`, the only
sanctioned writer of the phase graph.

### The three call sites, and why each is placed where it is

1. **The first-target decision.** `deadman_holds_tranche` is the one unsellable reason
   PATH B removes, so the decision now freezes the edge instead of taking the whole
   position at target. Taking the whole position is the trade this program exists to stop
   making.
2. **Above the handoff-priority block.** Every exit inside that block returns from the tick
   first, so a marker serviced below it could never advance while a close handoff existed:
   the phase would never reach `replace_stuck`, the containment would never be queued, and
   the deadlock would be permanent.
3. **The head of the exit chokepoint.** A tranche PATH B posted is durable on the CLAIM,
   and `_cancel_scale_limit_and_clamp` reads the session JSON. Without the rebuild the
   clamp is a pass-through no-op and the account carries Q + f of sell authority against Q
   shares — a short flip through the very chokepoint that exists to prevent one.

## Two defects found while wiring, both fixed here

- **`_ensure_alpaca_deadman_stop` would have flattened the runner.** Its head guard
  full-closed on ANY non-OCO resting limit
  (`alpaca_legacy_scale_order_conflicts_with_deadman`) — which is exactly what PATH B
  leaves behind. The runner would have been flattened moments after PATH B posted the
  tranche it exists to post. A PATH B tranche splits by the same arithmetic; the honest
  difference, now stated in the code, is that it carries no stop of its own.
- **A crash between an accepted PATCH and the phase write was unrecoverable.** The marker
  stayed at `intent_frozen`, where a re-submit is refused as a duplicate client order id
  and would then be mis-read as a rejection — abandoning a lineage the broker had granted.
  `intent_frozen` now adopts a `replaced` / `pending_replace` predecessor as
  `recovered_from_broker`.

R1 (the `pending_replace` transient) is handled by reporting the position as PROTECTED —
both orders rest at the broker and the predecessor is still live — rather than by widening
the certifiably-active lifecycle set. An indeterminate PATCH is never read as a refusal:
only a 4xx that is not a timeout is a refusal. The 300 s ceiling forces `flatten_queued`
for any naked phase and `abandoned` only for pre-certification lineage, so no marker can
wedge a position.

## Tests

| suite | result |
|---|---|
| `test_path_b_service_step.py` | 11 passed (4 DB-backed, driven against the replay mock) |
| `test_partial_exit_path_b_wired.py` | 8 passed (the tripwire, inverted) |
| `test_broker_agnostic_partial_gate.py` | 7 passed |
| `test_path_b_marker_claim.py` | 7 passed |
| `test_replay_mock_replace_order_qty.py` | 4 passed |
| `test_partial_exit_path_b_lineage_seam.py` | 12 passed (4 stale asserts updated) |
| deadman + OCO + exit-call-site + replay-v3 parity | 204 passed |

`test_rth_entry_rejects_stale_premarket_extended_hours_generation_before_place` fails
identically with these changes stashed. Pre-existing, entry path, not touched here.

The old tripwire `test_partial_exit_path_b_unwired.py` asserted that
`replace_order_qty` had zero production callers, and its own docstring said to replace it
with call-site guards in the PR that wired PATH B. That is what happened: the AST scanner
is kept verbatim (it is the only thing in the repo that can see a caller appear anywhere in
`app/`) and now asserts exactly ONE caller, inside the service step.

Four assertions in `test_partial_exit_path_b_lineage_seam.py` named the pre-#1292 state and
have been red at HEAD since #1292 landed. They are updated to the current truth.

## What is NOT done

- **The restore edge (§3.8) and the flatten remedy are not implemented.** The service step
  reaches those phases, reports them, and the 300 s ceiling still applies to every one of
  them, but no step drives `restore_intent_frozen` → `restore_certified` yet. A growing
  replacement also cannot be lineage-certified today:
  `_alpaca_replacement_successor_envelope` refuses any successor larger than its
  predecessor, and the restore edge is always larger.
- **No bench A/B yet.** This changes exit behaviour at the first target, so it needs the
  same gate as every other lever: winners captured up, losers not up, interleaved, dense
  tape. The Alpaca arms were still running when this was written.

## Next

1. A/B this against the clean gate-15 baseline on winners AND losers before it ships.
2. The restore edge, then the sibling-fill accounting (§3.7).
