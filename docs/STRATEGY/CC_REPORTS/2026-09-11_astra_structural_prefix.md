# Persistent tick evidence for structural scope — project 9 [21]/[29]

The current G reader repeatedly classifies a moving fixed-count window. On the
17 reconstructed original TNON G reads, two windows differ from classification
carried across the packet prefix; one actual previous-feature pair changes the
buy/unknown labels of four retained prints totaling 201 shares. Moving halves
also reweight old evidence. A structural replacement must separate those effects
from new information rather than simply choose another N.

`structural_tape_prefix.py` implements the incremental research state needed for
that replacement. It consumes a whole supplied source frontier and keeps
the classified prefix, exact rational mass sums, indexed extrema, and **all**
active confirmed local peak/valley references. There is no selected parent,
strategy sampling window, clock-gap trim, or trading rule. A captured-read
provenance adapter is provided; ordinary SQL adoption and entry/exit callers
remain open. The live G/D decisions and the current 255 setting remain unchanged.

## State and membership

Each source tick carries its database/source ID, event/receipt/publication clocks,
source epoch, price/size/attached quote, and available frame sequence/hash. Epochs
are explicit immutable tuples supplied by the adapter. The packet verifier uses
bridge-run, generation, bridge version, and timestamp basis; it does not invent
an epoch from a time gap.

A recorded frontier supplies its known clock, row count, ordered row-content digest, and
previous prefix digest. The reducer validates all rows before committing. A late
or duplicate tick, conflicting receipt, mixed epoch, invalid population, or
resource-capacity failure returns `unresolved` and preserves committed state.
Even a duplicate receipt must present matching rows. A newly discovered row from
an already-applied frontier requires reconstruction; it is not silently inserted.

Captured consumer reads have a separate `ConsumerFrontierReceipt` contract.
Actual `CaptureProducerLifecycleRuntime` permits separate source events at the
same availability timestamp; sequence and prefix root still advance. Treating
those reads as separate recorded-clock frontiers incorrectly rejects the second
read. Rewriting its clock would corrupt provenance. The consumer contract instead
binds capture identity, current sequence/root, explicit predecessor sequence/root,
and the reducer's predecessor digest. Availability may stay equal but cannot go
backward. Every row must already be known at the supplied consumer boundary.

In this contract, `Tick.id` is the **captured event sequence**, not a SQL row ID.
Rows must advance in both source sequence and event cursor, within the supplied
source interval. Sequence gaps may be other symbols, quotes, or control events;
the caller must prove that no eligible symbol print was omitted. A delta without
new symbol prints may advance source evidence without adding mass or confirming
a wave. Its boundary metadata still changes the reducer digest. The first
receipt has an explicit predecessor anchor; starting there does not claim an
observed history before that anchor. Classification starts unknown as before.

The two contracts cannot be mixed in one segment. Failed deltas preserve all
state, including `last_receipt`; duplicate delivery still validates actual rows.
Consumer metadata is domain-separated in the digest. Existing recorded-frontier
digests and measurements are preserved. A source root supplied by a caller is
not independent source authentication, a provider-completeness proof, or live
order authorization. The production capture reader/coverage adapter remains open.

The caller is responsible for complete frontier membership. These digests do not
prove physical database visibility, a complete provider stream, or independently
fresh quotes. This is a single-owner in-memory component, not a concurrent
transaction service. Catastrophic process/allocation failures are not a durable
checkpoint protocol. Rebuilding from the serialized source frontiers is tested;
incremental durable checkpointing and a production adapter remain open.

## Runtime-owned accepted source inventory

`CaptureProducerLifecycleRuntime.snapshot_iqfeed_sequence_delta` now inventories
all accepted IQFeed prints for one symbol after an explicit positive captured
sequence, under the same lock as source appends. It returns the global captured
sequence/root and last accepted availability clock. Other symbols and control
events contribute to that root; only the requested symbol's exact-print events
are returned. No read receipt or other capture event is emitted by this method.

This is different from the existing `submit_microstructure_window_receipt`,
which correctly answers a requested event-time interval. In an actual lifecycle
test, a newly captured tick has an older provider timestamp than the preceding
tick. The next event-time interval returns no rows while its source frontier
already names the newer capture sequence. The new sequence inventory returns
that late tick. Feeding it to the structural reducer yields an explicit ordering
failure and preserves the previous evidence, rather than silently filtering it.
This is a synthetic integration counterexample, not an identified paper loss.

Requested rows evicted from the bounded index prevent a complete sequence result,
even if their provider clocks are old. Evictions before the explicit anchor and
other symbols' evictions are distinguished. Reported target/whole-stream gaps,
producer-wide gaps, and submission-failure latches prevent a completeness claim.
Unknown or future provider clocks remain visible in the returned raw events;
their consumer interpretation cannot be repaired by changing timestamps.

The snapshot describes **accepted capture input**, not a disk-flush attestation,
certified durable read, upstream provider continuity, source freshness, or order
authority. Its availability is the captured boundary, not a fresh wall-clock read
time. Existing certified-read/attestation protocols are not bypassed. Production
adoption still needs decision-caller integration and explicit recovery for
missing or out-of-order data. No entry/exit caller uses this API yet.

The separate `submit_iqfeed_sequence_receipt` now inventories and commits a typed
`CaptureIqfeedSequenceReadQuery` through the existing `submit_read_receipt` path
under one append lock. The query binds capture identity, explicit sequence bounds,
pre-read global root, captured availability and read availability. The source
capacity rejects a whole read rather than truncating it. The generic receipt
submission path independently re-inventories this query type: removing, reordering
or substituting rows, changing identity/root, or using a stale boundary cannot
mint a complete-sequence claim. A newly arrived same-clock event invalidates a
previously constructed receipt. A different explicit start sequence denotes a
different query; consumers must bind that anchor to their own committed cursor.

`LiveReplayCaptureCoordinator.capture_iqfeed_sequence_read` uses this path,
observes the committed receipt event, and synchronizes the coordinator's global
prefix. The process service forwards the symbol-scoped capability. The parent
root in the query is **before** the READ_RECEIPT event; that receipt advances the
global sequence and can appear as a control-only advance in the next read.
Empty symbol reads have receipts without inventing market mass.

These receipts establish exact returned captured bytes. They preserve missing or
future provider clocks as evidence and do not grant first-dip or final order
authority. Existing provider/continuity/decision attestations remain required.
The coordinator uses its existing committed-read semantics; the snapshot alone
still does not certify a physical writer flush. Structural consumer conversion
and strategy adoption remain separate from this completed read-path binding.

## Captured source conversion

`CapturedStructuralPrefix` now consumes the coordinator's actual committed read.
It reuses `build_executed_capture_read_inventory` to check decision/run identity,
the receipt event and actual source hashes, then validates the sequence query
against its own committed cursor. It requires exact-print bridge provenance and
preserves integer clocks at the supplied datetime precision, capture sequence,
source frame sequence/hash, and an explicit stable source epoch. One immutable
epoch object is shared across the segment; no time gap determines membership.

Each retained tick has a source witness containing capture event, payload and
provenance digests plus the bridge's date/time/TickID/market-center trade key,
namespaced by bridge run/generation. TickID alone is not used as the bridge key.
Repeated identities require reconstruction rather than silent double counting;
the adapter does not guess whether a repeat was a correction. Missing provenance,
changed epoch, frame regression, future-at-read clock, or late event cursor leaves
the prefix, source cursor, read marker and source witnesses unchanged. Resource
failure can retry the same complete read without losing provider identities.

An initial verified capture anchor remains the caller's responsibility. Empty
unchanged source prefixes add no market facts; control-only advances bind new
source evidence without adding mass. A duplicate read is idempotent only after
its actual source content is revalidated. Reconstruction, durable checkpoints,
production memory/throughput measurements and automatic late-data correction are
not implemented here. The adapter is a data-validation seam, not independent
source authentication, provider-clock/freshness certification, or order authority.
No G/D, stop, entry, or re-entry caller has adopted it yet.

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
python -B -m pytest --noconftest -q -p no:cacheprovider tests/test_structural_tape_prefix.py tests/test_structural_tape_capture_boundary.py tests/test_iqfeed_sequence_snapshot.py tests/test_captured_structural_prefix.py
```

**127 focused tests passed** in the latest run: 67 component/integration tests,
37 sequence inventory/receipt tests, seven existing lifecycle neighbors, and
three coordinator tests (one new sequence read and two existing window reads),
and 13 captured-provenance adapter tests. The command above selects 117; the adjacent
`2026-09-11_astra_captured_structural_adapter_verification.json` names every selected
node and pins the exact source/log hashes. Tests cover
plateau identities, equality versus strict breach,
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

Consumer-contract tests cover tied clocks, explicit initial anchors, mixed-clock
deltas, empty source advances, identity/predecessor mismatch, out-of-interval IDs,
late event cursors, mixed modes, atomic resource failure and retry, and identical
mass/geometry under different source proofs. The actual lifecycle integration
submits three synthetic exact-print events at one availability clock, binds the
real lifecycle sequence/roots, and verifies the confirmed valley. Ordinary
recorded-clock behavior still rejects those separate tied-clock updates. Database
connections are explicitly forbidden in this test. This proves the integration
mechanics, not observed profitability or complete live consumer adoption.

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

The consumer-contract revision passed all 1,159,630 checks in 82.672 s with
identical recorded packet prefix digests and all 61 checkpoint records compared
directly against the previous revision's receipt. Its receipt is committed
beside this report as `2026-09-11_astra_structural_prefix_verification.json`.
The tested engine SHA256 is
`191020f08a07db67ec48f99f1f49a27ea98d779be4d363a83eea9c383e509347`;
the repository verifier SHA256 is
`c05c3ab267992b4d329dfcf9b18d7564c3d75e0417c0ca3594e5731f809f5988`.
The elapsed-time difference between runs is not an optimization claim.
The predecessor engine was
`a763b498eb2fc1ca09fc44fe88c1ab281f1fc6cec31e575cc81e31a4599b62cf`.
The adjacent `2026-09-11_astra_structural_consumer_verification.json` records the
consumer test source hashes and the comparison against that predecessor.

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
strategy flag, selected threshold, intentional partial exit, or entry/exit caller was
added. Whole-sale and viable re-entry remain the doctrine.
