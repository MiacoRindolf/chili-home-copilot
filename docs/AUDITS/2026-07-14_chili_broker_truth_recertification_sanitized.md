# CHILI broker-truth recertification — sanitized completion record

Completed: `2026-07-14T16:39:48Z`

Status: code committed locally for review; accounting correction completed; nothing
merged, pushed, deployed, or enabled.

This record intentionally omits account UUIDs, internal database identifiers, broker
order/client-order identifiers, share quantity, credentials, and local transcript paths.
The exact tuple remains only in the broker, database audit rows, and a controlled local
operator artifact whose SHA-256 is
`ce9ed7bf666a5e7a1f818be71af8acffc6a9c89e288ffba5047aaf39e51e49e5`.

## Evidence and conclusions

- Local Claude Desktop JSONL history was reviewed directly; screenshots were not used as
  the sole source.
- Broker position, order, fill, and account truth was reconciled against CHILI session,
  event, claim, and outcome rows.
- Ross Cameron's same-day recap was reviewed as primary process evidence:
  <https://www.youtube.com/watch?v=S2sOq-stPgA&t=2s>.
- The rejected Claude experiment remains quarantined and unmerged. It did not provide the
  promised fail-closed tape confirmation and could consume a once-per-day opportunity
  before later entry gates vetoed it.
- CHILI cannot know the absolute real-time peak. The measurable targets are move capture,
  open-profit giveback, response to observable deterioration, and loss containment.
- IQFeed was causal but some audited decision rows were stale. Final Massive attribution
  was not proven, and no causal ORTEX evidence was present in the audited entry decisions.
  Provider availability is therefore not treated as evidence of fresh or causal use.

## Implemented safety boundary

Local commit:
`a4ef7a040b8bba39d81a539753971d905f152d63`

The commit enforces, among other controls:

- paper-account identity pinning and generation-aware caches;
- whole-share, regular-session-only Alpaca entries during recertification;
- canonical price-tick rounding before sizing and risk reservation;
- a final account-wide flat/open-order/daily-loss posture proof at the entry transport
  boundary;
- durable account/symbol ownership claims, immutable order outboxes, exact same-client-id
  ambiguity recovery, and cumulative-fill watermarks;
- no scale/add/ordinary partial-exit path for the recertified Alpaca lane;
- a `$50` maximum planned risk per paper position and `$250` maximum paper broker-local
  daily loss admission ceiling;
- fail-closed account identity, terminal-order truth, risk, and governance checks across
  Alpaca, Coinbase, and Robinhood rails;
- IQFeed Q-message-only provenance with provider/receive clocks, process generation, exact
  bridge build identity, and a two-second freshness ceiling;
- dedicated runner ownership fencing and a durable heartbeat required before auto-arm;
- event-loop exit servicing that continues even when new-entry admission is disabled; and
- a paper-posture check before the scheduled Docker socket guard can query Alpaca or
  restart Docker.

The untracked production-module packaging risk was also closed: both the durable claim
module and cross-venue account-identity module are included in the commit.

## Verification

- All 52 modified or new test files ran serially against the dedicated test database:
  `917 passed` in `375.27s`.
- Focused broker-order, entry, exit, risk, feed, heartbeat, and reconciliation groups also
  passed before the aggregate changed-scope run.
- `git diff --check`, Python compilation, conflict-marker scan, staged credential-pattern
  scan, and private-trade-anchor scan passed.
- No credential, generated artifact, or private one-off repair tuple was committed.

Known non-blocking warnings are SQLAlchemy's existing mutually dependent foreign-key
sorting warning and existing `datetime.utcnow()` deprecation warnings.

## Accounting correction

The previously false `cancelled_pre_entry` outcome was corrected from exact broker fill
truth to a non-strategy governance exit:

- gross realized P&L: `-$1,259.37`;
- hold time: `1,270` seconds;
- fees: explicitly `unconfirmed`;
- evolution credit: disabled.

First isolated invocation:

- one exact outcome and one durable settlement marker repaired;
- exactly three historical broker order GETs;
- zero order placements, cancellations, claim mutations, or deployments;
- same paper account remained `ACTIVE`, with zero positions and zero open orders before
  the commit and after completion; and
- an internal second settlement attempt made zero changes.

Independent second whole-process invocation at `2026-07-14T16:37:37Z`:

- `already_repaired=true`;
- zero historical broker order reads;
- zero accounting changes; and
- paper account still `ACTIVE`, flat, and with zero open orders.

## Runtime and rollout state

At the final runtime check (`2026-07-14T16:39:48Z`):

- master live runner: `false`;
- live-runner scheduler: `false`;
- auto-arm: `false`;
- auto-arm scheduler: `false`;
- standalone orphan authority: unset/off;
- the old container's loop configuration remains `true`, but it is inert behind the
  disabled master runner; and
- the container process is only the existing scheduler worker running the old image.

The repaired code was not copied into `/app`, and the container was not rebuilt or
restarted. No merge, push, pull request, or deployment occurred.

## Next permitted work

1. Run offline broker-fidelity replay for leader freshness, failed-setup containment,
   move capture, and open-profit giveback.
2. Require replay evidence before changing strategy gates.
3. Forward-soak the reviewed code in paper only after explicit operator approval and a
   separate deployment review.
4. Keep premarket expansion as a separate protection architecture, not an implicit
   extension of the RTH certification.
5. Rotate any live Alpaca credentials that appeared in local Claude history before any
   future live use.

Daily profitability or parity with Ross is not guaranteed by this repair. The achieved
result is narrower and necessary: broker truth is no longer allowed to be mislabeled,
unknown evidence fails closed, and one unsafe trade path cannot silently broaden itself.
