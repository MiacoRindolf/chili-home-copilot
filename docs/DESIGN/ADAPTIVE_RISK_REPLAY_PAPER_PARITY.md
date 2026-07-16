# Adaptive Risk: Replay ↔ Alpaca Paper Parity

Status: **schema-v2 pure resolver and strict recorded-packet verification
implemented and tested; ReplayV3 remains diagnostic/noncertifying; canonical
reservation-ledger-to-runtime parity is not yet implemented**

Activation boundary: broker-facing Alpaca paper/live ordering remains off. The
Compose source now sets the momentum live runner and scheduler flags to `0`, but
that source edit has not been deployed or used to restart containers. Effective
container configuration must be reverified before any future activation. The
separate database paper simulator is not evidence that Alpaca paper execution is
enabled.

## Audit result

The existing Alpaca paper posture is not a temporary canary layer. Whenever the
paper rail is selected, `$50` planned loss, `$250` daily loss and one account-wide
exposure are re-applied in several modules. Simulated paper, ReplayV2, ReplayV3,
counterfactual replay and live Alpaca also use materially different sizing paths.
Merely raising the constants would leave policy fragmentation and daily-budget
overshoot intact.

The running-order boundary remains unchanged while this is fixed offline.

The call-site audit also found that paper-draft creation and paper-to-live
promotion omitted `execution_family` when building the immutable session risk
snapshot. That could normalize an unspecified family onto the Coinbase basis.
Both call sites now propagate the already-resolved execution family, with focused
regression tests. This is a correctness fix only; it does not certify sizing
parity or authorize an activation.

## New pure contract

`adaptive_risk_policy.py` resolves a quantity from one complete causal packet:

- stable unlevered account equity and buying power;
- broker day change/local realized P&L;
- open and pending structural risk;
- executable bid/ask, structural stop, spread, entry/exit slippage and fees;
- setup quality and realized volatility;
- ADV, recent volume and executable depth;
- correlation-cluster risk and portfolio gross/structural heat;
- account/config/feature/build/capture identities and timestamped provenance.

The current schema is `chili.adaptive-risk-decision.v2`. It binds the economic
decision to `capture_prefix_root_sha256`, not to a final manifest that can only
exist after hold/exit. It also carries execution family, venue and broker
environment; filled/open and pending same-symbol risk; filled/open and pending
cluster risk; filled/open and pending gross notional; stable policy buying-power
capacity; open and pending broker buying-power impact; and candidate
buying-power impact per share. The required evidence groups include the
content-addressed `reservation_ledger` and
`candidate_buying_power_estimate` reads.

The calculation is transparent:

1. `base_R = equity × configured risk fraction`.
2. Setup quality can scale within the recorded policy range; volatility may derate
   but cannot silently inflate R.
3. Candidate risk is the minimum remaining symbol, daily, portfolio and
   correlation-cluster budget after realized drawdown, open risk, pending risk and
   explicit reserves.
4. Per-share risk includes executable ask, structural stop exit, spread reserve,
   slippage, fees and volatility gap reserve.
5. Quantity is the minimum whole-share cap from structural R, equity/BP/gross
   notional, ADV, recent volume and executable depth.
6. Every input, cap, binding constraint, final quantity/risk/notional, policy hash
   and provenance record is included in the decision packet.

Buying power may constrain notional; it cannot increase R. There is no one-symbol
rule: concurrent positions are admitted only while aggregate daily, portfolio,
cluster, gross and liquidity budgets remain.

`ResolvedAdaptiveRisk.to_decision_packet()` emits canonical JSON-safe
evidence, including nested timestamps. The strict
`load_and_verify_adaptive_risk_decision_packet()` path reconstructs the policy and
inputs, reruns the resolver and canonical-compares the entire packet, including
identity and evidence hashes. Unknown fields, changed values, changed hashes or
impossible timestamps fail closed.

That strict packet check proves deterministic recomputation from the scalar
inputs inside the packet. It does **not** yet prove that the open/pending risk,
gross and buying-power aggregates were atomically derived from the exact
canonical reservation ledger named by the evidence hash. The production
admission/reservation path still needs one snapshot boundary that reads the
ledger, derives every aggregate, reserves the accepted candidate and emits the
schema-v2 packet without a race or a second policy interpretation.

ReplayV3 currently validates a recorded schema-v2 packet against a
decision-bound event graph/checkpoint, its predecision capture prefix,
run/generation, account, build, configuration and feature-flag identity, plus its
recorded availability clock. It persists the recomputed packet only under the
pending adaptive-risk key and explicitly classifies the economic seed as
noncertifying with `adaptive_risk_event_graph_and_runtime_not_migrated`. It does
**not** translate the packet into the legacy dollar clamps, because that would
double-derate the decision and falsely imply runtime parity. A shared direct
consumer of the exact resolved quantity, R, notional and buying-power impact does
not yet exist across ReplayV3, simulated paper, Alpaca paper and live
admission/reservation. The current DB-backed ReplayV3 path therefore remains a
diagnostic harness, not replay/paper/live policy-parity evidence.

## Offline evidence already passing

- canonical JSON packet round-trip and strict rejection of tampered evidence;
- identical pure-resolver output for identical replay/paper input fixtures (this
  is contract evidence, not runtime-path parity);
- buying power cannot increase R;
- daily budget includes realized drawdown, open, pending and candidate risk;
- same-symbol, correlation-cluster, gross-notional and buying-power caps subtract
  both filled/open and pending reservations;
- reservation and candidate buying-power evidence have their own tighter
  freshness boundary;
- a second symbol is permitted when aggregate/cluster budgets have room;
- wider spread, slippage and volatility reduce executable quantity;
- liquidity/depth and correlation each bind and are logged;
- missing, stale or future evidence fails closed to zero quantity;
- grid/property checks prove planned risk/notional never exceed any resolved cap;
- equity scaling is monotonic without a dollar ceiling;
- static AST regression excludes activation-only `50`/`250` literals from the
  resolver.

The focused ReplayV3 tests also prove that mismatched symbol/decision identity,
incomplete capture coverage, a tampered risk packet and impossible decision clocks
are rejected before ORM writes. Legacy ReplayV3 seeds remain available only as
explicit noncertifying diagnostics. These tests use constructed evidence
fixtures; they do not establish an atomic production ledger-to-packet derivation
or broker-facing runtime parity.

## Audited legacy migration blockers

These remain blockers until the shared adaptive packet is consumed atomically by
all economic and broker paths:

- `$50` planned-loss logic is duplicated in policy defaults/helpers, frozen
  snapshots, live and pre-sizing, reservations/orphan reservations, secondary
  heat proxies and other canary-era paths. Alpaca can therefore re-clamp a
  quantity even after an adaptive decision.
- `$250` daily-loss logic is duplicated across policy/governance evaluation,
  automatic arming, fill-boundary and fresh-order checks, operator/UI flows and
  profit-giveback handling.
- Alpaca's strict empty-account posture rejects any existing position or open
  order, while durable reservations reject persisted exposure, other claims and
  legacy pending state. This is an account-wide one-position rule and also blocks
  valid adds/pyramids. Ownership, identity, idempotency, reconciliation and
  unknown/manual-order fail-closed safeguards must remain when aggregate
  risk-aware concurrency replaces that blanket restriction.
- Fixed-notional behavior remains, including `$500` defaults/fallbacks, a database
  paper `min(..., 250)` path, notional-first versus risk-first sizing differences
  and a divergent paper deployment ladder.
- Fixed strategy-count gates remain (`10/5/5`, hard `15`, floor `5`/ceiling `20`
  and crypto `4`). Strategy opportunity limits must migrate to aggregate exposure,
  correlation and liquidity budgets; any operational capacity limit must be based
  on measured system capacity and recorded provenance.
- ReplayV2 and counterfactual tools still contain fixed notionals/counts and do not
  model shared portfolio heat. They remain diagnostic and cannot certify policy
  parity.

Removing these limitations is not permission to remove broker/account identity,
stale-feed failure, order ownership/idempotency, reconciliation or kill-switch
safeguards. Those protect operational correctness rather than cap valid alpha.

## Migration gate

Before any runtime activation:

1. capture the exact account/risk/market packet and its read receipts, then derive
   every schema-v2 reservation/risk/gross/buying-power scalar atomically from the
   canonical captured ledger and candidate estimate;
2. make ReplayV3 consume the verified recorded packet directly, rather than
   storing it as pending or translating it to legacy caps;
3. migrate simulated paper and Alpaca paper to that same direct consumer and run
   golden replay/paper policy-parity tests on the exact same packet;
4. replace counterfactual fixed-risk inputs or keep them explicitly diagnostic;
5. migrate live admission/reservation/governance atomically so all layers use the
   same remaining daily/portfolio/cluster calculation;
6. remove the duplicated dollar and one-exposure clamps, with scoped AST tests over
   the migrated modules;
7. replace arbitrary strategy-count gates with recorded aggregate risk/exposure
   and measured operational-capacity policy;
8. retain account identity, stale-data failure, order ownership/idempotency,
   reconciliation and kill-switch safeguards;
9. validate complete out-of-sample market sessions, including executable
   ask-entry/bid-exit results and aggregate portfolio behavior, before enabling
   Alpaca fake-money ordering.

No live-cash enablement is implied. Engineering migration can proceed offline,
but final parity/OOS evidence requires actual complete captured market sessions;
its ETA is measured in qualifying sessions, not an artificial same-day promise.
