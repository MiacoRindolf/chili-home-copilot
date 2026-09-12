# Changed-symbol publication at one shared revision

The ordinary owner no longer reduces or hashes an empty frontier for every enrolled
symbol when one source release arrives. It validates the contiguous global source
cursor and observation clock once. Each symbol prefix links its own nonempty
publications; the complete source coverage remains in the shared snapshot's source
cursor, source root and `source_observed_ns`. Receipts name this distinction as
`nonempty_symbol_publications; coverage=context_source`. The observation clock is
transport evidence, never a seconds-based strategy window.

Unprinted symbols retain their immutable state. Prior release events are cleared
once on the next source release so an event cannot be delivered again. The next
actual print preserves that symbol's frame order, epoch, accumulated wave structure,
flow and nonempty-publication predecessor. A malformed or missing global release
still prevents every symbol from advancing. Source queries send only the selected
members of the actual release, with the complete original release manifest retained;
the full enrolled universe is not retransmitted as an unnecessary SQL parameter.

## Durable representation

Publication contract v5 carries a typed global header, changed symbol values, a
base-state hash and the resulting complete-state hash. The complete state binds a
canonical manifest of every symbol's object hash and native routing descriptor.
The writer reuses unchanged immutable values and their encodings. Metadata hashing
is still linear in total symbol count; it does not reserialize all wave histories.

Three materialization tables are created by the still-unapplied migration382:

- `momentum_structural_context_objects`: immutable symbol values addressed by hash.
- `momentum_structural_context_members`: the complete current symbol index.
- `momentum_structural_context_versions`: historical index revisions.

Changed values, current/historical index rows, the publication, global head and
original recovery input commit in the same fenced transaction. PostgreSQL array
inserts carry the complete changed set without a round trip per symbol. Transaction
failure or an uncertain commit closes the owner; recovery determines what committed.

Historical event readers apply every delta to the exact prior state, preserving
all events and consumer offsets. A reader starting partway through history loads
the matching historical materialization. Recovery replays original inputs, compares
every resulting state and verifies the terminal current materialization before
returning a warm owner. Bad indexes, missing objects, corruption and old code or
contracts do not produce a silently reset stream.

## Current requested-symbol reads

Selection's current reader validates the complete compact index against the
publication's state hash in the same repeatable-read transaction. It then loads
only requested broker aliases or native UUIDs, verifying their symbol object hashes
and descriptors. A missing unrequested index member still invalidates the read.
This is complete membership verification, not certification that all providers are
live or that every unrequested symbol object was decoded.

The receipt identifies the shared state hash and `requested_symbol_projection`.
Missing mappings and ambiguous/stale native identities remain explicit. A projection
cannot report full event delivery or acknowledge historical offsets. Entry/exit
event consumers must use complete delivery with their durable offsets, not substitute
this current view for a replay of triggers they missed.

Caller byte/work budgets remain explicit resource limits. Publication bytes,
materialization metadata and selected object payloads are bounded before reads;
overflows are unavailable evidence, never successful top-N coverage. Historical
object/version retention and cold enrollment remain measurable storage costs. No
automatic deletion or stale fallback is introduced.

## Verification and remaining release work

Actual isolated database tests cover sparse source updates, global empty-release
clock recovery, exact historical state, unchanged-symbol event clearing, complete
index verification, native alias/UUID projections, forbidden partial acknowledgment,
materialization rollback and corruption. The full frozen native catalog is exercised
separately with synthetic source releases and exact warm recovery; those prices are
diagnostic inputs, not an executable-profit or market-throughput study.

The final full-catalog run retained13,426symbols and emitted one changed symbol
version per synthetic release, with publication payloads29,814-31,482bytes. The old
whole cold snapshot was10,434,541bytes. Four unprofiled release paths took157-189ms;
requested-symbol reads took309-596ms. Cold enrollment took7.54s and exact warm
recovery11.43s. The first version of this implementation took40.14s/34.42s for
enrollment/recovery; batching writes and validating each header once removed much
of that overhead. These timings include different paths than the old standalone
encode/decode measurement, so they are not a direct end-to-end speedup comparison.

Latest wider batch:122passed plus one new test setup error (source anchor read
lacked repeatable-read isolation). After correcting that test,33focused delta/
publisher checks passed31.92s, including new migration-table preflight cases; the
actual selection caller check passed6.69s. Earlier94integration and54focused checks
also passed. Counts overlap; no whole-suite claim. Full evidence and source hashes
are in AgentOps/handoff_astra_0911/ASTRA_CHANGED_SYMBOL_FULL_CATALOG_VERIFY.json.
No production throughput or latency guarantee is established by four releases.

The actual native/mapping worker, publisher host, coordinated source rollout and
entry/exit policy consumers still need deployment. This implementation remains
observation-only in draft1429; it does not enable native crypto orders or a new
backside veto. Continue coherent PAPER releases under the operator authorization.
