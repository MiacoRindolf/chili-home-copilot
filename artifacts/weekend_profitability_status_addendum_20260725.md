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
