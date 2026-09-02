# PATH B — the MARKETABLE partial, on a DEFERRED PATCH (2026-09-02, revision 2)

**Status: DESIGN. No wiring. This branch ships this document, one pure module
(`path_b_marketable.py`, zero production callers, tripwire-asserted), its guard
tests, and the research script + frozen evidence that reproduce every number
below.**

Base: `origin/main` `1b345d2`. Amends `docs/DESIGN/PARTIAL_EXIT_PATH_B.md`
(revision 4, branch `seam/partial-exit-path-b-0902`, PR #1291) — **§3.3 (when the
decision site fires)**, **§3.5 (P4)**, and **§4/D1**. The phase graph, the CAS
discipline, `conservation_holds`, §3.7's sibling-first accounting, S1 and S2 are
untouched.

> **Revision 2 supersedes revision 1 of this document (commit `c54eff3`), which
> was refuted.** Revision 1 deferred only the POST and left the PATCH where
> revision 4 puts it — at the entry fill. It then claimed the naked window
> collapsed to a 0.109 s round trip. **That claim was false, and this document
> retracts it.** §2 of revision 1 said so itself, in contradiction with its own
> §3 and its commit subject. What follows is the corrected sequence, the
> corrected price population, and a materially weaker price argument.

---

## 0. The operator's question, and the one-line answer

> *"30% ba yung ginagawa ni Ross?"*

**The fraction is his; the shape is not, and neither is the timing.** Verified
claim A1 (deep research `wf_f31dc5bc`, 21 sources, adversarially verified 3-0,
`memory/reference_ross_exit_discipline.md`): *"Partials INTO strength — sell
~30% at the recent high; when a stock spikes while I'm holding, I sell into the
spike."* CHILI's shipped default is already
`chili_momentum_scale_out_fraction = 0.30` (`app/config.py:5200`). (The same
memory's claim A5 is **refuted 0-3**: "sell 1/2 at first target" is folklore.
30 % is the verified number.)

Ross's verb is *hit the bid*, and he does it **when the spike happens** — not
before. PATH B revision 4 does neither: it posts a resting offer **above** the
market, and it does so **at the entry fill**, carving `f` shares out of the
deadman stop before it knows whether the bid will ever reach the target.

---

## 1. What actually changes — the correction

**Revision 1 of this document got this wrong and the error was load-bearing.**
It asserted:

> *"PATCH → certify → sell. In both variants. Unchanged."*

That is false as a description of the fix it was proposing. Revision 4's
decision site executes **P0–P2 only** (§3.3) — and P2 *is* the PATCH. The
decision site fires at the entry fill:

```
live_runner.py:33724
    # Sell INTO strength: rest the scale-out limit AT the target now,
    # while the move is paying the level (fail-open -> reactive path).
    _place_scale_out_limit(
        db, sess, adapter, le=le, product_id=product_id,
        target_px=float(target_px), filled=float(filled), prod=prod,
    )
```

immediately after the entry fill is booked and before
`_safe_transition(db, sess, STATE_LIVE_ENTERED)`. This is also why
`alpaca_scale_out_suppressed_for_deadman` fires once per entry, and why the
design can rest a 4.6349 limit on CANF while the market is at 4.2.

So deferring only the POST leaves `f` carved out of the stop **from the entry
fill onward**, and the naked span is `certification → bid reaches T → round
trip`. Measured on this study's own window-A rows (`ttf_s`, entry-relative):
**24.5 s, 103.4 s, 119.5 s** for the three that fire, and **unbounded** for the
ten that never do. Not 0.109 s. `place_rtt_s` measures only the last hop.

### The corrected sequence

**Move the gate, not just the shape. The PATCH fires at the trigger.**

| | revision 4 (resting) | revision 1 (refuted) | **this document** |
|---|---|---|---|
| PATCH `Q→R` fires | at the entry fill | at the entry fill | **when `bid >= target`** |
| POST fires | at certification | when `bid >= target` | immediately after the PATCH certifies |
| sell shape | `limit` @ target, **above** the bid | `limit` @ `bid×(1−cross)` | `limit` @ `bid×(1−cross)` |
| `f` carved out of the stop | entry → terminal | entry → fill | **PATCH RTT + sell RTT** |
| cases where the stop is touched at all | 13/13 | 13/13 | **3/13** |

Pre-trigger there is **no marker at all**: the deadman rests at the full `Q`,
`naked_downside_qty = 0`, and nothing in PATH B has run. That is the property
revision 1 could not have.

### The phase graph is unchanged — verified

A deferred PATCH needs **no new phase**, because pre-trigger no marker exists.
The marker is created at the trigger and runs `intent_frozen →
replace_submitted → successor_certified → partial_posting → partial_filled`
exactly as revision 4 specifies. The one new question — *the bid retreats inside
the PATCH's own round trip, so `f` is naked and the trigger is gone* — is
already representable: revision 4's `post_deferred → restore_intent_frozen →
restore_replace_submitted → restore_certified(T)` **is** the PATCH-back `R→Q`.
`post_patch_sell_decision()` in the pure module returns exactly that trichotomy
(`sell` / `hold` / `restore`) and fails **safe to `restore`** on an unreadable
quote or an untrustworthy age.

**What the lease and the ceiling now measure from.** Both the 30 s
owner-transport lease and `MARKER_UNRESOLVED_CEILING_SECONDS = 300.0` are keyed
to marker age. Under revision 4 the marker is born at the entry fill, so they
measure from entry. **Under this document the marker is born at the trigger**,
so they measure from the trigger — which is what they were designed for
(§3.1 below is entirely a consequence of this).

---

## 2. §3.3 / §3.5 replacement text

### P0-gate (new) — the decision site does not fire at the entry fill

`_place_scale_out_limit`'s call site moves from LR:33724 (post-entry-fill) into
the maintenance pulse, gated on
`partial_trigger_ready(bid=…, target_price=…)["ready"]`, i.e. `bid >= target`.
The trigger is the **bid**, never the mid and never the ask. Until it fires,
**no PATCH, no marker, no phase, and the deadman covers the full `Q`.**

This costs one pulse of detection latency, and that is a real cost: a spike
whose `bid >= target` dwell is shorter than the pulse interval is missed
entirely, where a resting offer would have been lifted. Measured dwell of
`bid >= target` in this corpus: AUUD **1.2 s** (33 prints of 34,676), WETO 9.8 s,
UPC 42.9 s, CANF 99.0 s, SSM 185.2 s, GYGY 197.4 s, MOVE 325.5 s, LIDR 1,216 s,
XPON 8,783 s; RDHL, LIDR-19415, JLHL, CDTG never. The three window-A cases
dwell **185 s / 326 s / 8,783 s** — far longer than any pulse — but AUUD's 1.2 s
shows the tail is real. **This is the one advantage the resting limit
genuinely has and it is not measured away.**

### P4a — trigger and PATCH

On the first pulse where `partial_trigger_ready` returns `ready`: run P0–P2
(plan, freeze, PATCH `Q→R`), then certify. `f` becomes naked at the instant the
PATCH certifies, not before.

### P4b — the post-PATCH re-check (new, and it is the whole safety argument)

The PATCH has its own round trip (**200–260 ms**; the 09-01 probe recorded
200/254 ms on a `status=new` stop). The bid can retreat inside it. Immediately
after certification, before the POST:

```
post_patch_sell_decision(bid_now=…, target_price=…, naked_age_s=…)
    -> 'sell'     bid still >= target: POST the marketable sell
    -> 'hold'     retreated, within grace: re-check next pulse
    -> 'restore'  retreated past grace, OR unreadable quote, OR unreadable age:
                  take post_deferred -> restore_intent_frozen (PATCH R -> Q)
```

`DEFAULT_POST_PATCH_GRACE_SECONDS = 2.0` **is the only naked window in this
design**, and it is an **operator knob this study did not measure**. It is
stated as a knob, not as a finding. `test_the_only_naked_window_is_bounded_by_grace`
binds the property that past the grace no input returns `sell` or `hold` while
the bid is below the target.

### P4c — price and shape

`marketable_partial_limit_price(bid=…, extended_hours=…)`, rounded **down** to
the tick and asserted `<= bid`. A limit above the bid is a resting offer, which
reinstates D1 in full; the helper cannot produce one and the test binds it.
`partial_post_request(...)` assembles the body — byte-for-byte the shape the
whole-position exit already submits (LR:15660-15694): `limit`,
`time_in_force='day'`, `position_intent='sell_to_close'`,
`extended_hours = market_session_now(symbol) != 'regular'`, priced
`bid × (1 − (notional_guard−1) × 8)`. With
`chili_momentum_order_notional_guard_bps = 25.0` (LR:19814-19820) that is
**200 bps under the bid** — a *worst acceptable price*, not an expected fill.
(The venue's `_EXT_HOURS_CROSS_FRAC = 0.015` = 150 bps, AS:3595, fires only when
the caller passes no price, which the chokepoint never does.)

### P4d — the residual

If the bid falls more than the crossing fraction inside the sell's round trip,
the marketable sell is **left behind** and rests.
`marketable_left_behind(bid_now=…, limit_price=…)` detects it and the marker
advances `partial_posting → partial_posted` exactly as revision 4 specifies,
with every remedy intact. **The remedy path is not deleted — it is made rare.**

---

## 3. §4/D1 replacement text — the exposure, restated

> Replaces the block quote in D1 beginning *"From certification until the partial
> is terminal…"*. The paragraph after it is unchanged and still applies.

> **From the PATCH certifying until the partial fills, `f` shares carry no
> downside stop.** Under the **resting** shape (revision 4) that span begins at
> the **entry fill** and runs to the position's terminal: measured over the 13
> usable cases, **16,505 s of total naked exposure across 13/13 cases** (mean
> 1,269.6 s; ex-XPON 3,318.9 s over 12 cases, mean 276.6 s). Under the
> **deferred-PATCH marketable** shape the stop is touched in **3 of 13 cases**,
> for **PATCH RTT + sell RTT** each — p95 0.260 s + p95 0.452 s = **0.712 s** —
> for **2.1 s of total naked exposure**, a **~7,700× reduction** (~1,550×
> excluding the XPON outlier), plus a `grace_seconds` bound on the retreat case.
> In **10 of 13 cases the deferred design never touches the stop at all.**

**Direct evidence that the marketable sell fills.** 21 days, live Alpaca: **11 of
11 CHILI-submitted software exits filled** (5 stop, 4 bailout, 1 trail_stop,
1 operator_flatten), 10 of them extended-hours totalling 3,061 shares —
including **CANF 355 sh @ 4.119915 at 11:11:05Z, premarket**, the very position
this design is written around. `place_rtt_s` (`live_entry_submitted`, Alpaca
live, 90 d, n=137): p50 **0.109 s**, p75 0.135, p95 0.452, max 0.860.

**Honest limit on that number:** `place_rtt_s` is the *submit* round trip only.
No exit-side submit timestamp exists anywhere in the record
(`trading_order_state_log` empty for the window, `broker_symbol_action_claims`
carries only `action='entry'`, exactly 1 `live_exit_submitted` event in 21 days),
so submit→fill is proxied from the **entry** leg — a marketable BUY chasing an
ask, with cancel-on-first-tick abandonment — at median 4.38 s, p75 10.6 s. That
proxy is deliberately pessimistic and it is still a proxy.

### 3.1 The 300 s ceiling — and it depends on the deferred PATCH

`MARKER_UNRESOLVED_CEILING_SECONDS = 300.0`, and against the shipped helper at
`70805e2`:

```
marker_ceiling_forced_target('partial_posted')       -> 'flatten_queued'
marker_ceiling_forced_target('partial_posting')      -> 'flatten_queued'
marker_ceiling_forced_target('successor_certified')  -> 'flatten_queued'
marker_ceiling_forced_target('post_deferred')        -> 'flatten_queued'
marker_ceiling_forced_target('partial_indeterminate')-> 'flatten_queued'
```

i.e. **every phase in the normal PATH B span**. `flatten_queued` is
`_queue_full_close('path_b_stop_qty_unrestored')` — a **whole-position** close
(§3.8).

**7 of 11 Alpaca first-target decisions (63.6 %) were followed by a position that
outlived 300 s** (459, 379, 1,106, 2,563, 3,967, 4,191, 7,167 s), plus 2 more
with no booked terminal. Only 4 of 11 resolve inside the ceiling. So revision 4
says: *in roughly two of every three first-target partials, if the offer has not
been lifted within five minutes, sell the runner at market* — **H4's failure
mode re-entering through the safety valve rather than through a bug.**

**This finding depends on the deferred PATCH, not on the marketable shape
alone.** Under revision 1 of this document the marker was still born at the entry
fill and still sat in `successor_certified` for the whole trigger wait, and
`marker_ceiling_forced_target('successor_certified') -> 'flatten_queued'` — so
the ceiling still fired on the same 7/11. Only when the marker is **born at the
trigger** does its age become the round trip, the ceiling stop firing in the
normal case, and `flatten_queued` go back to being the exceptional remedy §3.8
designed it to be.

**This — not the price comparison — is what unblocks D1.**

---

## 4. Price cost — the corrected population, and it favours the LIMIT

> Revision 1 reported **+11.4 bps** at instant fill and **−48.1 bps** at the 1 s
> bound over **nine** rows. Six of those nine were cases where the bid reached
> the target **only after the position had already exited**. The marketable
> variant **cannot fire there** — the position is closed, there is nothing to
> sell — so those rows credited it with a trade it never makes. Both headline
> numbers were wrong, in the direction that flattered the recommendation.

Reproduce with `python scripts/research/path_b_marketable_price_cost.py`
(DB-free; reads the frozen evidence in `scripts/research/data/`).

**Population**, n=13 usable of 17 `alpaca_scale_out_suppressed_for_deadman`
firings against 17 `live_entry_filled` (complete coverage of the Alpaca live
lane, 21 days): **window A n=3** (bid reached T in-life — both variants act),
**window B n=6** (bid reached T only post-terminal — only the limit acts),
**neither n=4** (byte-identical, delta exactly $0).

### 4.1 Window A — the only rows where the marketable variant exists

Resting-limit proceeds on the `f` shares: **$858.00**, modelled **generously**
(credited with a fill the first instant `bid >= target`, queue position waived).

| fill assumption | Δ vs resting limit | as bps |
|---|---|---|
| instant fill at the touched bid | **+$0.24** | **+2.8** |
| worst bid within **1 s** (round trip is 0.3–0.8 s) | −$7.20 | **−83.9** |
| worst bid within 5 s | −$13.62 | −158.7 |
| worst bid within 15 s | −$13.62 | −158.7 |

**The 1 s row is binding, and −83.9 bps EXCEEDS the 69.4 bps median tape
spread.** Revision 1 argued the cost sat "inside one spread". On the corrected
population it does not — it is **~1.2 spreads**. The 5 s / 15 s rows are *worst
bid in the interval*, not expected fill, and they measure the lane's pre-existing
8.7–15.7 s decision→fill latency, which taxes today's all-or-nothing exit
identically.

**Slippage sensitivity** (revision 1 hardcoded 0.0 bps and published no ladder):

| slippage | Δ $ | bps |
|---|---|---|
| 0.0 (measured **median**) | +$0.24 | +2.8 |
| **2.8** | −$0.00 | **−0.0 ← break-even** |
| 35.0 (half spread) | −$2.76 | −32.2 |
| 54.0 (burst study crossing) | −$4.39 | −51.2 |
| **74.2 (measured mean)** | −$6.13 | **−71.4** |
| 150.0 (venue ext-hours cross) | −$12.63 | −147.2 |

**The instant-fill advantage is +2.8 bps and it goes negative at 2.8 bps of
slippage.** It is razor thin, not a margin.

Measured slippage, `live_exit_filled` vs the NBBO bid at the fill instant,
Alpaca live, n=10 (bps): −485.2, −189.3, −119.8, −25.3, 0, 0, 0, +13.8, +19.1,
+45.1 → **median 0.0, mean −74.2**. The two large negatives are CANF 19471
(4.1199 vs a 4.33 bid) and AUUD 19337 (1.0301 vs 1.05) — **both are stops firing
through a collapsing bid, not sells into a rising one**, so the mean is
wrong-signed as an assumption for a partial that fires into strength. Both rows
are published anyway; revision 1 published only the median.

In R, on the only in-life case with a recorded `stop_distance` (SSM 19315,
`stop_distance` 0.0375, target 4.05, bid-at-touch 4.05): **0.000 R**. A *mid*-touch
trigger instead costs −0.53 R on the `f` shares (−0.16 R at position level) at
zero slippage — **the price of the wrong trigger, not of being marketable**,
which is why P4a mandates the bid. **n=1.**

### 4.2 Window B — the marketable variant cannot fire

The bid reached T only after the position was out. The limit fills there **while
`f` is naked**, the `R` stop having already taken the runner. The marketable
variant never fires; `f` leaves with the position at the terminal exit price.

| | limit $ | marketable $ | Δ |
|---|---|---|---|
| GYGY 19261 | 262.50 | 241.50 | −21.00 |
| WETO 19299 | 424.00 | 405.45 | −18.55 |
| AUUD 19337 | 189.75 | 169.97 | −19.78 |
| LIDR 19394 | 77.48 | 72.78 | −4.70 |
| UPC 19457 | 543.77 | 492.56 | −51.21 |
| CANF 19471 | 491.30 | 436.71 | −54.58 |
| **TOTAL** | **1,988.80** | **1,818.97** | **−169.83** |

### 4.3 The honest bottom line on price

| | Δ |
|---|---|
| window A, instant fill | +$0.24 |
| window A, 1 s bound | −$7.20 |
| window B | −$169.83 |
| neither (n=4) | $0.00 |
| **TOTAL, instant fill** | **−$169.59** |
| **TOTAL, 1 s bound** | **−$177.03** |

**On price, over this sample, the resting limit wins, and it is not close.**
Revision 1's framing ("hitting the bid does not cost meaningfully more than
waiting") is withdrawn.

**What that $169.83 is actually buying.** Every dollar of it is earned by holding
`f` with **no downside stop** until the market came back, in six cases where
**6 of 6 recovered**. Measured naked mark-to-market downside across those six:
**$25.05** (excursions −0.7 %, −1.3 %, +1.9 %, −2.9 %, −3.2 %, −0.7 %). UPC held
naked `f` shares for **7,528 s (2 h 05 m)** to earn $51.21. **This corpus
contains zero observations of the D1 tail** — the gap-down where the `R` stop
fires and `f` rides down against an unreachable limit. So $169.83 has no measured
downside attached and **must not be read as an expected value**; $25.05 is a
lower bound conditioned on survival, not a risk estimate.

**The recommendation therefore does not rest on price. It rests on §3.1 and §5.**

---

## 5. The sequence question — settled by the broker, not by preference

**(b) sell first, then PATCH `Q→R`** — the ordering that would have *no* naked
window — is **rejected by Alpaca before a share moves.**
`qty_available = qty − held_for_orders`; with the full-`Q` deadman still resting,
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
over-reservation, which is the correct fail-safe outcome, not a bug."*

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
    assess_protection(broker_qty=249, stop_qty=249, open_partial_qty=0)
      -> 'covered', ok=True                                   (after the f fills)
```

(Verbatim, run 2026-09-02 at `70805e2`. All three are keyword-only — a wiring PR
calling them positionally raises `TypeError`, the #1283 shape.)

Three independent refusals. **There is no "brief oversell risk" to weigh — the
oversell never becomes real, because the order never reaches the book.** And even
absent reservation, §1's replace semantics force (a) anyway: the replace is not
atomic and the reservation during the transition is the larger of the two.

**Recommendation: (a), on a deferred trigger. When `bid >= target`: PATCH `Q→R`,
certify, re-check the bid, then submit the MARKETABLE sell for `f`.** The naked
window is PATCH RTT + sell RTT, bounded on retreat by `grace_seconds`.

---

## 6. Ross parity

**Matches:** the fraction (0.30, claim A1); the mechanic — hit the bid, it fills,
it is gone; and now the **timing** — the stop is not touched until the spike
actually happens. The same memory's implementation map already said this in item
A3: *"a simple **bid-limit** partial at the MFE-p60 target needs no L2."*
Bid-limit, not offer.

Revision 1 claimed parity while still PATCHing at entry. **That claim was false
for the same reason its window claim was false:** between the PATCH certifying
and the sell firing, `f` was carved out and uncovered for the whole trigger wait.
Ross does not occupy that state either. Only the deferred PATCH achieves the
parity this document claims.

**Still differs, three ways, all real:**

1. **The deadman exists because the machine can die; Ross's protection is his
   attention.** `place_deadman_stop`'s docstring names the incident
   (AS:3682-3689): 2026-07-10, GMM, TCP ephemeral-port exhaustion — the worker
   was alive but could not reach Alpaca, and GMM collapsed unprotected. Ross
   needs no resting broker stop on his runner because he is sitting there. CHILI
   does, because it has demonstrated it can be alive and blind. **That is why
   even this variant must shrink the stop *first* rather than cancel it, and why
   the retreat case restores rather than waits.**
2. **The trigger is not "the recent high."** Ross's ~30 % fires at a
   discretionary structural level. CHILI's first target is
   `entry + target_r × stop_distance` (`stop_target_prices`), and several of the
   17 targets land on round numbers (18.00, 10.00, 8.50, 17.00, 8.00, 1.50).
   Same fraction, different reason to fire. Changing the trigger to a structural
   high would change both fill rate and price cost and is **not measured here.**
3. **Wiring PATH B silently changes the target price of every Alpaca trade.**
   `stop_target_prices(..., partial_capable = family not in ALPACA_EXECUTION_FAMILIES)`
   (LR:33642-33645) passes `False` for Alpaca today, disabling
   `round_number_first_scale_target`. Making Alpaca partial-capable flips it to
   `True` — and the code's own comment (`paper_execution.py:342-351`) records what
   that pull-in did to SSM on 09-01: entry 4.01, stop 3.9725, 2R target 4.085
   pulled in to 4.05 = **1.07R**, against a 5-trade backtest of 1.5R = −1.67 /
   2.0R = +13.62 / 2.5R = +30.86. It also interacts with shipped #1271
   (R:R 2.0 → 2.5). **On the available evidence this coupling is a larger P&L
   lever than marketable-vs-resting** and must be measured on its own.

---

## 7. What this unblocks, and what it does not

**Unblocked — on the D1 axis only:**

* **D1 loses its character.** The exposure stops being *"the whole life of the
  position, in every case"* and becomes *"PATCH RTT + sell RTT, in 3 cases of
  13, bounded on retreat by `grace_seconds`."* Operator acceptance in writing is
  **still owed** — now for a sub-second transient plus a 2.0 s grace knob, not a
  4.58-hour aggregate.
* The 300 s ceiling stops being a runner-flattener in the majority case (7/11) —
  **because the marker is now born at the trigger**, not because the sell is
  marketable.
* `partial_posted` stops being the phase the marker normally lives in.

**NOT removed — do not claim otherwise in any wiring PR:**

* **S1** — marker-sourced successor envelope + both dispatch gates. Untouched.
  Still its own PR, still first.
* **S2** — chokepoint-head sibling reconcile. Still required: `partial_posting`
  still exists and `_cancel_scale_limit_and_clamp` is still a silent noop while
  it does. Only the exposure **duration** shrinks; the code shape is identical.
* **D2** (transactional CAS untestable DB-free), **D3** (blast radius on the
  protection path), **D4** (source-order placement untested) — unchanged.
* **The `partial_capable` coupling** in §6/3 — a blocker of its own.
* **`DEFAULT_POST_PATCH_GRACE_SECONDS`** — an unmeasured operator knob, and the
  only naked window this design has.
* **Missed-spike cost of the deferred gate** (§2 P0-gate) — a resting offer can
  be lifted during a dwell shorter than the pulse interval. AUUD's 1.2 s dwell
  shows the tail is real; it is **not** measured away.

**Revised unblock order:** S1 → S2 → the `partial_capable` measurement → a
pulse-cadence / missed-spike measurement for the P0 gate → operator acceptance of
the round-trip D1 exposure and the grace knob → DB-backed CAS test → marker-free
refactor → PAPER soak. Still four-plus PRs. Still not one.

---

## 8. Sample honesty

* **n=13** usable; **only 3** in window A. The price comparison rests on **n=3**,
  the R figure on **n=1** (SSM 19315), the naked-fill economics on **n=6** in
  which 6 of 6 recovered. **No observation of the D1 tail exists in this sample.**
* Three sessions were excluded as unresolvable — and one, **BDRX 15344, is the
  known +4.0R round-trip that gave it all back.** That exclusion is **not
  neutral**: it drops the single most informative "target reached then
  surrendered" case in the window.
* The Alpaca-era first-target cohort is **n=2** (MOVE, SSM). The n=11
  decision→terminal distribution is dominated by June/July trades under a slower
  exit stack; today's cohort has a p50 whole-trade life of 90.4 s. Both reported.
* The submit→fill leg is measured on the **entry** side because no exit-side
  submit timestamp exists anywhere in the record. Pessimistic, and a proxy.
* The **968-case burst corpus could not be queried** — never persisted, and
  regenerating it needs an unbounded scan of the 61 GB tape. Its published
  constants (53.9 bps median spread, 54 bps per crossing, 8.7–15.7 s
  decision→fill) are cited, not re-measured.

**A non-finding, demoted.** Revision 1 listed *"0 of 13 disagreements between the
limit's fill model and the marketable trigger"* as one of three mechanism
findings. It is an **identity, not a measurement**: both predicates are literally
`bid >= target`, so zero disagreements is guaranteed by construction and would
hold on any sample, including an empty one. It is removed from the evidence list.
The informative version — how often the ask or mid touched T without the bid
following — is a different comparison against the mid-trigger set, and the
window-A rows show the ask leading the bid by 35.2 / 52.7 / 49.3 bps at the
trigger instant, which is exactly the half-spread the mid-trigger variant would
have sold into.

**Because the price evidence now favours the limit, the recommendation rests
entirely on mechanism:** §3.1 (the 300 s ceiling against a measured lifetime
distribution, under a marker born at the trigger), §5 (a broker refusal with four
logged instances that makes the alternative sequence impossible rather than
merely worse), and the exposure arithmetic in §3 (16,505 s → 2.1 s; the stop
untouched in 10 of 13 cases). Those are the parts that survive a larger sample.
Price is not, and this document no longer claims it.

---

## 9. What this branch ships

* This document.
* `app/services/trading/momentum_neural/path_b_marketable.py` — pure, no I/O, no
  settings read, no clock. Five total functions: `partial_trigger_ready`,
  **`post_patch_sell_decision`**, `marketable_partial_limit_price`,
  `partial_post_request`, `marketable_left_behind`. Every one returns a verdict
  on unreadable input rather than raising (revision-4 defect #10), and
  `post_patch_sell_decision` fails **safe to `restore`**.
* `tests/test_path_b_marketable_partial.py` — 33 tests. Binds the bid trigger;
  binds *limit ≤ bid* at every price scale in both sessions; binds the 200 bps
  extended-hours crossing to the chokepoint's own constant; binds the retreat
  trichotomy and its fail-safe; binds the grace bound as a property; binds
  totality; and asserts **zero production callers** with a tripwire that fails if
  it scanned nothing.
* `scripts/research/path_b_marketable_price_cost.py` + `scripts/research/data/` —
  DB-free reproduction of every number in §3 and §4.

No wiring. `replace_order_qty` still has zero production callers.
