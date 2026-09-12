# Durable native broker observation demand

`DurableBrokerNativeDemandService` persists the complete native inventory and
held/pending retention state before exposing its new immutable revision. Opening
the service reads the latest committed checkpoint; it does not call the broker,
create schemas, place orders, or initialize an empty book after a damaged read.
Migration384 adds `momentum_native_demand_checkpoints` through normal application
migrations. No production migration has been applied for this work.

The account hash keys one complete checkpoint containing the prior successful
catalog, latest catalog failure, paired exposure probe and bracket evidence,
retained held/pending members, native aliases and all source revisions. A failed
first refresh after restart therefore keeps the previous observation demands,
marked stale. Only a complete newer inventory can retire inventory membership;
only a complete paired held/pending read can release exposure membership.

Updates stage both books privately and atomically compare-and-swap the old
revision and payload digest. Concurrent first inserts also have exactly one
winner. If SQL fails or its commit acknowledgement is uncertain, the instance
requires a new authoritative open; it never retries the mutation or publishes
uncommitted memory. Pure input validation/capacity failures happen before SQL and
leave the previous committed view available. Each checkpoint is current retained
state, not historical event delivery, a financial ledger, an atomic broker account
snapshot or source-provider coverage. No automatic schema or history reset occurs.

The versioned closed-type JSON codec retains exact native metadata strings,
integers, booleans and tuples without pickle or dynamic class imports. It checks
the account and synchronized revisions, reconstructs catalog metadata/digests,
checks coverage/bracket hashes and retained observations, then recomputes the
native union and non-authority flags. Payload byte capacity is checked before
the full database payload is fetched and before a write. An oversized snapshot
fails whole; no symbol subset is selected to fit it. Hashes detect ordinary
corruption, not coordinated rewriting of every database field.

## Evidence and runtime placement

The final checkpoint/native-demand/catalog/coverage batch passed72 tests in12.96s.
Tests use isolated DB26 schemas and cover retained pending-to-held transitions,
first refresh failures, full/partial reads, native aliases, competing CAS writes,
commit acknowledgement loss, rollback, payload limits, corrupted data and type
confusion. Counts overlap earlier native-demand tests; this is not a whole-suite
claim or actual nonempty account execution evidence.

Actual PAPER GET-only refresh10:27:09Z returned14,381 assets:14,308 equities and73
crypto pairs, with13,508 tradable inventory demands and no held/open-order demand.
The complete14,147,898-byte encoded checkpoint round-tripped exactly. No inventory
was silently filtered. All14,381 native-to-tick-provider bindings remain unbound.
The codec validation round trip took9.915s on this host; it is not instantaneous.

That exact frozen checkpoint was additionally persisted and reopened through the
real store in an isolated DB26 schema: all14,381 native members and every field
matched. Persist/open took5.040s/5.331s in that run; its temporary schema was removed.
These timings establish a placement constraint, not an execution SLA: broker
catalog refresh/checkpoint recovery must run outside per-print entry/exit work.
Shared tick consumers should use already-published immutable context and explicit
native/source revision and coverage state. Neither broker HTTP calls nor full
catalog serialization belongs on their critical path.

Still required: connect a dedicated native-demand producer and the structural
publisher host, bind native IDs to actual provider instruments, preserve mapping
and capacity gaps, perform the coordinated source rollout, and connect measured
entry/exit policy to the same tick revision. Crypto funding/fill/fee/full-sale
integration and backside policy are separate unfinished requirements. This store
is implemented/tested branch code; no new trading behavior is deployed by it.
