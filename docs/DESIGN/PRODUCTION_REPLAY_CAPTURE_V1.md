# Production Replay Capture V1

Status: **blocking workstream; sealed/resource foundations implemented, production input coverage not yet certifiable**  
Activation boundary: Alpaca paper/live ordering remains off until the capture,
hermetic ReplayV3, adaptive-risk parity, and out-of-sample gates all pass.

The implemented foundation now includes a content-addressed event index,
decision-bound read receipts, a `CaptureDecisionCheckpoint` whose
`input_prefix_root_sha256` covers only inputs available before the decision, and
a v4 exact-object `CaptureRunSeal`. The seal now hash-binds a runtime-derived
close proof which reconciles accepted/written events, explicit gaps, writer
drain, and ingress closure. Producer registration/heartbeat/quiesce/close facts
are also append-only and hash-chained. These are integrity primitives, not a
certification result: no current retained market session has the complete
provider clocks, watermarks, read receipts and call-site coverage needed to use
them for Ross/OOS credit.

## Certification objective

A ReplayV3 run may receive coverage credit only when it can reproduce a live
decision from the exact inputs CHILI could observe at that time, with no network,
provider cache, current database state, or wall-clock fallback. Market/event time
is used to construct features; recorded `available_at` is the only replay release
clock.

Every FSM read must have a receipt referencing captured content, including empty
results. The manifest must pin account identity, broker environment, code build,
effective configuration, feature flags, process run, and connection generation.
Any overflow, missing stream, late/unbounded provider, continuity break, content
failure, unclean close, or network attempt grades the window
`coverage_unavailable`.

`closed_cleanly` alone is never trusted. Certification binds the manifest to the
v4 seal, its exact close-proof SHA, the producer-lifecycle grade, and a
caller-pinned expected seal SHA loaded through the read-only verified loader.

Receipts are also bound to the requested live decision identity. Duplicate
receipts, a receipt for another decision, mutable/changed hashed evidence or an
inconsistent watermark are coverage failures rather than replay inputs.

## Required streams

The executable registry lives in `replay_capture_contract.py` and currently
enumerates:

- exact IQFeed prints and exact quote/NBBO events;
- hot-symbol L2 deltas plus periodic full checkpoints;
- every queried provider OHLCV response with interval/range/parameters and all
  clocks;
- ORTEX, scanner, catalyst/news, admission and eligibility inputs;
- authoritative halt/LULD, SSR, and session state;
- account identity/equity/buying power/risk state;
- raw broker order request, accept/reject/cancel/fill/reconcile lifecycle;
- effective config, feature flags, code/model build, run and generation;
- FSM decisions, provider watermarks, read receipts, health and explicit gaps.

Query/change inputs can be content-deduplicated. Required hot-symbol market-data
streams cannot be sampled or silently downshifted.

## Resource-bounded architecture

The bounded queue/ring, batched writers, compression, shared admission budget,
single process-wide store quota, adaptive pressure controller, retention tiers,
immutable pins and audited retention dispositions now exist and have focused
tests. `LiveReplayCaptureCoordinator.create_with_shared_store()` prevents each
hot symbol from claiming a separate disk quota, while
`SharedStoreLiveCaptureRunFactory` requires the full runtime configuration and
its hash in startup evidence. These primitives are not yet installed across the
actual runner/provider call sites, so they still do not establish live coverage.
Pressure must never silently reduce fidelity.

1. A bounded broad-universe pre-trigger ring retains preceding context. Promotion
   of a hot/eligible/candidate symbol atomically detaches its prehistory.
2. Hot symbols switch to full-fidelity trades and NBBO. Full L2 is hot-only and
   stores deltas with checkpoints, not repeated full ladders.
3. OHLCV, ORTEX and news are persisted only when queried; change-state inputs are
   persisted only when changed. Immutable payload/chunk hashes provide dedupe and
   integrity.
4. Producers submit to a bounded, non-blocking queue. Batched writers publish
   immutable partitioned Zstandard chunks. Every rejected event becomes a gap; if
   a crash prevents persisting the gap, the absent clean-close proof still fails
   coverage.
5. Raw retention is short, derived bars are longer, and Ross-labeled/traded windows
   are pinned. Quota cleanup must operate from an audited retention plan; it may
   never silently discard a required window.
6. Queue depth, drops, writer lag, compression throughput, RSS, CPU, disk quota and
   watermark lag are capture-health inputs. Backpressure may reduce the hot-symbol
   admission set, but never the fidelity of a symbol already being certified.

## Measurements on this PC (2026-07-14)

Host baseline:

| Measurement | Observed |
|---|---:|
| CPU | Ryzen 9 5950X, 16 cores / 32 logical |
| 5-second average CPU | 31.96% |
| Available RAM | 18.76 GB average |
| D: free space | 240.3 GB |
| Sequential append | 29.0 MB/s |
| Batched fsync p95 | 583 ms |
| PostgreSQL `chili` | 72 GB |

The fsync result prohibits per-event durability calls; chunk-level batching is
mandatory.

Synthetic mixed-stream benchmark, 100,000 events / 16 symbols, zero drops, safe
temporary cleanup:

| Codec / writers | End-to-end events/s | Drain after producer | Peak RSS delta | Raw:compressed | Files |
|---|---:|---:|---:|---:|---:|
| zlib-6 / 1 | 2,798 | 27.19 s | 149.8 MB | 8.46x | 2,021 |
| zlib-1 / 1 | 4,793 | 12.43 s | 183.3 MB | 7.22x | 2,011 |
| zstd-3 / 1 | 6,057 | 8.03 s | 184.5 MB | 9.18x | 2,011 |
| zstd-3 / 2 | 6,313 | 7.21 s | 209.3 MB | 9.18x | 2,012 |

Zstandard level 3 is the provisional codec. Two Python writers added only about
4% throughput, so writer count remains a measured setting rather than an assumed
CPU-scaled value. The synthetic producer reached about 11.8k events/s, faster than
the writer; therefore this benchmark does **not** yet clear sustained 16-hot-symbol
stress. A bounded queue absorbs bursts and exposes gaps, but actual market-session
rates and an in-container benchmark are still required before activation.

Provisional offline-soak envelope, derived from current headroom rather than
strategy literals:

- preserve a 12 GB free-RAM reserve;
- allocate at most roughly 2.5 GB of the remaining headroom to capture;
- target approximately 0.75 GB broad ring, 0.5 GB writer queue, and the balance
  for calibrated hot-symbol state;
- reserve 80 GB free on D: and cap the initial raw capture tier near 80 GB;
- cap capture disk traffic below 25% of measured append bandwidth;
- raw retention target 3 days, derived bars 90 days, Ross/trade windows pinned.

These are captured resource-policy inputs, not trading-risk constants. Final
values must be regenerated from a fresh benchmark at process start/soak and
recorded by hash.

### Fresh contained benchmark (2026-07-15)

A second-generation benchmark of the current packed-object runtime ran 100,000
events across 16 symbols with zstd-3 and two shared writers. It accepted and
wrote 100,000/100,000 events with no shared-admission rejection, produced 42
physical capture objects, compressed 115,170,294 raw bytes to 12,453,694 bytes,
and used a 237,338,624-byte peak RSS delta. Producer throughput was 5,176.3
events/s; end-to-end writer throughput was 2,724.3 events/s with a 17.39-second
post-producer drain and 289.0 ms measured fsync p95 under the contemporaneous
host load.

The 13,634,426-byte retained artifact is content addressed at:

`D:\CHILI-Docker\chili-data\benchmarks\chili-replay-capture-benchmark-dce149e662d146d2827cff50b892eb73-tfp9kipx\reports\59544d56543759f6350de86f5d5b3687afda7fa1e9dfef9db3e01cd4c531219e.json`

Its independently recomputed SHA-256 matches the filename. Synthetic acceptance
is true, but capacity authority remains `diagnostic_only` because an empirical
hot-symbol receipt, full runner/watcher soak, and writer-scaling calibration are
still unavailable. This result cannot authorize paper/live activation.

## Known blockers found by the input-surface audit

- IQFeed L1 currently has no exact quote-event timestamp; its trade timestamp is
  only a containment reference. Exact quote certification is unavailable.
- The L1 bridge now stamps the exact post-insert release clock and commits it in
  the same transaction that emits `NOTIFY`. After that commit it can hand the
  identical released batch to a typed, nonblocking, resource-bound capture queue.
  Hot rows retain socket order/full fidelity; broad rows use the declared bounded
  pre-trigger sampling policy. Queue overflow, malformed envelopes, sink failure,
  and unexpected batch-handoff failure become promotion/run coverage gaps. The
  bridge source/config, connection generation, source-frame order/hash, queue/gap
  limits and capture resource-binding hash are carried in every envelope.
- That IQFeed handoff is still an explicit injection seam, not an installed
  production bootstrap. It also cannot manufacture an exact Q-frame event clock,
  authoritative provider watermark, or provider-process lifecycle proof. Until a
  hash-bound process bootstrap attaches it and a complete session is sealed,
  IQFeed end-to-end coverage remains unavailable.
- Current equity L2 persists periodic local snapshots, not raw deltas/provider
  sequence/event/receive/release provenance.
- Massive stock WebSocket handling now preserves the documented SIP Unix-ms
  event clock, per-symbol provider sequence, local socket-receive clock,
  pre-publication availability clock, process run, and connection generation.
  Its legacy SQL tape remains throttled/diagnostic. A capability-bound exact
  capture producer and nonblocking overflow-to-gap bridge now exist and pass
  focused tests, but no production bootstrap currently attaches that producer
  to the process service for a complete session, and Massive still exposes no
  certifying provider watermark. End-to-end Massive coverage is unavailable.
- Halt/LULD/SSR are inferred/approximated rather than sourced from authoritative
  status events.
- Legacy/current-DB diagnostic replay surfaces remain noncertifying. The sealed
  adapter, read-only expected-seal loader, Python guard, and OS zero-egress
  container gate now exist, but they have not yet replayed a complete
  production-captured live decision whose every dependency is receipt-bound.

These are coverage failures, not permission to substitute sampled or after-fact
data.

## Implementation status

Focused capture-primitive tests cover the following foundation, not an
instrumented end-to-end live decision:

- immutable dual-clock envelopes and exhaustive stream policies;
- full SHA256 identities/content addresses, deeply frozen hashed JSON, an exact
  event graph, decision-bound read receipts, watermarks and gaps;
- decision checkpoints whose predecision sequence and
  `input_prefix_root_sha256` are recomputed from that event graph, rather than
  borrowing the final post-exit manifest as entry-time evidence;
- duplicate-receipt, partition/hash and inconsistent-coverage rejection;
- fail-closed warmup→entry→hold→exit coverage grading;
- deterministic `available_at`, sequence, hash replay ordering;
- deterministic out-of-order pre-trigger expiry, atomic promotion and bounded
  non-blocking ingress;
- immutable partitioned zstd/zlib chunks, query/identity blob dedupe;
- an exact content-addressed run inventory and object seal that detects missing,
  changed, conflicting or post-seal chunk/blob state;
- a v4 runtime close proof which reconciles accepted, written, dropped/gapped,
  drained and writer-finished state before sealing;
- globally bounded explicit overflow/gap accounting;
- durable batched writer synchronization, one-shot lifecycle and failure of clean
  close when submission occurs after shutdown;
- shared-store writer leases so concurrent hot symbols share one disk quota,
  write-rate budget and measured writer-thread ceiling;
- an IQFeed L1 commit-bound immutable handoff with a measured bounded queue,
  source/config/resource provenance, nonblocking parser/DB behavior and upstream
  loss propagation into the same atomic hot-promotion ledger;
- fail-closed disk quota enforcement, adaptive pressure admission, immutable
  Ross/trade retention pins, and audited raw/derived retention dispositions;
- a Python-level replay network guard plus an OS zero-egress container gate and
  exact attestation binding;
- host benchmark script with contained cleanup.

Passing these tests does **not** establish provider coverage, a valid live-run
manifest, a lifecycle-clean exact run seal, hermetic ReplayV3 execution or
sustainable full-session resource use. In particular:

- the production runner/provider process does not yet instantiate the concrete
  shared-store factory or route every read through the capture service;
- Massive/IQFeed/L2/status providers still lack complete authoritative event
  clocks, deltas/sequences and watermarks on all required seams;
- `ReplayNetworkGuard` alone remains noncertifying; only the separate OS gate can
  produce zero-egress evidence, and no complete live capture has reached it;
- current DB-backed ReplayV3 remains diagnostic and ineligible for certification.

Still required before certification:

1. instrument every provider, producer and FSM read seam from the audited call-site
   matrix, including explicit receipts for empty results;
2. replace lossy quote/L2 producers and establish real provider watermarks;
3. install the IQFeed/Massive provider-process bootstraps, bind their lifecycle
   identities, and prove authoritative provider continuity/watermarks rather than
   relying on DB rows or local last-seen frames;
4. emit the implemented event graph, decision checkpoints, prefixes, manifests
   and receipts from actual run call sites and preserve audited retention pins;
5. install the concrete shared-store process bootstrap and prove clean shutdown,
   lease release and aggregate quota enforcement across a real full session;
6. keep legacy/current-DB replay diagnostic and route certification exclusively
   through the expected-seal, receipt-bound adapter;
7. run replay in the implemented isolated process with OS-enforced zero egress,
   equivalently strong external sandbox), without native-client/subprocess
   bypass, and prove identical state transitions, adaptive risk values and
   normalized order intent;
8. wire the implemented quota/backpressure/retention enforcement into the actual
   process lifecycle and prove pressure never deletes or downshifts a required
   or pinned certification window;
9. benchmark and soak the complete instrumented capture path in its actual runtime
   environment, then pass complete-session out-of-sample Ross benchmarks.

Engineering primitives can be completed without waiting for a market open. The
coverage and out-of-sample gates require actual complete market sessions; their
ETA is therefore measured in captured sessions, not an artificial same-day time
promise.
