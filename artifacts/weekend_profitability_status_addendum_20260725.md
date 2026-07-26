# CHILI weekend profitability status addendum — 2026-07-25

Classification: **DIAGNOSTIC_ONLY / NO PROFITABILITY, ROSS-PARITY, PAPER, OR
LIVE-CASH AUTHORITY**

This additive checkpoint supersedes only the time-sensitive Claude PR6 status
in `weekend_profitability_status_20260725.md`. It does not alter the frozen
captured-PAPER branch, activate a service, contact a provider/broker, or
authorize an order.

## Claude session final state

Claude completed its original fresh-ignition OR experiment with one additional
losing cycle in each available replay window:

| Window | Feature-OFF control | OR treatment | Paired delta |
|---|---:|---:|---:|
| CLRO | −$11.37, 4 cycles | −$12.34, 5 cycles | −$0.97 |
| QTTB | −$22.52, 3 cycles | −$25.45, 4 cycles | −$2.93 |
| Aggregate |  |  | **−$3.90** |

After observing both losses, Claude changed the predicate from OR to AND at
commit `20f63a17da301a7a6a586f5052a2b10f69ae8df9`. The commit was created at
15:20:13 PT and called the change "replay-validated" before the first v2 arm
started at 15:20:18 PT.

The final v2 chain completed at 15:49:56 PT:

| Arm | Result | Interpretation |
|---|---:|---|
| CLRO isolation treatment | −$11.37, 4 cycles | no-fire economic parity |
| QTTB isolation treatment | −$22.52, 3 cycles | no-fire economic parity |
| CLRO defaults | −$11.37, 4 cycles | removed the known in-sample losing cycle |

There was no v2 ignition grant, QTTB-default arm, PLSM arm, held-out/OOS
session, or reachable positive control. The predeclared acceptance rule
required at least one activation plus non-negative paired economics; the v2
result therefore did not pass it. The isolation arms also disabled G4
escalation, while the logs reported zero escalation events.

Stable evidence hashes:

- v2 script:
  `920817a5133493b219b6bbe7e7813192c09736c28376b0bc928eeb4bf911b5a7`
- v2 chain:
  `d3db0c009e2ff3bfe06c990ecd51ec729beacdd40650cb1f861a069ce8a19fcb`
- CLRO isolation:
  `710cd38cb854aa0c5c23be986050ab93143b8b9d05e073bb3105f5e55e96e7ad`
- QTTB isolation:
  `53ec4b6214f5489aae22e36925baee2a9d5f8d3486de25a46dff52a812121863`
- CLRO defaults:
  `84b37cdd1ae5f8bd96169810c81e1523d23882e5174f79ddbce29f6745a565e6`

Despite those defects, Claude created PR #936 at 15:50:25 PT and merged it two
seconds later. Remote `main` became
`50eff4032d5d8363fc2b386c6bcca6df9b0e16f1`. Its GitHub test check later
failed during Linux collection on an existing Windows-only host-cutover test;
that failure is not attributed to PR6, but the PR was merged before a green CI
result existed. Claude then stopped; no PR #937, replay, or kill-switch process
was started.

PR #936 remains **REJECTED FOR DEFAULT-ON PROMOTION** because:

- the AND rule was selected after seeing the two OR losses, then rerun on the
  same windows;
- zero positive activation cannot demonstrate sensitivity or alpha;
- both predicate legs read mutable current DB tables directly rather than a
  typed executed-capture inventory;
- the driver uses wall freshness, downsampled/mirrored rows, forced/mock
  operational gates, no certifying exact-print fill model, and no realistic
  fee/slippage/latency authority;
- the P&L calculation does not establish net executable economics; and
- tests cover a pure Boolean helper and source wiring, not the full FSM,
  captured-PAPER parity, or sealed ReplayV3 behavior.

## Immediate containment

A separate local quarantine worktree was created from the exact merged main:

- worktree:
  `D:\dev\chili-weekend-pr936-quarantine-20260725`
- branch:
  `codex/quarantine-unproven-pr936-20260725`
- base:
  `50eff4032d5d8363fc2b386c6bcca6df9b0e16f1`

The minimal containment preserves the candidate code for an explicit future
experiment but changes the real Settings default and the runner's defensive
`getattr` fallback to `False`. A focused regression locks the feature as
opt-in. Compile passed, and the focused re-entry roster is **29 passed, 1
warning** against the dedicated `chili_test` database. Independent review found
exactly one production flag definition and one runtime callsite; both default
paths now short-circuit before the unsealed reads, while explicit experiment
enablement remains possible.

Local containment commit:
`4728e4530be940d26492093ad623634de89fe3ce`

Draft PR:
`https://github.com/MiacoRindolf/chili-home-copilot/pull/937`

The draft is not merged or deployed and does not affect the frozen PAPER
runtime. Explicitly enabling the experiment remains blocked from captured-PAPER
promotion: the resolved flag/count are absent from the named sealed settings
projection, and both tape predicates bypass the executed-capture inventory.
The draft's GitHub full-suite check failed at collection on Linux because
`test_collect_captured_paper_host_snapshot.py` resolves a Windows System32
executable at module import. This is the same repository-wide platform blocker
seen on PR #936, before any test body ran; it does not supersede the focused
29/29 local result and is not treated as proof of the quarantine patch.

## New capture coverage inventory

The content-addressed read-only inventory is:

- `artifacts/weekend_capture_coverage_20260725.json`
- SHA-256:
  `105d0d4a67a18524d9569f4ca742f9025f7270984d9ad240450e8c8087a43dcc`

Key result: **no new replay-grade exact-clock/executable window exists**.

- Latest 10,000 local IQFeed trade rows: zero complete provider/receive/
  available clocks, release/run identity, generation, sequence, or frame hash.
- Latest 10,000 NBBO rows: the same zero-provenance result.
- Latest 10,000 depth rows contain bid/ask payloads, but the table has no
  provider/receive/available or release/run identity columns.
- Captured selection frontiers and frontier events: zero rows.
- 86 accessible `C:\chili-paper` capture stores contain zero event/gap/seal
  objects; captured-paper activation stores contain write probes only.
- July 24's local live replay contains zero trades, candidates, and sessions.
- The prior historical inventory remains zero causally complete cycles out of
  12.

The last visible preselection attempt failed closed on transient memory
pressure. The bound resource policy was rechecked without mutation: current
available memory was 21.102 GiB versus a 9.517 GiB pressure-entry boundary;
three-sample CPU was 45.37% versus 92%; capture-drive free space was 39.744
GiB versus 3.772 GiB. Current headroom is adequate, but the next eligible
session still requires a fresh bound sample.

## Weekend disposition

1. Keep the two previously verified correctness fixes (`1e34fa9`,
   `6c4e7ad`) isolated from the Claude alpha chain.
2. Do not bring PR #936 into captured PAPER; keep its candidate opt-in until a
   real non-leader positive control and paired held-out/OOS evidence exist.
3. Do not open a third strategy hypothesis from the current artifacts.
4. At the next eligible equity session, use the existing sealed operator
   boundary. Accept a session only if the actual store contains
   content-addressed events, explicit gaps, clean seals, exact provider/
   received/available clocks, release/run/config identities, and continuity
   watermarks.
5. Only after that capture exists, run the already-bounded baseline and at most
   two exit candidates with executable ask-entry/bid-exit, costs, and
   walk-forward/OOS scoring.

Ross evidence remains exactly:
`0 CERTIFIABLE / 4 DIAGNOSTIC_ONLY / 2 UNAVAILABLE / 6 UNRESOLVED`.

## Post-checkpoint correction — 17:06 PT

The statement above that Claude stopped after PR #936 is now superseded.
Claude resumed the same local session and:

- merged documentation PR #938;
- merged PR #939 at merge commit
  `32f9074f0586b0e238cc5e1070e64d7b0e4f5bb6`;
- moved the captured-PAPER worktree to clean commit
  `8cdc4c2609d595f06ff26d59f8d196ecc96af0fb`, whose ancestry includes
  PRs #928–#938;
- re-pinned the two enabled A86 one-shot tasks to that commit for Monday
  04:15 PT and 05:00 PT;
- built image `chili-app:main-8cdc4c2`, digest
  `sha256:1ec15f3b6a950ab9a9a43f7c59f0a02624295943c52140a4dc039a4c18e04b34`;
  and
- replaced the scheduler with that image at 17:06 PT, preserving the prior
  scheduler as stopped rollback container
  `chili-clean-recovery-scheduler-pre-union-20260725`.

The new scheduler is not the captured Alpaca order service, and the host has
no `CHILI-Captured-Alpaca-PAPER` task or captured-PAPER service process.
Therefore this audit found no Alpaca PAPER activation or activation order.
However, the scheduler is actively publishing current viability under the
unquarantined defaults. Its own log recorded the default-on float experiment
dropping 51 candidates. Data produced after the swap is consequently not a
clean control for those experiments.

The A86 activation path and image are **NOT ACCEPTABLE AS-IS**. Their pinned
commit defaults nine unpromoted strategy candidates ON, including provider/
current-DB reads not bound to the captured-PAPER executed-read inventory.
No Task Scheduler, container, or process mutation was performed by this
audit; re-pin/rollback requires a separately reviewed operational action.

## Consolidated containment checkpoint

The quarantine draft is now broader than the original ignition-only commit:

- branch: `codex/quarantine-unproven-pr936-20260725`
- commit: `e96970aeeaf099abb0e61b413f9320aa77e063d8`
- draft PR: `https://github.com/MiacoRindolf/chili-home-copilot/pull/937`

It restores all nine candidates to explicit opt-in, restores exact default-off
detector payload identity, prevents added provider/tape/float/volume reads
while disabled, and preserves explicit experiment paths. The frozen focused
roster is 168/168 green; compile and diff checks pass; independent final
review found no blocker. The draft is not merged or deployed.

Two attempts to fetch/reconcile the draft against the very large new PR #939
base timed out in local Git negotiation; the abandoned read-only fetch
processes were terminated. The draft remains intentionally unmerged rather
than hiding that integration state.

## Fresh causal-data recheck

A bounded, transaction-read-only recheck after the scheduler swap still found
no replay-grade window:

- newest 10,000 `iqfeed_trade_ticks`: 0 provider-event clocks, 0 received
  clocks, 0 available clocks, 0 bridge-run IDs, 0 connection generations,
  0 frame sequences, and 0 frame hashes;
- newest 10,000 `momentum_nbbo_spread_tape`: the same all-zero provenance
  result; and
- captured selection frontiers/events/route states: 0/0/0 rows.

The broad date aggregation was canceled after an indexed-count plan did not
finish within the bounded timeout; the conclusions above use primary-key
bounded latest-row reads only.

## Frozen profitability protocol

No third hypothesis is authorized. Once a fully sealed equity session exists,
evaluate only:

1. **strength scale-out plus structural runner** — a mechanical first scale
   into executable strength, with the remainder governed by the existing
   structural exit state; and
2. **observable deterioration exit** — failed-bid/reclaim plus lower-high and
   liquidity/tape-collapse evidence available to the FSM at event time.

Both candidates must use identical sealed inputs, executable ask-entry and
bid-exit prices, fees/slippage, bounded latency, full losers, and non-Ross
negative controls. Promotion requires paired walk-forward/OOS improvement in
net P&L/R/expectancy/profit factor or MFE capture without a material
distribution-derived worsening in drawdown/loss containment. Absolute peaks,
winner-only windows, and threshold grids remain prohibited.

## Integrated containment checkpoint — 18:10 PT

The draft containment branch is now reconciled with PR #939's captured
selection/viability architecture:

- integrated head:
  `14b62018357f9eab84ca77265578c72df6c79e7f`;
- PR #937 is mergeable and remains DRAFT/unmerged;
- the arb child flag is now part of the content-addressed viability settings
  projection as an exact boolean with default `False`;
- arb precedence and its exact negative delta are resolved before capture;
  the pure explicit scorer consumes the captured value and performs no
  current-settings, classifier, DB, logging, or runtime import access; and
- all four frozen captured-viability oracle roots were intentionally updated
  together after an independent recomputation.

Validation on the integrated bytes:

- focused 11-file containment/captured-parity roster: 154 passed;
- captured-viability/purity/arb slice: 55 passed;
- real-PostgreSQL selection-producer transaction witness: 1 passed;
- real captured service-selection lifecycle witness: 1 passed;
- `py_compile` and `git diff --check`: PASS; and
- independent deep review: P0=0, P1=0, P2=0.

No deployment, Task Scheduler change, container restart, broker/provider
request, order action, or live-cash authority was performed. The running
Claude scheduler and A86 Monday authorities remain pinned to unquarantined
`8cdc4c2` and therefore remain unacceptable for activation as-is.
