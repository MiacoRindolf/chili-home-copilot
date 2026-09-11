# Persistent tick evidence for structural scope — project 9 [21]/[29]

The current G reader repeatedly classifies a moving fixed-count window. On the
17 reconstructed original TNON G reads, two windows differ from classification
carried across the packet prefix; one actual previous-feature pair changes the
buy/unknown labels of four retained prints totaling 201 shares. Moving halves
also reweight old evidence. A structural replacement must separate those effects
from new information rather than simply choose another N.

`structural_tape_prefix.py` implements the incremental research state needed for
that replacement. It consumes a whole supplied recorded-known frontier and keeps
the classified prefix, exact rational mass sums, indexed extrema, and **all**
active confirmed local peak/valley references. There is no selected parent,
strategy sampling window, clock-gap trim, trading rule, DB adapter, or runtime
caller. The live G/D decisions and the current 255 setting remain unchanged.

## State and membership

Each source tick carries its database/source ID, event/receipt/publication clocks,
source epoch, price/size/attached quote, and available frame sequence/hash. Epochs
are explicit immutable tuples supplied by the adapter. The packet verifier uses
bridge-run, generation, bridge version, and timestamp basis; it does not invent
an epoch from a time gap.

A frontier supplies its known clock, row count, ordered row-content digest, and
previous prefix digest. The reducer validates all rows before committing. A late
or duplicate tick, conflicting receipt, mixed epoch, invalid population, or
resource-capacity failure returns `unresolved` and preserves committed state.
Even a duplicate receipt must present matching rows. A newly discovered row from
an already-applied frontier requires reconstruction; it is not silently inserted.

The caller is responsible for complete frontier membership. These digests do not
prove physical database visibility, a complete provider stream, or independently
fresh quotes. This is a single-owner in-memory component, not a concurrent
transaction service. Catastrophic process/allocation failures are not a durable
checkpoint protocol. Rebuilding from the serialized source frontiers is tested;
incremental durable checkpointing and a production adapter remain open.

Limits bound retained rows, frontier work, and active references. They are
resource capacities, never market thresholds: reaching one rejects the full
frontier. No old reference or tick is dropped to fit a capacity, and no smaller
window is silently substituted. The offline verifier sets capacities from the
entire supplied packet cardinality, not from a fitted trading horizon.

## Classification and structural facts

The quote/tick classifier preserves current float comparison semantics. Quote
classification, price-change/carry fallback, and unknown mass remain separate.
Fallback state persists across source frontiers and is never reset by a path
query or a changing phase origin. A new instance explicitly starts a new
segment; its initial carry is unknown. This is inferred flow, not provider
aggressor truth. Rational accumulation is exact over the supplied numeric
representations; it cannot recover precision or provenance absent upstream.

A nonzero price-direction reversal confirms a local reference; the first
unobserved direction at the packet boundary does not create a fabricated pivot.
Plateaus retain both first and last source identities. The **last** plateau
endpoint is the path origin used by the existing reference-family oracle. The
first endpoint is also retained for a future explicit join to
`completed_swing_facts`, whose candidate identity uses the first low endpoint.
Those two identity conventions must not be conflated.

Equality touches remain active; only a strict price breach terminates a
reference. Nested valleys below a prior high remain present. Birth and breach
events can occur in the same frontier; `active_references()` after commit is the
authority for surviving references. `Result.born` alone is not a stop/entry
confirmation. No outcome, broker fill, fixed age, or selected count decides
which structural references survive.

An indexed context query returns the whole origin-to-current mass plus the
three paths through the latest directed extreme and latest opposite extreme.
Their volume memberships partition `(origin, current]`; each path's origin is
included in geometry and excluded from its volume. Paths can contain child
oscillations. The three paths are not three monotonic legs or independent votes.
Changing phase origins does not change the underlying classified source rows.

## Validation

Pure tests run without the repository conftest/database setup:

```powershell
python -B -m pytest --noconftest -q -p no:cacheprovider tests/test_structural_tape_prefix.py
```

**38 passed.** Tests cover plateau identities, equality versus strict breach,
same-frontier confirmation/undercut/recovery, nested references, unknown initial
classification, persistent fallback, exact fractional mass, supplied-row digest
validation, mixed epochs, stale/conflicting reads, resource failure, retry after
failure, source-frontier reconstruction, out-of-range reads, and randomized
comparison with brute-force references/extrema. Clock dilation leaves structure
and mass unchanged; clocks establish availability, not strategy membership.
An integration test also joins the existing `completed_swing_facts` micro facts
to the new reducer's first/last plateau identities across ten atomic frontiers,
including same-frontier undercuts; only surviving references agree with intact
completed-low facts. This does not yet wire a runtime stop or prove every
source-precision adapter.

The packet verifier has a separate raw-row classifier and compares every
retained structural path against the existing pinned frozen snapshots:

```powershell
python -B scripts/verify_structural_tape_prefix_offline.py --evidence-root D:/dev/chili-home-copilot/project_ws/AgentOps/handoff_astra_0911
```

The required frozen files and helper hashes are explicit in that script. Its
computation functions are AST-identical to the initial local adapter that
produced `ASTRA_STRUCTURAL_PREFIX_PACKET_VERIFY_INITIAL.json`; the repository
CLI adds an explicit input-directory argument and repository-relative engine.
The final CLI receipt is `ASTRA_STRUCTURAL_PREFIX_PACKET_VERIFY.json` in that
evidence directory. These files are research inputs/receipts, not a live DB read.

| Packet | Rows processed | Complete supplied frontiers | Saved snapshots matched | Peak active references |
|---|---:|---:|---:|---:|
| TNON September 11 | 76,000 | 7,101 | 41 | 485 |
| TPET September 10 | 38,926 | 7,030 | 14 | 429 |
| PCLA September 10 | 10,528 | 2,321 | 6 | 126 |
| Total | 125,454 | 16,452 | 61 | — |

The initial full pass completed **1,159,630 raw/frozen arithmetic checks**.
Every source tick's inferred/quote signs is checked; all active reference IDs,
confirmation IDs, phase boundaries and mass values are compared at all 61 saved
snapshots. Future rows are appended only after earlier snapshot checks. This
does not test every possible context query at every intermediate frontier.

Initial measured append-call totals were 20.705 s (TNON), 11.357 s (TPET), and
2.170 s (PCLA); maximum observed single append was 1.828 s. The whole verifier
took 93.016 s. These are wall-time observations under concurrent host load,
excluding some receipt preparation from append timings, not a production
latency/RSS guarantee or a reason to introduce a timer. Throughput/memory under
the actual source adapter and runner remains a release requirement.

The final repository CLI also passed all 1,159,630 checks in 82.406 s with
identical packet prefix digests and checkpoint contexts. Its receipt is committed
beside this report as `2026-09-11_astra_structural_prefix_verification.json`.
The tested engine SHA256 is
`a763b498eb2fc1ca09fc44fe88c1ab281f1fc6cec31e575cc81e31a4599b62cf`;
the repository verifier SHA256 is
`c05c3ab267992b4d329dfcf9b18d7564c3d75e0417c0ca3594e5731f809f5988`.
The elapsed-time difference between runs is not an optimization claim.

## Remaining integration and release gates

1. Bind actual complete source frontiers and durable/reconstructible prefix
   state to the consumer; preserve explicit late/unknown/mixed-epoch behavior.
2. Join confirmed-swing facts by their correct first/last plateau identities,
   with whole-frontier survival, before allowing a structural floor to ratchet.
3. Compare new shared flow separately from fixed-origin and changing-origin
   path effects. Add bid/spread/price response and inventory/pending-exit truth;
   do not count overlapping scopes as independent confirmations.
4. Derive and evaluate complete entry/hold/whole-sale/re-entry policy on both
   loss cases and winning subwaves. Keep the +$8.43 G leg and larger recovery
   winners as controls, and retain their execution/adoption limitations.
5. Use the corrected #1413 benchmark and measure the pending timing-regime
   differences. This reducer does not establish a profitable strategy, repair
   missing historical quote depth, or justify switching a current decision.

Validation here is same-author testing against separate raw/frozen oracles.
Two independent adversarial reviews and program verification remain required.
No active paper lane or bridge was edited/restarted. No broker order, migration,
strategy flag, selected threshold, intentional partial exit, or live caller was
added. Whole-sale and viable re-entry remain the doctrine.
