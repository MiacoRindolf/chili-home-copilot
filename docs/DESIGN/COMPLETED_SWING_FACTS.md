# Completed local-swing facts: research reducer v1

`completed_swing_facts.py` is an observational, stdlib-only reducer. It has no
runtime caller, DB adapter, stop/order operation, D-field assignment or market
threshold. It does not select a profitable swing scale. The paired reference is
the local plateau at the start of the immediately preceding maximal decline,
with an observed rise into that peak. It is not a half-window minimum or VWAP.

## Facts and identity

Each contiguous price plateau retains its first/last exact print witnesses and
count. A strict decline followed by a strict rise creates one fixed-low candidate.
The low and paired peak never evolve afterward. Identity is the candidate ID
`<stream_key>:low:<first-low-print-id>` **scoped by segment_key**; the stream key
must bind the caller's leg/entry identity. A new continuity segment cannot inherit
an old candidate merely because its string ID matches.

Three independent variants are reported:

- `micro_upturn`: first strictly higher print after the low plateau.
- `local_peak_reclaim_ge`: a touch or crossing of the observed paired peak.
- `local_peak_reclaim_gt`: a strictly higher print than that peak.

A strict low undercut before reclaim invalidates the pending variant. Equality
at the peak does not confirm the strict variant. Equality at the low does not
break it. Missing left-boundary rise evidence produces `unknown_peak_at_boundary`
for reclaim, not an invented peak or a candidate that future prices can repair.

## Complete recorded frontier

V1 accepts only `recorded_known_frontier_v1`: all supplied rows share
`max(event_us, received_us, published_us)`, clocks are explicit UTC microseconds,
and publication cannot precede receipt. The event/ID cursor strictly advances.
An epoch change or late insertion is unsupported and leaves the committed state
unchanged. Starting another segment requires an explicit new `State` and a new
segment key; there is no timed expiry or hidden reset.

The caller supplies `Receipt` with exact row count, final cursor, row digest and
previous-state digest. This verifies membership in the supplied frame; it does
**not** prove that a DB query included all eligible rows, provider completeness,
physical commit visibility or an actual consumer/captured prefix. A future
adapter must establish its own complete membership authority. Do not treat an
N-print tail or the last available timestamp as sufficient proof.

`Frontier(state, receipt, capacities).feed(rows)` can accept arbitrary transport
chunks. Only `finish()` returns committed facts. A confirmation that is strictly
undercut later in the same complete frontier becomes
`confirmed_broken_by_frontier`, even if the price subsequently recovers. Otherwise
it is `confirmed_intact_at_frontier`. A published historical fact is not a promise
of validity at subsequent frontiers; v1 does not track all subsequent breaks.

Incomplete, conflicting or resource-exhausted frontiers return `unresolved`, no
facts, and the **same supplied frozen State object**. The caller can discard the
working frontier and replay from that checkpoint. JSON restart verifies the
version, exact record shape, checksum and internal identity/cursor consistency.
The checksum detects corruption; it is not authentication. Exact last-receipt
replay verifies its rows again and returns `already_applied` without duplicate
facts. Unknown stale/conflicting receipts are rejected.

## Explicit resource contract

`Capacities` has no defaults. Callers supply positive integer limits for pending
candidates, staged facts, frontier rows and serialized-state bytes. The byte limit
also bounds a single serialized input row. Transient storage is bounded by object
counts and row sizes; this is not a global process-RSS bound. Historical emitted
output and caller-owned input storage are outside the reducer state.

With U unresolved candidates and F facts staged in a frontier, the simple
implementation uses O(U+F) records and O(U+F) comparisons per print. Serialization
and receipt hashing have additional linear cost. No older candidate is silently
evicted. Repeated touches below an unbroken peak can leave arbitrarily many
strict-reclaim candidates pending, so exact all-candidate semantics do not imply
constant memory. Capacity exhaustion invalidates the entire working frontier;
it does not substitute a market timer, depth or newest-only selection rule.

## Offline verification

The app package's existing `__init__` imports the trading pipeline. The research
test and oracle therefore load only this file by `importlib` and do not import
the application or pytest DB fixtures.

```powershell
python -B tests/test_completed_swing_facts.py -q
python -B scripts/verify_completed_swing_facts_offline.py --help
```

The oracle takes explicit artifact/output paths and all resource capacities. It
pins the existing September10 study/ledger and verifies source hashes before and
after. Every one of the27 legs is checked against its exact candidate IDs,
plateaus, local peaks, confirmation/invalidation IDs and clocks, same-frontier
undercuts, pre-entry exclusions and unfinished decline. It compares uninterrupted
processing with caller-selected chunks plus restart at every frontier. The two
TPET legs without eligible held prints remain unknown, not zero-opportunity proof.

These are dependent descriptive structures within a broker-held cohort censored
at actual close. There is no stop selection, executable-fill simulation, net
reward claim or interpretation of a later recovery as money recovered.
