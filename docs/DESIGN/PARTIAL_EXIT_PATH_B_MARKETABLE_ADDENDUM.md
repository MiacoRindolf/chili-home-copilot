# PATH B addendum — make the partial MARKETABLE (2026-09-02)

**Status: DESIGN ADDENDUM. Still no wiring. This branch ships this document plus
one pure module (`path_b_marketable.py`, zero production callers, tripwire-asserted)
and its guard tests.**

Base: `origin/main` `1b345d2`. Amends `docs/DESIGN/PARTIAL_EXIT_PATH_B.md`
(revision 4, branch `seam/partial-exit-path-b-0902`, PR #1291) — specifically
**§3.5 (P4, the POST)** and **§4/D1**. Nothing else in that document changes:
the phase graph, the CAS discipline, `conservation_holds`, §3.7's sibling-first
accounting, S1 and S2 are all untouched.

---

## 0. The operator's question, and the one-line answer

> *"30% ba yung ginagawa ni Ross?"*

**The fraction is his; the shape is not.** Verified claim A1 (deep research
`wf_f31dc5bc`, 21 sources, adversarially verified 3-0,
`memory/reference_ross_exit_discipline.md`): *"Partials INTO strength — sell ~30%
at the recent high; when a stock spikes while I'm holding, I sell into the
spike."* CHILI's shipped default is already `chili_momentum_scale_out_fraction =
0.30` (`app/config.py:5200`). (Note the same memory's claim A5, **refuted 0-3**:
"sell 1/2 at first target" is folklore, not his mechanic. 30% is the verified
number.)

But Ross's verb is *hit the bid*. He does not post an offer above the market and
wait to be lifted. **There is no state in his process called "30% waiting with no
stop."** The shares are sold, the money is in the account, and what remains is
the runner with his stop under it.

PATH B revision 4 posts the partial as a **resting sell limit at the scale-out
target, above the market**, and carves those `f` shares out of the deadman stop
for as long as it rests. That is a state Ross never occupies — and §4/D1 is
correct that it is unfixable *for a resting limit*, because a resting limit is
supposed to rest.

The marketable shape was in fact the original recommendation. The 09-02 probe
that produced PATH B (`project_ws/AgentOps/partial_probe_verdict_20260902.md`,
step 3) said *"Partial MARKETABLE limit sa runner fraction"*. §3.5 turned that
into a resting limit priced at the target. **D1 is a consequence of that drift,
not of the PATCH mechanism.** `grep -n marketable` on the design confirms the
word appears only at §3.9, only for the containment close's `close_shape`.

---

## 1. What actually changes — and what does not

Both variants are **identical up to certification**, and the broker forces it
(§1's own table, AS:3786-3792): the replace is not atomic, the predecessor keeps
resting, and the reservation during the transition is **the larger of the two**,
so the freed `f` is unusable until the replace is terminal.

**PATCH → certify → sell. In both variants. Unchanged.**

The only delta is the shape of the sell at P4:

| | revision 4 (resting) | this addendum (marketable) |
|---|---|---|
| price | `limit` @ scale-out target, **above** the bid | `limit` @ `bid × (1 − cross)`, **at or below** the bid |
| trigger | posted at certification, waits | posted when **`bid >= target`** |
| phase it lives in | `partial_posted`, for the life of the position | `partial_posting`, for one round trip |
| what ends it | the market coming to the offer, or the position dying | the bid |

This is not new code. It is byte-for-byte the shape the lane's **whole-position**
exit already submits (LR:15660-15694): `limit`, `time_in_force='day'`,
`position_intent='sell_to_close'`,
`extended_hours = market_session_now(symbol) != 'regular'`, priced
`bid × (1 − (notional_guard−1) × 8)` in the extended session — with
`chili_momentum_order_notional_guard_bps = 25.0` (LR:19814-19820) that is
**200 bps under the bid**, a *worst acceptable price*, not an expected fill.
(The venue's own `_EXT_HOURS_CROSS_FRAC = 0.015` = 150 bps, AS:3595, fires only
when the caller passes no price, which the chokepoint never does.)

---

## 2. §3.5 replacement text — P4, marketable

> Replaces the paragraph of §3.5 beginning *"The order shape follows the
> chokepoint's own extended-hours rule (LR:15668-15694)"*. **Every fresh
> re-check above that paragraph is unchanged**, including the un-netted
> `open_partial_qty` rule, `verdict.requires_remedy` gating, and the
> `consumed_by_exit` write-ordering rule.

The partial is posted **marketable**, and only when the bid has actually reached
the scale-out target.

**P4a — trigger.** The POST does not fire at certification. It fires on the first
pulse where `partial_trigger_ready(bid=…, target_price=…)` returns `ready`, i.e.
`bid >= target`. The trigger is the **bid**, never the mid and never the ask.
Between certification and the trigger the marker sits in `successor_certified`,
which is already a `NAKED_RISK` phase — but the naked quantity there is **zero**:
no partial has been split off, the `R` stop covers the whole intended remainder
and the `f` shares are still covered by nothing *because they have not been
carved out yet*. **This is the one accounting change the addendum makes to the
phase semantics** and it must be made explicit in `assess_protection`'s caller:
before the POST, `open_partial_qty = 0` and `stop_qty = R < Q`, so
`naked_downside_qty = Q − R = f` is genuine and must still be reported. The
window is real; it is simply *pre-sale* rather than *during-sale*, and it is the
window the operator is being asked to accept in §3 below.

**P4b — price.** `marketable_partial_limit_price(bid=…, extended_hours=…)`.
The limit is rounded **down** to the tick and is asserted `<= bid`. A limit above
the bid is a resting offer, which reinstates D1 in full; the helper cannot
produce one and the test binds it.

**P4c — shape.** `partial_post_request(...)` assembles the body. Do not construct
it at the call site: the same shape drift produced
`tranche_oco_skipped_extended_hours` (40310000 `oco_rth_only`) and the
inert-premarket containment close of R5.

**P4d — the residual.** If the bid falls more than the crossing fraction inside
the round trip, the marketable sell is **left behind** and rests.
`marketable_left_behind(bid_now=…, limit_price=…)` detects it, and the marker
advances `partial_posting → partial_posted` exactly as revision 4 specifies, with
every remedy intact. **The remedy path is not deleted — it is made rare.** That
is the whole claim of this addendum, and it is stated as a claim, not as an
elimination.

---

## 3. §4/D1 replacement text — the exposure, restated with measurements

> Replaces the block quote in D1 beginning *"From certification until the partial
> is terminal…"*. The paragraph after it — *"every post-certification failure
> branch…"* — is unchanged and still applies.

> **From certification until the partial fills, `f` shares carry no downside
> stop.** Under the **resting** shape that span is the remaining life of the
> position. Measured on every `live_partial_exit` in 120 days (n=23; n=17 with a
> booked terminal, n=11 Alpaca): Alpaca **p25 263 s, p50 459 s, p75 3,265 s,
> max 7,167 s**, and **6 of the 23 decisions have no booked terminal at all** —
> including MOVE 2026-08-31, whose exit was never recognised. Under the
> **marketable** shape that span is the order's round trip: `place_rtt_s` on
> Alpaca live, 90 days, n=137 — **p50 0.109 s, p75 0.135 s, p95 0.452 s,
> max 0.860 s** — plus the fill. On a deliberately pessimistic proxy (the entry
> leg, a marketable BUY *chasing* an ask, with cancel-on-first-tick) the
> submit→broker-fill median is 4.38 s, p75 10.6 s. Direct evidence on the sell
> side, 21 days live Alpaca: **11 of 11 CHILI-submitted marketable exits filled**,
> 10 of them extended-hours totalling 3,061 shares — including **CANF 355 sh @
> 4.119915 at 11:11:05Z, premarket**, the very position this design is written
> around.

**The exposure therefore stops being a steady state and becomes a transient.**
That is not the same as removing it, and this document does not claim it is
removed. What changes is which machinery the exposure needs: the 30 s lease, the
300 s ceiling and the whole remedy graph were built to handle *transients*. They
were being asked to supervise a *normal operating state*.

### 3.1 The number that actually decides this — the 300 s ceiling

`MARKER_UNRESOLVED_CEILING_SECONDS = 300.0`, and against the shipped helper:

```
marker_ceiling_forced_target('partial_posted')       -> 'flatten_queued'
marker_ceiling_forced_target('partial_posting')      -> 'flatten_queued'
marker_ceiling_forced_target('successor_certified')  -> 'flatten_queued'
marker_ceiling_forced_target('post_deferred')        -> 'flatten_queued'
marker_ceiling_forced_target('partial_indeterminate')-> 'flatten_queued'
```

i.e. **every phase in the normal PATH B span**, run 2026-09-02 against the
shipped helper at `70805e2`.

`flatten_queued` is `_queue_full_close('path_b_stop_qty_unrestored')` — a
**whole-position** close (§3.8).

**7 of 11 Alpaca first-target decisions (63.6%) were followed by a position that
outlived 300 s** (459, 379, 1,106, 2,563, 3,967, 4,191, 7,167 s), plus 2 more
with no booked terminal. Only 4 of 11 resolve inside the ceiling.

So the design as written says: *in roughly two of every three first-target
partials, if the offer has not been lifted within five minutes, sell the runner
at market.* That is **H4's failure mode — "taking a partial flattens the runner"
— re-entering through the safety valve rather than through a bug.** CANF is
exactly that shape: a 4.6349 limit on a position whose max bid was 4.35 in a 45 s
life; it would have rested unfilled for the entire trade, then tripped the
ceiling.

Under the marketable shape the marker resolves in ~0.1–5 s, the ceiling never
fires in the normal case, and `flatten_queued` goes back to being the exceptional
remedy §3.8 designed it to be.

**This — not the price comparison — is the finding that unblocks D1.** It is a
mechanism argument from the design's own constant and a measured lifetime
distribution, and it does not depend on the thin P&L sample in §4.

---

## 4. Price cost — small, and thin. Read the caveats.

Corpus: `alpaca_scale_out_suppressed_for_deadman` fires at entry carrying the
exact `target_price` PATH B would rest at. In 21 days it fires **17 times against
17 `live_entry_filled`** — complete coverage of the Alpaca live lane. **13
usable**; 3 excluded with unrecoverable terminals (COIW 14842, BDRX 15344,
AEMD 18035 — no exit event of any kind), 1 with no tape inside its 12 s life
(SDOT 14825).

**Fill certainty.** The limit is modelled **generously**: credited with a fill the
first instant `bid >= target`, queue position ignored. Even so:

* target reached **in-life**: 3/13 = 23.1%
* target reached only **after the position was already out**: 9/13 = 69.2%
* fills **only while naked**: 6/13 = 46.2%
* **disagreements between "limit fills" and "marketable triggers": 0 of 13.**

Not one case had the target touched on the ask or mid without the bid following.
So the bid-touch marketable partial does not *beat* the limit on fill certainty —
it **matches it exactly**, while removing the queue risk the model waived. The
dwell of `bid >= target` shows how much that waiver is worth: **AUUD 1.2 s**
(33 prints of 34,676), WETO 9.8 s, UPC 42.9 s, **CANF 99.0 s** (11 prints of
2,897). 23.1% is an *upper bound* for the limit and an *exact figure* for the
marketable variant.

**Price.** On 9 comparable cases, `f` = 30%, in dollars of proceeds on the `f`
shares against the limit's own $2,846.80:

| fill assumption | Δ vs resting limit | as bps |
|---|---|---|
| instant fill at the touched bid | **+$3.23** | **+11.4** |
| worst bid within **1 s** (the API round trip is 0.3–0.8 s) | −$13.70 | −48.1 |
| worst bid within 5 s | −$68.02 | −238.9 |
| worst bid within 15 s | −$80.27 | −282.0 |

**The 1 s row is the binding one.** The 5 s and 15 s rows are *worst bid in the
interval*, not expected fill, and they measure the lane's pre-existing
8.7–15.7 s decision→fill latency, which taxes today's all-or-nothing exit
identically; UPC alone supplies $45.57 of the $68.02 at 5 s.

Against context: median tape spread **69.4 bps**, and the burst study charges
**54 bps** per crossing. **Hitting the bid does not cost meaningfully more than
waiting.**

In R, on the only in-life case with a recorded `stop_distance` (SSM 19315,
`stop_distance` 0.0375, target 4.05, bid-at-touch 4.05): **0.000 R**. A *mid*-touch
trigger instead costs −0.53 R on the `f` shares (−0.16 R at position level) at
zero slippage. **That is the price of the wrong trigger, not the price of being
marketable** — which is why P4a mandates the bid.

**Slippage assumption, justified from the lane's own fills.** `live_exit_filled`
vs the NBBO bid at the fill instant, Alpaca live, n=10 usable (bps):
−485.2, −189.3, −119.8, −25.3, 0, 0, 0, +13.8, +19.1, +45.1 → **median 0.0 bps**,
mean −74.2. The median marketable sell fills **exactly at the bid**. The two large
negatives are CANF 19471 (4.1199 vs a 4.33 bid) and AUUD 19337 (1.0301 vs 1.05) —
**both are stops firing through a collapsing bid, not sells into a rising one.**
Using them as the partial's slippage assumption would be wrong-signed: Ross's
partial fires into strength, where the bid is the thick side.

### 4.1 What the resting limit's apparent advantage is actually buying

Scored post-terminal, the limit "wins" +$206.92 across the 13. All of it comes
from **6 cases that filled only while naked** — and **6 of 6 recovered to the
target**. Measured naked mark-to-market downside across those six: **$25.05**.
Excursions: −0.7%, −1.3%, +1.9%, −2.9%, −3.2%, −0.7%. UPC held naked `f` shares
for **7,528 s (2 h 05 m)** with no downside stop to earn $51.21.

**This corpus contains zero observations of the tail D1 names** — the gap-down
where the `R` stop fires and the `f` shares ride down against an unreachable
limit. So **+$206.92 has no measured downside attached and must not be read as an
expected value.** $25.05 is a lower bound conditioned on survival, not a risk
estimate.

---

## 5. The sequence question — settled by the broker, not by preference

Must the PATCH precede the sell?

**(b) sell first, then PATCH `Q → R`** — the ordering that would have *no* naked
window — is **rejected by Alpaca before a share moves.** `qty_available =
qty − held_for_orders`; with the full-`Q` deadman still resting,
`held_for_orders = Q`, `qty_available = 0`. Four rejections of exactly this shape
are in this account's own live event log (`live_deadman_stop_placed`, 2026-07-13):

```
40310000 insufficient qty available for order (requested: 4578, available: 2289)  TRNR
40310000 insufficient qty available for order (requested: 8114, available: 4057)  SOBR
40310000 insufficient qty available for order (requested: 11325, available: 5663) SOBR
40310000 insufficient qty available for order (requested: 1519,  available: 760)  VEEE
```

`held_for_orders` equals the resting sell quantity in every case. The repo states
it independently at AS:3773, LR:33638-33645 and `paper_execution.py:342-347`, and
`place_protected_partial_oco`'s docstring (AS:3886-3889) says it outright:
placing it under a full-quantity deadman *"will be rejected by the broker for
over-reservation, which is the correct fail-safe outcome, not a bug."* The entire
existence of `alpaca_scale_out_suppressed_for_deadman` (17 firings in 21 days) is
the lane avoiding the order (b) requires.

The design's own checkers agree, run against the shipped helpers at CANF's
numbers (Q=355, f=106, R=249):

```
(b) assess_protection(broker_qty=355, stop_qty=355, open_partial_qty=106)
      -> status='oversell_risk', oversell_ok=False, naked_downside_qty=0.0, ok=False
    plan_replacement_edge(total_qty=355, partial_qty=106,
                          predecessor_filled_size=0, open_partial_qty=355)
      -> SplitPlan(successor_qty=0.0, ok=False, reason='oversell_after_split')

(a) assess_protection(broker_qty=355, stop_qty=249, open_partial_qty=0)
      -> 'naked_downside', naked_downside_qty=106.0, unhedged_with_sell=0.0, ok=False
    assess_protection(broker_qty=355, stop_qty=249, open_partial_qty=106)
      -> 'naked_downside', naked_downside_qty=106.0, unhedged_with_sell=106.0, ok=False
    assess_protection(broker_qty=249, stop_qty=249, open_partial_qty=0)
      -> 'covered', ok=True                                   (after the f fills)
```

(Verbatim from the shipped helpers on `seam/partial-exit-path-b-0902` @ `70805e2`,
run 2026-09-02. All three are keyword-only — a wiring PR that calls them
positionally raises `TypeError`, the #1283 shape.)

Three independent refusals. **There is no "brief oversell risk" to weigh — the
oversell never becomes real, because the order never reaches the book.** And even
absent reservation, §1's replace semantics force (a) anyway.

**Recommendation: (a). PATCH `Q → R`, certify, then submit the MARKETABLE sell
for `f` when the bid reaches the target.** The naked window is the sell's round
trip. That is not a design choice; it is the only legal ordering.

---

## 6. Ross parity — and the three places it still differs

**Matches:** the fraction (0.30, claim A1) and now the mechanic — hit the bid, it
fills, it is gone; the remainder carries the `R` stop from the instant the PATCH
certifies. The same memory's implementation map already said this in item A3: *"a
simple **bid-limit** partial at the MFE-p60 target needs no L2."* Bid-limit, not
offer.

**Still differs, three ways, all real:**

1. **The deadman exists because the machine can die; Ross's protection is his
   attention.** `place_deadman_stop`'s docstring names the incident
   (AS:3682-3689): 2026-07-10, GMM, TCP ephemeral-port exhaustion — the worker
   was alive but could not reach Alpaca, and GMM collapsed unprotected. Ross needs
   no resting broker stop on his runner because he is sitting there. CHILI does,
   because it has demonstrated it can be alive and blind. **That is why even the
   marketable variant must shrink the stop *first* rather than cancel it:** the
   runner is never uncovered — only the `f` being sold, and only for the round
   trip.
2. **The trigger is not "the recent high."** Ross's ~30% fires at a discretionary
   structural level. CHILI's first target is an R-multiple / MFE-percentile
   computation (`stop_target_prices`), and several of the 17 targets land on round
   numbers. Same fraction, different reason to fire. Changing the trigger to a
   structural high is a **separate** question and is not measured here.
3. **Wiring PATH B silently changes the target price of every Alpaca trade.**
   `stop_target_prices(..., partial_capable = family not in ALPACA_EXECUTION_FAMILIES)`
   (LR:33642-33645) passes `False` for Alpaca today, disabling
   `round_number_first_scale_target`. Making Alpaca partial-capable flips it to
   `True` — and the code's own comment (`paper_execution.py:342-351`) records what
   that pull-in did to SSM on 09-01: entry 4.01, stop 3.9725, 2R target 4.085
   pulled in to 4.05 = **1.07R**, against a 5-trade backtest of 1.5R = −1.67 /
   2.0R = +13.62 / 2.5R = +30.86. **That flip must be a deliberate, separately
   measured part of any wiring PR, not a side effect**, and it interacts with
   shipped #1271 (R:R 2.0 → 2.5). On the evidence available this coupling is a
   larger P&L lever than marketable-vs-resting.

---

## 7. What this unblocks, and what it does not

**Unblocked:**

* **D1 loses its character.** The exposure stops being *"the whole life of the
  limit, the normal case"* and becomes *"the sell's round trip, plus a rare
  left-behind tail with a bounded remedy."* Operator acceptance in writing (§4
  item 3 of the unblock list) is **still owed** — but for a ~0.1–5 s transient
  with a measured p95 submit of 0.452 s, not a 459 s median steady state.
* The 300 s ceiling stops being a runner-flattener in the majority case (7/11).
* `partial_posted` stops being the phase the marker normally lives in.

**NOT removed — do not claim otherwise in any wiring PR:**

* **S1** — marker-sourced successor envelope + both dispatch gates. Untouched.
  Still its own PR, still first.
* **S2** — chokepoint-head sibling reconcile. Still required: `partial_posting`
  still exists and `_cancel_scale_limit_and_clamp` is still a silent noop while it
  does. What changes is the **duration** of the exposure it guards — round trip
  instead of trade life. Code shape identical; risk shrinks ~3 orders of magnitude.
* **D2** (transactional CAS untestable DB-free), **D3** (blast radius on the
  protection path), **D4** (source-order placement untested) — unchanged.
* The `partial_capable` coupling in §6/3 — new, and it is a blocker of its own.
* The crossing-fraction choice premarket (200 bps chokepoint vs 150 bps venue)
  against measured premarket spreads of 65–330 bps on low-float movers
  (AS:3589-3594).

**Revised unblock order:** S1 → S2 → the `partial_capable` measurement → operator
acceptance of the *round-trip* D1 exposure → DB-backed CAS test → marker-free
refactor → PAPER soak. Still four-plus PRs. Still not one.

---

## 8. Sample honesty

Every number above is labelled where it is thin, because it is thin:

* **n=13** usable cases; **only 3** reach the target in-life. Enough to say the
  marketable variant is not *expensive*; **not** enough to rank the variants on
  P&L.
* The R figure rests on **n=1** (SSM 19315).
* The naked-fill economics rest on **n=6**, all of which recovered. **No
  observation of the D1 tail exists in this sample.**
* Three sessions were excluded as unresolvable — and one of them, **BDRX 15344,
  is the known +4.0R round-trip that gave it all back.** That exclusion is *not*
  neutral: it drops the single most informative "target reached then surrendered"
  case in the window.
* The Alpaca-era first-target cohort is **n=2** (MOVE, SSM). The n=11
  decision→terminal distribution is dominated by June/July trades under a slower
  exit stack; today's cohort has a p50 whole-trade life of 90.4 s. Both are
  reported.
* The submit→fill leg of the marketable round trip is measured on the **entry**
  side (a marketable BUY chasing an ask) because **no exit-side submit timestamp
  exists anywhere in the record**: `trading_order_state_log` is empty for the
  window, `broker_symbol_action_claims` carries only `action='entry'`, and exactly
  1 `live_exit_submitted` event was emitted in 21 days. It is a pessimistic proxy,
  and it is a proxy.
* The **968-case burst corpus could not be queried** — it was never persisted, and
  regenerating it needs an unbounded scan of the 61 GB tape. Its published
  constants (53.9 bps median spread, 54 bps per crossing, 8.7–15.7 s
  decision→fill) are cited, not re-measured.

**Because the P&L sample is thin, the recommendation does not rest on it.** It
rests on §3.1 (the 300 s ceiling against a measured lifetime distribution), §5
(a broker refusal with four logged instances), and §4's *zero disagreements*
between the two trigger conditions. Those are mechanism, and they are the parts
that would survive a larger sample.

---

## 9. What this branch ships

* This document.
* `app/services/trading/momentum_neural/path_b_marketable.py` — pure, no I/O, no
  settings read, no clock. Four total functions: `partial_trigger_ready`,
  `marketable_partial_limit_price`, `partial_post_request`,
  `marketable_left_behind`. Every one returns a verdict on unreadable input rather
  than raising (revision-4 defect #10).
* `tests/test_path_b_marketable_partial.py` — binds the bid trigger, binds
  *limit ≤ bid* at every price scale, binds the 200 bps extended-hours crossing to
  the chokepoint's own constant, binds totality, and asserts **zero production
  callers** with a tripwire that fails if it scanned nothing (the revision-1 L1
  defect).

No wiring. `replace_order_qty` still has zero production callers.
