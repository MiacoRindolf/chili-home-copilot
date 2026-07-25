# CHILI weekend profitability status — 2026-07-25

Classification: **DIAGNOSTIC_ONLY / NO PROFITABILITY OR ROSS-PARITY CLAIM**

This handoff records the bounded weekend evidence review. It does not authorize
PAPER/live execution, broker/provider access, deployment, or a third strategy
hypothesis.

Time-sensitive disposition: **BLOCK/REJECT merge of Claude commit
`20f63a17da301a7a6a586f5052a2b10f69ae8df9`.** At this checkpoint it was
pushed only to the feature branch; no PR existed and `main` remained at
`2b087ccb15c9043f972d21bc32c9ac1b632d50c1`.

## Isolation and code state

- Frozen captured-PAPER lineage is separate from Claude PRs #928–#935 and the
  unmerged PR6 branch.
- Claude PR6 feature commit:
  `0fab28db50f395ff5459bfb188b40603c81b9d1d`
- Claude replay worktree HEAD after merging current main:
  `51ffbf04bcc5f950ef05234346ae9196c2ff5e8b`
- Current main at review:
  `2b087ccb15c9043f972d21bc32c9ac1b632d50c1`
- PR6 driver SHA-256:
  `10593e211e7b587d519ab7d9cbf40af01f7833ea18600c9ef443e889d5bd766f`
- PR6 runner SHA-256:
  `a61b968cddeefc7bb58b87ab3503d20a60188be6c341a510ca9b42c7e9d82841`

## Claude replay scoreboard

All amounts below are raw mock-broker diagnostics, not net/certifying results.

| Change | Paired result | Disposition |
|---|---:|---|
| #928 default-on `bail_on_no_confirmation` | CLRO old flags −$3.72/1 cycle; enabled −$11.90/4 cycles; delta −$8.18 | QUARANTINE |
| #929 catalyst re-sign | No economic/FSM replay | UNMEASURED |
| #931 tick-break tape | CLRO +$0.53; QTTB −$2.90; PLSM no trades; net −$2.37 | MIXED / HURT |
| #933 flush-dip volume | CLRO parity; QTTB +$4.56, but remaining trade still lost | OFFLINE CANDIDATE ONLY |
| #934 stop alignment | CLRO/QTTB parity; PLSM no trades | NO-FIRE |
| #935 structural consumption | CLRO/QTTB parity | NO-FIRE; prospective exposure widening |
| PR6 fresh-ignition re-entry | CLRO −$0.97 paired delta; QTTB −$2.93 paired delta; aggregate −$3.90 | REJECT / DO NOT PROMOTE |

PR6 added one losing CLRO cycle: approximately BUY 14 @ $3.75, SELL 14 @
$3.68, or −$0.97 before fees. The cap counted bailout losses, while G4
escalation stayed zero because it increments only STOP-class exits. The extra
entry therefore did not earn the advertised heightened structural+tape
confirmation. It also added a QTTB cycle of BUY 49 @ $18.99 and SELL 49 @
$18.93, with a paired arm delta of −$2.93 before fees.

After observing both losses, Claude committed `20f63a1` at 15:20:13 PT,
changing the predicate from `instant tape OR sustained burst` to `instant tape
AND sustained burst`, and started new validation arms at 15:20:18 PT. The
commit message already called the new composition "verified" before those
arms started. Returning the same two loser windows to no-fire parity would be
post-hoc in-sample suppression, not positive/OOS evidence that the default-on
feature captures true non-leader ignition. This v2 change is also
**REJECT/PARK** unless it earns the full sealed OOS contract. Claude pushed
the commit to the remote feature branch; at the review checkpoint there was no
PR and `main` remained at `2b087ccb`.

The v2 commit changed no tests. Existing tests collapse both market predicates
into one caller-supplied `ignition_ok` boolean, so they cannot distinguish or
validate OR versus AND composition. The mutable observed-time reads,
decision-receipt gap, artificial leader seeding, and bailout-with-zero-G4
quality gap all remain.

Both v2 predicate legs still query current DB tables directly rather than
requiring the captured-PAPER executed-read inventory or a sealed ReplayV3
decision receipt. A default-on feature cannot earn promotion through a
same-window no-fire result; v2 is at most an OFF-by-default future experiment.

#935 also cannot be treated as a stop-only correction. Its one default-on flag
simultaneously adds ORB/IHS reasons to structural-stop consumption, post-loss
structural classification, and the leader/chase-cap bypass. Its tests prove
tuple/accessor/source-string mechanics, not detector → runner → sizing → order
behavior or paired OOS economics.

## Existing-log funnel

This is a diagnostic count funnel, not selection or profitability authority:

| Window/arm | Trigger waits | Backside vetoes | Candidates/submits/fills | Exits | Result |
|---|---:|---:|---:|---:|---:|
| CLRO PR6 control | 583 | 120 | 4/4/4 | 4 bailouts | −$11.37 |
| CLRO PR6 treatment | 583 | 120 | 5/5/5 | 5 bailouts | −$12.34 |
| QTTB PR6 control | 1,371 | 45 | 3/3/3 | 3 bailouts | −$22.52 |
| QTTB PR6 treatment | 1,414 | 48 | 4/4/4 | 4 bailouts | −$25.45 |
| PLSM #934 treatment | 3,597 | 448 | 0/0/0 | 0 | $0.00 |

The identical CLRO wait/veto counts and exact +1 downstream lifecycle chain
bind the adverse delta to the PR6 cap exemption. QTTB also added one complete
candidate→fill→bailout cycle. PLSM did not exercise the candidate changes at
all. The harness seeds known symbols and does not retain the real
scanner/eligibility/adaptive-risk authority, so missed-winner and
false-positive counts remain unavailable.

## Why these replays cannot promote a strategy

- Mutable current DB and `observed_at` reads; no content-addressed dual-clock
  input receipt, watermark, bounded-lateness proof, or continuity proof.
- No demonstrated exclusion receipt for the 176 historically clock-mutated
  IQFeed rows.
- Every-eighth trade sampling, approximately 1.5-second NBBO grid, and
  rescaled/floored print volume.
- Mock broker with zero configured fees/slippage and no exact-print latency
  model.
- Forced risk approval, mocked operational gates, hard-coded $13,000 equity /
  1% risk, and Robinhood execution family rather than captured-PAPER adaptive
  policy parity.
- Known-symbol, known-window reuse; no walk-forward/OOS split or adequate
  non-Ross negative controls.
- Each PR6 arm clears the viability board and seeds the selected symbol as the
  deterministic day leader, even though PR6 claims a non-leader use case.

Ross evidence remains:
`0 CERTIFIABLE / 4 DIAGNOSTIC_ONLY / 2 UNAVAILABLE / 6 UNRESOLVED`.

## KEEP

The following correctness changes are separate from the Claude alpha chain:

1. `676b6d9` (current-main integration equivalent `1e34fa9`): a vetoed
   first-dip decision does not consume the once-per-day opportunity or leak its
   receipt into a later decision.
2. `5a5a976` (current-main integration equivalent `6c4e7ad`): historical
   replay fails closed when selection inputs are unsealed.

Compatibility worktree:
`D:\dev\chili-weekend-integration-20260725`

Compatibility HEAD:
`6c4e7adbc5c7ac4cb962d6f950cbdd613c8a8001`

Focused result: **287 passed, 2 warnings**. This validates compatibility only;
it does not approve #935 or prove profitability.

## Historical loss/autopsy constraints

Historical current-status manifest SHA-256:
`86cb7dd83e5a11466bed6a91bc0cbc8a4671ae31e91de888ae44bd9cbbd88b0a`

Bound dataset-coverage manifest SHA-256:
`94bb88926da13581463a45f7de9c88958614947131313662323f38be0cff61ca`

Bound broker reconciliation SHA-256:
`2408ca2432763f732fe5480e5f84e5a7189639a085e5aad1fd53729ce7ff51d7`

The previously frozen July 13 reconstruction reconciles:

- local subset: −$451.848993
- omitted ACTU round trip: −$1,259.37
- combined: −$1,711.218993
- displayed broker daily change: −$1,711.22

The three priority exit legs (SOBR twice, TRNR once) were all
`COVERAGE_UNAVAILABLE` for causal replay. There were zero causally scorable
legs, zero selected exit candidates, and zero paired OOS sessions. Broker
classification-to-fill was approximately 2.03 seconds for SOBR cycle 2 and
1.79 seconds for TRNR; most of the previously quoted 10.5/30.4-second spans
were post-fill local observation delay, not proven price-impacting broker
latency.

## Frozen next-session evidence contract

Before another P&L-affecting candidate is considered, retain and bind:

1. exact provider event, received, and available clocks;
2. continuous executable trades/NBBO plus decision-required depth and
   provider watermarks;
3. scanner → eligibility → setup → admission → order → fill → exit receipts;
4. exact adaptive-risk inputs/config provenance;
5. executable ask entry / bid exit, fees, slippage, and bounded order latency;
6. full-session losers and non-Ross negative controls;
7. paired walk-forward/OOS net P&L, R, expectancy, profit factor, MFE capture,
   giveback, drawdown, false positives, missed winners, and risk utilization.

Until that contract is met, the correct weekend action is to preserve the two
correctness fixes, quarantine adverse/unfired defaults, and collect the next
fully causal sessions. No new exit threshold or entry bypass is justified by
the retained historical evidence.
