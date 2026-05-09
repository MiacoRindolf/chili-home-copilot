# NEXT_TASK: f-coinbase-autotrader-enablement (Phase 1: audit)

STATUS: PENDING

## Goal

Phase 1 of a multi-phase initiative to wire the autotrader for
Coinbase alongside Robinhood. Operator funded $2.2k cash on
Coinbase; wants the broader crypto universe + native crypto
stop-loss primitive that Coinbase provides.

**Phase 1 is read-only audit.** Zero code changes shipped. Outputs
a thorough report covering capability, RH-implicit assumptions,
cost economics with real numbers, risk-infrastructure gaps, and a
proposed venue abstraction design for Phase 3.

The full multi-phase brief is at
`docs/STRATEGY/QUEUED/f-coinbase-autotrader-enablement.md`
— **read it first.** This NEXT_TASK is Phase 1 only; the brief
covers Phases 1-7.

## Why now

Today shipped:
- Brain Phase 2 producer completion (mining is back online via
  watchdog hook) — new crypto patterns will start flowing in 24h
- 11 RH crypto positions still open + working; pattern lifecycle
  durable; reconciler chain (A+B+C) solid
- Bracket-writer crash dead

The system is ready to receive a new venue. Coinbase enablement
is the highest-leverage growth move: wider universe, lower
operational risk per crypto trade (no IndexError class issues,
native stop primitive, no PDT conflation), and the operator's
$2.2k cash is sitting idle.

But — operator's directive is **"don't band-aid; properly
integrate."** Phase 1 is the load-bearing audit that prevents a
band-aid. It demands explicit design choices for venue
abstraction, multi-venue risk aggregation, position-correlation
across venues, and cutover strategy for the 11 currently-open RH
positions.

## Why this scope (Phase 1 only, audit-first)

* **Vs. shipping all 7 phases at once**: would be a multi-week
  effort done at midnight — the exact dangerous mode that
  produced tonight's Phase E false-cancel mistake. Phase 1
  surfaces the integration plan; subsequent phases ship on
  fresh-start days with explicit operator approval.
* **Vs. CC's "just wire Coinbase auth and add an if/else"
  approach**: that's the band-aid the operator explicitly
  rejected. Phase 1 demands a venue abstraction design.
* **Vs. starting with Phase 2 (auth verification)**: Phase 1 is
  cheaper (<1 hour CC, read-only), and its audit might reveal
  Coinbase auth is actually working — saving a Phase 2 round-trip.
* **Vs. queued briefs (cpcv-gate-emit, oos-revalidation,
  pattern-discovery)**: Coinbase enablement is higher-leverage.
  Operator's funded cash + tonight's mining producer fix mean
  pattern flow is restoring; the binding constraint becomes
  venue capacity.

## The change (Phase 1 deliverable)

Read-only audit producing one report at
`docs/STRATEGY/CC_REPORTS/2026-05-09_f-coinbase-autotrader-enablement-phase-1-audit.md`.

The report MUST cover (per the brief's expanded acceptance
criteria — sections A through H):

* **A. Capability inventory** of `coinbase_spot.py` +
  `coinbase_service.py` with line refs.
* **B. Autotrader RH-implicit assumption inventory** — every call
  site that hardcodes RH; the refactor surface for Phase 3.
* **C. Cost economics with REAL numbers** — Coinbase fee tier,
  round-trip cost at taker/maker mix, minimum-edge calc per
  fee path, comparison to patterns 1011/1016's realized edge.
* **D. Risk infrastructure audit** — drawdown breaker
  cross-venue or per-venue, PDT confirms-skip-Coinbase, kill
  switch cross-venue support, position-correlation risk for
  same-ticker on both venues.
* **E. Venue-abstraction design** — propose ONE design (adapter
  pattern, function dispatch, polymorphic Trade — pick) with
  cutover strategy that doesn't break the 11 currently-open RH
  crypto positions.
* **F. Reconciler + lifecycle audit** for Coinbase — webhook vs
  polling architecture, fast-path overlap with autotrader.
* **G. Phase 2-7 scope + prerequisite chain + risk-to-existing-
  system per phase**.
* **H. Hard constraints honored** — zero code changes from
  Phase 1.

## Acceptance criteria

See full brief for the demanding 23-criterion list (sections
A-H). Summary:

1. Read-only audit report committed.
2. Capability + assumption inventories with line-ref citations.
3. Cost economics with real numbers tied to Coinbase tier.
4. Risk-infra audit covering breaker, PDT, kill-switch, and
   cross-venue position-correlation.
5. ONE venue-abstraction design recommendation with cutover plan.
6. Per-phase risk-to-existing-system ratings.
7. NO code changes shipped.

## Brain integration (read-only)

- `app/services/trading/venue/coinbase_spot.py` — read.
- `app/services/coinbase_service.py` — read; identify canonical
  vs shim surface.
- `app/services/trading/auto_trader.py` + `auto_trader_monitor.py`
  — read; catalogue RH-implicit calls.
- `app/services/trading/pdt_guard.py` + `portfolio_risk.py` —
  read; venue-aware?
- `app/services/trading/bracket_writer_g2.py` +
  `bracket_reconciliation_service.py` — read; venue-aware?
- `app/services/trading/crypto/exit_monitor.py` — read.
- Fast-path code (`fast_path/executor.py`, etc.) for the existing
  Coinbase patterns.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Operator's directive: don't band-aid; properly integrate.**
  Phase 1 forces architectural rigor in the audit; Phases 2-7
  ship the integration in measured steps with operator approval
  per phase.
- **Operator's directive: don't break what works.** RH autotrader
  + RH crypto reconciler chain (Phases A+B+C) untouched. The 11
  open RH crypto positions and patterns 1011/1016 must continue
  to work exactly as today.
- **NO live Coinbase trades from Phase 1.** Audit only.
- **NO code changes from Phase 1.** Surfacing a fix is Phase 2+
  territory.
- **DO NOT remove tonight's
  `crypto_ticker_unsupported_via_equity_primitive` backstop** for
  RH. The backstop is correct for RH; Coinbase gets a different
  primitive (Phase 4).
- **Edit-tool truncation discipline (HARD).**

## Out of scope (Phase 1 — covered by later phases)

- Coinbase auth verification (Phase 2).
- Broker selection logic (Phase 3).
- Bracket writer Coinbase paths (Phase 4).
- Cost-aware sizing (Phase 5).
- Paper-trade soak (Phase 6).
- Live verification (Phase 7).
- Universe expansion beyond Coinbase (separate brief).
- Multi-broker support beyond RH+Coinbase (separate brief).

## Sequencing (within Phase 1)

1. Truncation scan on the read-target files.
2. **Section A**: capability inventory of Coinbase adapters.
3. **Section B**: autotrader RH-implicit assumption inventory.
4. **Section D**: risk infrastructure audit FIRST among the
   remaining sections (it's the load-bearing one — if cross-venue
   position-correlation is broken, every later phase has to
   account for it).
5. **Section C**: cost economics with real numbers.
6. **Section F**: reconciler + lifecycle.
7. **Section E**: venue-abstraction design recommendation.
8. **Section G**: phase recommendations + scope.
9. Commit + push the audit report.

## Operator-side after Phase 1 ships

1. Read the audit report.
2. Verify the venue-abstraction design proposal makes sense for
   how the operator wants chili to evolve. Reject if it's a
   band-aid disguised as architecture.
3. Approve the next phase brief; CC ships it (typically Phase 2
   = auth verification).
4. Per-phase operator approval continues through Phase 7 (live
   small-size).

## Rollback plan

N/A — Phase 1 is read-only.

## What CC should do if it's unsure

1. **If `coinbase_spot.py` is incomplete relative to needs**,
   document the gaps in the audit report — those become Phase 4
   subtasks. Don't try to extend the adapter from Phase 1.
2. **If the autotrader's RH-assumptions are deeper than expected**
   (>20 sites), surface the cost in the report; Phase 3 may need
   to be split into multiple briefs.
3. **If Coinbase API authentication is broken out of the box**,
   surface the gap; Phase 2 becomes urgent before Phase 3.
4. **If the venue-abstraction design choice has multiple
   reasonable paths**, document all of them but recommend ONE
   with reasoning — operator's "don't band-aid" directive demands
   a clear architectural choice, not a punt.
5. **If Phase 1 surfaces a critical bug** (e.g., the existing
   fast-path Coinbase code already has an architectural mistake
   that would compound in autotrader-Coinbase wiring), surface
   in the report's Section G as a prerequisite-fix brief, but
   DO NOT ship the fix from Phase 1.
