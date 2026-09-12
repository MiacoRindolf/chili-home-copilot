# Warm recovery of the shared ordinary tick owner

`RecoverableStructuralContext` connects the actual ordinary owner to input and
output persistence. Its `create` method starts from an explicit source anchor;
its `restore` method returns an owner only after reconstructing all retained
inputs and comparing every resulting immutable output under the writer fence.
This is implemented service code, not yet ordinary trading lifecycle activation.

## What survives a restart

Migration383 adds recovery heads and ordered input capsules, alongside the
migration381 input release journal and migration382 output/consumer journal.
Schema creation remains migration-only. None was applied to the running lane.

The initial capsule records the source anchor, reducer capacities, symbol and
trade-row resource limits. Later capsules retain successful/failed authoritative
demand inputs with their original revisions, or the exact original multi-symbol
source read with publication membership, rows, observed frontier and observation
clock. An external source-read failure records its observed failure status.
Every capsule binds the implementation file identity and resulting output cursor
and payload hash. Typed serialization preserves integers, datetime precision and
original timezone/naive-UTC representation; no dynamic class import or evaluation.
The observation clock records when source evidence was known. It does not select
a seconds-based strategy population or boundary.

Input insertion, its chained head update, the output publication/head and wake
notification share one transaction. A higher demand revision may leave the output
snapshot identical; that input still commits, without manufacturing a new output
or repeating a source event. Otherwise restart could accept an already-consumed
inventory revision and silently restore the wrong state.

A transaction that rolls back leaves no durable input or output from the failed
attempt. Private reducer mutation is discarded through owner reconstruction. If
the transaction commits but its acknowledgement is lost, recovery discovers the
committed input/output pair and resumes after it. It does not replay the source
as a second live event. Existing selection/entry/exit consumer offsets stay intact.
This proves internal publication/recovery behavior, not exactly-once broker orders.

## Reconstruction and refusal conditions

Recovery first acquires the same PostgreSQL session advisory lock as publication.
It holds that connection throughout a read-only repeatable-read reconstruction.
Each input is fetched only after checking its stored byte length. The caller must
supply explicit input/output byte and replay-work capacities; exceeding them
refuses recovery without returning a partially restored owner. These are resource
bounds, not strategy lookbacks or permanent opportunity-universe limits.

Recovery validates the input chain, implementation identity, original source
anchor, every output chain link and snapshot, and terminal input/output/source
heads. It runs the actual reducer with the retained inputs and original clocks.
The next live observation uses the newly supplied clock only after reconstruction.
A retained unresolved observation remains unresolved; replay cannot certify fresh
provider health, correct a delayed feed, or invent pre-anchor history.

The writer fence remains exclusive throughout reconstruction. Any missing input,
changed output, mismatched computation/code, missing legacy recovery history,
capacity failure or reconstruction error closes the writer. There is no silent
empty restart under an existing stream name. A new implementation requires an
explicit audited upgrade/replay path; this version refuses a code-identity change.
Hashes compare supplied retained evidence; they do not authenticate the upstream
provider or defend against coordinated rewriting of every input and output.

`writer_present` still means a held database fence. In particular, a recovering
owner may hold it before it is ready. It is not proof of current provider health,
completed recovery or trading eligibility. The application lifecycle must only
expose the returned recovered owner and must bind actual source/execution health
before connecting trading decisions.

## Verification and remaining work

The initial recovery/output/owner/migration batch passed53 tests in45.11s.
Tests use the isolated chili_rossbench26_test database and throwaway schemas.
The final batch passed70 tests in54.17s, including source-journal neighbors,
historical backlog with late symbol enrollment, code-identity read failure, and
unserializable-input fencing. Counts overlap; this is not a whole-project suite.

Coverage includes a separate process that reconstructs identical context with
source-reading and fresh-clock calls forbidden; altered old trade-table rows;
unchanged-output demand revisions; continuation against an uninterrupted reducer;
held/pending membership; existing consumer offsets; rollback after input insertion;
commit acknowledgement loss; concurrent recovery ownership; corruption/deletion;
changed observation clock even after rehashing the input chain; unresolved source
and read failures; legacy streams without input history; and capacity refusal.

Provider subscriptions and application lifecycle still need integration. Actual
selection/entry/exit callers, selected parent/local derivation, the calibrated
tick net-opportunity model and Ross perturbation invariance remain unfinished.
Crypto native source semantics and fractional PAPER execution remain separate
required work. Independent adversarial reviews, final integration verification
and coordinated migration/producer/PAPER rollout are still required. No trading
activation, lane restart, broker order or profitability claim follows from this
recovery verification.
