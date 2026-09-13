# Grouped native tick source — implementation in progress

The current V1 source repeatedly fetches and rebuilds the complete REST history. Actual PAPER entry receipts used quote references several minutes older than the broker fills. Recovery rebuilds every historical publication and took about 42 minutes in the measured generation. The new components separate observed-frontier acquisition from full-history audits and append newly observed members to the existing tick math.

These components are implemented and connected to an explicitly configured host adapter in the source branch. Production activation and prospective verification are still pending. No strategy deployment or sustained latency claim follows from this document.

## Data path

1. `frontier_source.plan_frontiers` receives the exact prior membership, original anchor, inventory and source-state digest. Each trade/quote channel covers every symbol, including cold and non-USD pairs. Dynamic programming minimizes known page units, then duplicate rows, then request groups among contiguous observed-frontier partitions. This is a transport cost objective, not momentum ranking or a profit optimum. Actual new arrivals can change page counts.
2. `collect_frontiers` records each actual request/response before parsing it. Every group preserves its own start and end, page tokens and receipt clock. All page chains must finish before a complete grouped observation exists. Global page/member capacities, provider exhaustion and durable-write failures stop the pass without declaring missing symbols complete. No retries or trading windows are introduced.
3. `frontier_members.merge_members` deduplicates exact provider trade IDs and quote values. Current observed members within a queried interval must remain present and unchanged. A full-anchor audit can add old observations but cannot erase input beyond its own query end. An old fast plan cannot replace a newer state; an older audit must descend from a retained state in the same source lineage.
4. A trade at/before its symbol's retained trade frontier, or a new quote strictly before an existing later trade, marks the symbol for reconstruction. `frontier_reducer` then creates a new current revision with all accumulated evidence. Other batches append only new members. The implementation currently rebuilds all symbol contexts when any symbol needs reconstruction; optimization must preserve the same semantics.
5. Publication uses the existing `CryptoTickContext` and exact wave/flow reducer. A normalization receipt binds the real grouped observation, cumulative member-state hash, prior publication and combined known-at clock. It does not invent a single uniform HTTP interval. New immutable views become visible only after the publication callback durably returns.

The source's observed frontier is not provider finality. Full-anchor audits remain required to find previously unseen older trades or quotes. Old publications are immutable; a late discovery does not become information available at an earlier trade decision. Repeated quotes with the same event time have no newly asserted provider ordering guarantee.

Member-state checksum version 3 uses immutable `MemberSequence` nodes. Each node hashes its prior content root, total count and exact new trade/quote fields. Snapshots share old nodes; new updates neither copy nor reserialize the whole retained evidence. Source lineage and clocks remain in the state checksum. Chunk boundaries record append provenance and are not strategy windows. Replacing a node recomputes its root, and deep chains iterate without Python recursion. This changes a new component's checksum representation, not original V1 metadata or history.

On retained Alpaca observations 139→140, all 73 symbol math/flow/economic decisions matched the full reference after this change: grouped apply 1.181s, full rebuild 30.249s in that run. The prior version measured 3.068s versus 37.621s in an earlier run. These are CPU replay results, not prospective feed or order latency.

## Failure and restart contract

Input membership snapshots remain immutable. A reducer that fails after beginning an append or publication is invalidated and refuses further use. Recovery must replay the seed and only committed grouped publications with their original clocks. The tests verify exact reconstruction of committed delta and late-data publications, and rejection after fsync failure.

`record` is a durable sink contract supplied by the owner. In-memory test callbacks do not establish filesystem durability. The production capture owner must implement fsync, a bounded append-only hash chain, raw-response replay validation and a source-directory lock before host integration. Dataclass construction is not an authentication mechanism or order authority.

## Remaining activation before PAPER release

* Create a current transition manifest bound to the unchanged V1 metadata and complete retained chain boundary/root. The adapter refuses a stale seal that leaves newer legacy bytes outside the transition. Preserve V1 history; never rewrite its implementation hashes to bypass checks.
* Validate all-symbol prospective acquisition against full reference audits, then cut over through the authorized flat PAPER ownership boundary and verify source age, decisions, account ownership, fills and uptime.

Budget, broker quantity increments, short borrow authority and entry/cover ownership remain separate account/execution responsibilities. Improved input speed alone does not prove a profitable wave or certify short execution.
# Durable grouped capture component

`frontier_capture.FrontierCapture` now owns a directory lock and fsynced hash
chain, replays original HTTP records through the collector, and reconstructs
committed grouped publications through the reducer. It seeds the existing math
once per recovery. Failed/incomplete observations do not advance the publication;
torn or changed records fail recovery. An fsync failure disables the writer.

The caller must still verify the externally supplied legacy seed origin. The
component binds that seal, seed evidence, seed publication, resources and code
identity. The separate V1 seal/seed loader verifies retained old bytes and the
latest full publication; production transition remains pending.
This component is not deployed and is not a claim of prospective latency.

## Indexed membership and sealed recovery

Member snapshots now maintain immutable key maps and per-symbol event indexes.
Queried inclusive ranges still require every previously observed member; a late
audit cannot delete newer evidence. Unchanged symbol arrays/indexes are shared,
while changed maps currently copy retained keys. This is not yet an entirely
delta-cost membership structure. A paired warmed-seed benchmark on the same
retained observation measured median merge 0.620s before and 0.548s after across
six alternating pairs with identical outputs. The subsequent full reducer run
was 1.300s versus 34.506s full rebuild, so it does not establish an additional
full-pipeline gain over the earlier 1.181s run.

`legacy_seed.load_sealed_seed` checks every envelope in an externally pinned V1
byte prefix and rebuilds only its latest full-anchor publication. It verifies
unchanged metadata/code identity, chain root/count, original request and exact
latest raw HTTP evidence and mathematical publication. It does not authenticate
its external seal or re-certify every historical decision. Unsealed future bytes
are excluded, and an incomplete sealed attempt cannot become a committed seed.
The host transition must obtain and bind a verified current seal; the loader
alone is not a deployment or authority grant. Eight seed tests and three index
tests pass in addition to the 66 existing source/durability/sequence tests.

## Background audits and host ownership

The journal serializes writes from one fast collector and one full-anchor audit
collector, retaining interleaved observations with their plan identities. Only
the source publication owner mutates the reducer. Completed audits wait for that
owner and merge between fast observations; an older audit keeps newer data.
Restart checks interleaved raw chains and committed publications and recovers
durable pending audits without declaring them already published. A successful
fast update cannot erase an unresolved audit failure. Closing waits for writers
before releasing directory ownership.

Host config accepts both `grouped_source_manifest_path` and its SHA256 together.
The manifest fixes legacy `source` and new `source-grouped-v1` sibling directories
under the existing host root. The adapter preserves assets/resources, source
identity, ledger and PAPER lease checks. CurrentRunSource still requires a fresh
host observation before entry. The new `source-audit` worker does not publish or
place orders. Provider reset evidence is handled even when a collector raises on
exhaustion, while the raw HTTP response remains durably retained first.

113 targeted source, recovery, concurrency, host and lifecycle tests passed,
including the real host assembly with simulated PAPER inventory/process boundary.
This is not actual broker fill or production activation evidence.
