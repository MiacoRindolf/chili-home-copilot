# Ordinary print publication inventory for shared context

The ordinary IQFeed bridge already persists pending trades and releases them in a
second transaction. Quote wakes may coalesce, and a highest-trade-ID watermark
misses a smaller ID committed/released later. The context source therefore needs
the exact committed release membership, independent of notifications.

Migration 381 adds a journal head and release manifests. IDs 379 and 380 are left
reserved for pending quote evidence and task37 work. The actual bridge release
uses UPDATE RETURNING id,symbol to record precisely which trades became available.
The journal and its small wake notification commit or roll back with that release.
A singleton head serializes publication revisions through transaction completion.
A publication can contain several symbols. It is an atomic source frontier, not
a chosen strategy window. Canonically sorted database IDs are not provider order.

The reader requires a repeatable-read/serializable database snapshot. Its explicit
cursor is (journal epoch, revision). It reads all intervening publication numbers,
including those without the requested symbol, and fetches exact trade membership
by primary key and symbol. Lost manifests, missing trade rows, changed availability
or a replaced epoch are explicit recovery errors. Capacity limits page whole
publications; a spike and reversal in one release cannot be split into fictional
intermediate decision opportunities. Slow consumers can catch up without reading
only the latest quote. There is no rank/position-membership dependency in this
source reader.

The journal is populated by the bridge at runtime after a coordinated rollout.
The reader is not yet wired into the Momentum Neural context owner or its
selection/entry/exit callers. This build is not the completed shared-context
service and does not authorize a backside rule.

## Boundaries that still matter

- Journal completeness means membership of this instrumented release path only.
  An old bridge, another writer, predeployment history, omitted provider frames,
  and synthetic self-test rows do not become authenticated inputs by reading it.
  The rollout must establish producer identity; cold starts declare their anchor.
- Trade fields still need source-epoch/frame/event-order, timestamp-basis, delay
  and quote-evidence validation before structural reduction. An immutable returned
  mapping is not authenticated historical payload storage. Table retention can
  cause a declared gap; it must not silently reset the consumer to the latest row.
- available_at is the existing post-insert publication stamp, not the physical
  commit clock. Read visibility is established by the consumer database snapshot.
- The serialized head adds per-batch work and contention, not per-print SQL calls.
  Throughput/latency under representative writer load must be measured before
  rollout. No new publisher/catch-up worker or live bridge was launched here.
- Crypto uses a separate venue-labelled source, never the IQFeed equity table.
  The shared structural contract must preserve provider-reported taker side
  separately from inferred signed flow.

## Verification so far

28 source/journal/provider-delay checks passed. Coverage includes actual SQL
release through both ID and identity-join paths, spike/reversal membership,
two reader snapshots, late smaller-ID publication, rollback, duplicate release,
concurrent writers, explicit source/retention gaps, atomic resource pagination,
commit-coupled NOTIFY, and all existing write-mode/fallback delay storage paths.
Bridge neighbors: 86 passed, 2 failed. Both failing provider-loop tests reproduce
on unchanged base main 4f75f0a0203083ac096d35a3519fa2348c87e438. Their fixture/runtime
contract mismatch remains to be resolved before final verification; this is not
a green entire-neighbor result. Independent reviews and deployment are pending.
