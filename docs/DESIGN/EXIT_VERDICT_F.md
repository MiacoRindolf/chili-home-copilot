# EXIT VERDICT F — the tape answers the exit, in prints since the leg's own high

Planner: [44] / [21] / [47]. Shipped 2026-09-10 (this PR). Build A of the judge-merged spec of the same
date; §1, §4, §6, §10 of that spec are reproduced here as the design of record, with the two places the
code proved the spec wrong marked **DEVIATION**.

## 0. Doctrine and the measurement

- **The tick always answers.** Exits are decided on PRINTS, never on a wall-clock bar or a quote-mid bar.
- **Mechanism, not binary.** Chart = boundary (sell all). Tape = moment (sell PART). Reclaim = come back.
- **No magic numbers, no dark flags.** Every value is a named derivation reported in the receipt; the
  machine is LIVE + ON; the only fallbacks are named ones.

7 days, 35 opinion-exit legs since 2026-09-03, **the tick-by-tick harness of record**
(`scratchpad/acceptance_exit_verdict_f_0910.py`: the SHIPPED `exit_verdict.py`, the verdict at every 3.19-s
tick = the measured p50 HELD spacing, the deadman walked per print, exits priced at the NBBO bid, N = 255):

| rule | P&L of the same legs |
|---|---|
| actual (what the lane did) | −$697.87 |
| D = whole on the first since-high print verdict (bid) | −$502.18 (print-priced −$380.82) |
| F(11/31) = part on D, runner under the tick deadman + D2 | −$485.50 |
| F(0.5) | −$489.25 |
| **F(0.75) — the shipped fraction**, by linearity `q·D + (1−q)·R`, R = −$476.32 | **−$495.7** |

The shipped 0.75 is the worst of the three by $6–$10 — inside noise on 35 legs; the linear form has no interior
optimum (q → 0 = R = −$476.32) while the hit-rate rule says q > 0.5 (§7). The designer's in-memory STEP = 100
print-priced re-run (D −$232.62 / F(0.5) −$152.79 / F(11/31) −$129.60) is SUPERSEDED: bid pricing ($121 of spread
the print-priced harnesses never paid), tick-by-tick evaluation and the per-print deadman explain the gap; it is
cited in no receipt. The brief's 34-leg print-priced scripts: actual −$690.79 → D −$297.77 → F −$209. 16 of 34
opinion exits fired at n = 0 prints since the leg's high — the name was AT its own high print when we left. The
tick deadman beat the ATR deadman −$304.93 vs −$468.88 on the same legs.

## 1. The two facts that shape the design (judged in code)

**Fact 1 — the chokepoint's deadman handoff is a WHOLE-close protocol.** `_release_deadman_at_literal_submit`
(nested in `_submit_live_market_exit_impl`) runs for every Alpaca exit while `le["deadman_stop"]` rests. Phase 1
freezes a successor intent for `requested_quantity` and returns `deferred/pre_place_blocked`; phase 2 cancels
the stop, accounts it, and **re-derives `final_request = _successor_request(remaining)`** — the close becomes
the whole broker remainder. This is also why the SCALING_OUT block forces `scaling=False` for Alpaca.

**DEVIATION (proven on the claim tables, `tests/test_exit_verdict_f_chokepoint_partial.py`).** The spec's
answer was a marker-conditional bypass of that protocol (§6.4). The first version of the test hit the next
wall: the owner-transport **outbox is single-slot** — the resting deadman IS the active transport, so the
ordinary-exit lease the chokepoint needs for the f sell is refused with `alpaca_owner_transport_kind_mismatch`.
The shipped precedent for a partial sell resting BESIDE the deadman is the OCO tranche
(`_place_scale_out_limit`): a **sibling order** POSTed straight to the adapter, cid written before the POST
(ack-loss), fill adopted on a later tick, cancelled by every whole exit before the handoff. That is what the f
sell is (§6.6). The bypass is not in the code.

**Fact 2 — the resting deadman reserves `qty_available`** (alpaca_spot.py `place_deadman_stop` docstring;
`live_deadman_stop_inert_until_rth` still reserves), so the stop must cover exactly R before f can rest. The
shrink is cancel + re-arm through the existing terminal → re-arm path (`_apply_terminal_deadman_outcome` →
`_ensure_alpaca_deadman_stop(quantity=remaining)`), where the **head guard** subtracts `pending_qty` (§6.3).
NOT `replace_order_qty` (zero callers; 422 while `accepted`; `pending_replace` is not certifiable ⇒
`_queue_full_close` = PATH B R1, a whole-runner flatten on a transient).

**Fact 3 (found while building) — an ACTIVE deadman generation is never resized in place.** The existing-
generation path certifies the resting order as protection from identity alone; only NEW placements cross the
head guard. So "the head guard re-covers Q after a failed partial" cannot happen by itself: the **cover pulse**
(§6.2) runs in reverse (mode `recover`) — cancel the R generation, let the maintenance call re-arm Q − k.

## 2. Seams

REUSED unchanged: `_arm_opinion_exit` and the four opinion sites with their gates (30-s floor, dwell confirm);
the 9 `_transition_to_bailout` callers (viability floor kept — operator); `_submit_live_market_exit` for the
WHOLE exits (cannot-split, partial-failed, tick deadman, D2); `_apply_confirmed_live_partial_exit`;
`_ensure_alpaca_deadman_stop` re-arm-after-terminal; pure `_signed_tape_features`; `scale_out_quantity`;
`_utcnow()` / `_tape_asof_default`; `_cancel_scale_limit_and_clamp`; `_strict_client_order_id_truth`;
`_EXIT_SUBMIT_MAX_ATTEMPTS` / `_EXIT_SUBMIT_BACKOFF_BASE_SECONDS`; `_notional_guard_multiplier` (rung pricing).

NOT used: PATH B (`replace_order_qty`, `path_b_partial`); SCALING_OUT; `_scale_out_to_runner` (breakeven +
TRAILING — F did not measure it); a new live FSM state; an env kill switch.

NEW: `exit_verdict.py` (pure); three bounded tape readers + `bounded_fetchall`; the verdict elif; the cover
pulse; the sibling f sell + its service; `_verdict_partial_to_runner`; the chokepoint-head release/abandonment;
the deadman head guard; guards on the chandelier and first-target blocks; recycle keys; reasons; parity alphabet;
`chili_momentum_exit_verdict_sell_fraction`.

## 3. The per-leg phase machine (`le["exit_verdict"]["phase"]`; the live FSM state never changes for a partial)

| phase | live state | who may sell | enter on |
|---|---|---|---|
| (absent) | ENTERED/TRAILING | today's machinery | — |
| armed | ENTERED/TRAILING | verdict partial; resting deadman; bid-stop at `pos.stop_price`; USD caps; first-target whole (unchanged) | first `_exit_verdict_tick` pass after `opinion_exit_armed` exists (`live_exit_verdict_armed`) |
| partial_shrink_pending | ENTERED/TRAILING | nothing new (bid-stop / USD caps / bailouts still run and ABANDON the partial at the chokepoint head) | D fired, can_split (`live_exit_verdict_partial`) |
| partial_sell_pending | ENTERED/TRAILING | the f SIBLING in flight (`chili_ml_tv_…`) | R generation `protected` with `deadman_stop.qty == R` (`live_exit_verdict_partial_shrunk`) |
| runner | ENTERED/TRAILING (unchanged) | tick deadman; D2; resting deadman(R); bid-stop; USD caps | f fill adopted (`live_partial_exit_filled` + `live_exit_verdict_runner_started`) |
| runner_exit_pending | ENTERED/TRAILING | the runner sell in flight | print ≤ level (`live_tick_deadman_exit`) or D2 (`live_exit_verdict_exit`) |
| exited | EXITED | — | `_complete_confirmed_live_exit` |

Edges (pure table `exit_verdict._ALLOWED`, tested over the full product): absent→armed; armed→armed;
armed→partial_shrink_pending; armed→exited (cannot split ⇒ whole = D); partial_shrink_pending→partial_sell_pending;
partial_shrink_pending→armed (cap with the Q stop intact); partial_shrink_pending→exited (protection unavailable ⇒
full close); partial_sell_pending→runner; partial_sell_pending→armed (zero-fill terminal ⇒ `recover`);
partial_sell_pending→exited (cap ⇒ whole, kind `whole_partial_failed`); runner→runner (ratchet);
runner→runner_exit_pending; runner→exited; runner_exit_pending→exited; any→exited; any→cleared on recycle.

Invariants (each a test): **I1** total sold == Q (a whole exit during the sibling's flight cancels + adopts it
first; the clamp then sells Q − k). **I2** the deadman covers exactly `pos.quantity − pending_qty` on every
placement path; ONE subtraction, in the head guard; `pending_qty` cleared exactly once. **I3** every phase
write is `_commit_le` + flush BEFORE the broker call it authorises; every POST carries a cid written before it.
**I4** no phase write outside `tick_live_session`. **I5** the first-target block and the chandelier never run
while phase ∈ {partial_shrink_pending, partial_sell_pending, runner, runner_exit_pending} (chandelier also not
while armed).

## 4. The held tick, in order

5.0 `tick_as_of = _utcnow()` once per tick right after `_live_tick_bbo` resolves; threaded into every read and
receipt. 5.2 cover pulse (shrink / recover) and the sibling sell service run before the deadman maintenance
call; certification right after it. 5.4 the verdict elif sits before the break elif
(max_loss_circuit < verdict < break < burst < opinion sites). 5.5 the break elif ARMS (`momentum_break_bars`,
no 30-s floor) on equity with a readable entry-fill anchor; crypto / unreadable anchor keep the #1377
`momentum_break_stop` submit as the named fallback (`verdict_unavailable` on the receipt). 5.7 the chandelier
block is under `if not _ev_trail_bypass:` with `_be_floor` never breakeven while bypassed, and the five other
quote/flow stop-movers in the TRAILING block — the measured-move / double-top composite (live once
`partial_taken` is set, which the verdict partial sets), the OFI exhaustion lock, the tape-accel reversal exit
(a rolling 15-s window = a clock in disguise), the sell-into-strength ladder and the ask-side pressure lock —
keep their telemetry but may NOT write `pos["stop_price"]` while `_ev_trail_bypass` (review of #1385: 15 stop
lifts / 7 d across the four, `trail_stop` = 8 of 66 exits; a lift would make the bid-stop exit on a QUOTE,
pre-empting the print). 5.8 the first-target condition carries the phase guard.

## 5. The partial (§6)

6.1 Decision: `f, R, can_split = partial_split(Q_cur, Q_cur, fraction, venue increments)`; cannot split ⇒ whole
on `tape_sellers_took_it` (kind `whole_cannot_split`). Else write-ahead `partial{f, R, Q, pending_qty=f, …}`,
phase `partial_shrink_pending`, continuation wake.
6.2 Cover pulse: (a) OCO tranche cancelled first (fill adopted, f/R recomputed); (b) resting deadman cancelled,
exact CID truth re-read — not terminal ⇒ stay (attempt++, backoff); filled during the race ⇒ partial abandoned
(the maintenance call books the fill); (c) terminal zero-fill ⇒ the maintenance call this same tick re-arms
through the head guard ⇒ R. Certification: `protected` and `deadman_stop.qty == R` ⇒ `partial_sell_pending`
(`naked_window_s` reported). Cap (8) with the Q stop intact ⇒ armed again.
6.3 Head guard (in `_ensure_alpaca_deadman_stop`, after the tranche split): `quantity -= pending_qty`, else
`verdict_partial_split_arithmetic_invalid` full close.
6.5 Chokepoint head: any reason ≠ `tape_sellers_took_it` with a pending partial ⇒ the sibling is cancelled and
its fill adopted (block while the cancel is not terminal), then `pending_qty = 0`, `abandoned_by = reason`,
receipt `partial_failed{fallback: whole_by_<reason>}`, and the WHOLE handoff runs for Q − k.
6.6 The f sell (sibling): rung 1 marketable limit at bid − guard, rung 2 at 4× guard, rung 3+ market in RTH;
extended hours always a limit crossing 8× guard, DAY, `extended_hours=True`; cid `chili_ml_tv_` durable BEFORE
the POST; `live_exit_verdict_partial_submitted`. The sell service: resolve an indeterminate submit by CID; adopt
a fill (⇒ runner); zero-fill terminal ⇒ next rung (cap ⇒ whole, kind `whole_partial_failed`); past the rung's
patience (the exit backoff schedule) ⇒ cancel, a race fill is adopted.
6.7 `_verdict_partial_to_runner`: books the fill, NO state change, NO breakeven move, `frontier_at` rewound to
`decision_as_of`, runner `{level, level_source, runner_high = fill price, saw_new_high False}`; a terminal PARTIAL
fill starts the runner and sets `recover` (the R stop under-covers by f − k).

## 6. Verdict evaluation and the runner (§7)

7.2 First pass: `leg_high_print_first` (FIRST occurrence at the max in `(entry_filled_at_utc, as_of]`).
7.3 Every pass: `leg_prints_between(frontier_at, as_of]`; a strictly higher batch max moves the anchor;
`leg_prints_since_high` after the tuple `(hi_at, hi_id)`; `tape_frontier_age_s`; `stale = age > window_s/2`
(7.5 s) is a **do-not-DECIDE** rule (the first verdict D and the second D2 are withheld, `withheld: stale_tape`
on the tick result, receipt on change) and NEVER a do-not-walk / do-not-execute rule: the runner walk over the
batch, the already-certified f sell (`partial_sell`) and a forced whole after a failed partial all run on a
stale tick (review of #1385: the old early return ran BEFORE the walk and AFTER the frontier moved, so a print
≤ the level inside a > 7.5-s tick gap — 79.5% of live runner ticks, p50 9.9 s — was dropped forever; the
harness of record walks every batch and applies stale to D2 only); `since_high_verdict(rows)`; `frontier_at =
as_of` only in armed/runner. 7.5 Runner: `walk_runner_prints` per print in order — print ≤ level ⇒
`tick_deadman_stop`; new high ⇒ `runner_high`, `saw_new_high`, ratchet `level = max(level, swing_low_prev @ N
prints)` (receipt on a move only), the ratchet read `signed_tape_accel_features(as_of = the print's observed_at,
available_by = the TICK's as_of)` — the N prints observed up to the high print, as delivered by now (one
bound for both excluded the high print itself and everything delivered in its ~0.55-s lag, a different
`swing_low_prev` than the measured one); after the batch `saw_new_high and fired` (and not stale) ⇒
`tape_sellers_took_it_d2`. Exit priced at the tick's bid; the trigger is the print. 7.6 Reads: symbol-scoped,
`observed_at <= :as_of`,
`(available_at IS NULL OR available_at <= :as_of)`, `ORDER BY observed_at ASC, id ASC`, no LIMIT on the
since-high read, `bounded_fetchall(timeout_ms = _EVENT_TICK_MIN_SPACING_S · 1000 = 2000)` in a nested savepoint
that is ROLLED BACK (the `SET LOCAL` never leaks); a timeout ⇒ `unreadable{timeout}` (fail-open).

## 7. Binding values (§10)

| name | value | derivation / decision |
|---|---|---|
| sell_fraction | 24/32 = 0.75 of the CURRENT position (`chili_momentum_exit_verdict_sell_fraction`); ONE named fallback 0.5 (doctrine "sell part") — the config default, the getattr fallback and every receipt agree | 1 − runner_beats_partial_share, share = 8/32 over the 32 runner legs (tick-by-tick, bid-priced, 2026-09-10); Wilson 95% CI of the share [0.13, 0.42] excludes 0.5. EVALUATED at the shipped value by linearity: F(0.75) −495.7 vs F(0.5) −489.25 vs F(11/31) −485.50 — the worst of the three by $6–$10, inside noise; the hit-rate rule and the P&L rule disagree and the operator decides (PR #1385 open question 3). The in-memory 20/31 ⇒ 11/31 is superseded. |
| verdict floors | binding 4 (count halves), feature 3 | both reported; 0 evaluations bound at n == 3 |
| since-high anchor | FIRST print at the leg max; window excludes the high print; ties counted | never the feature dict's `prints_since_high` (newest occurrence) |
| tick_deadman window N | 255 = `chili_momentum_g4_reentry_tape_window_prints` | F(11/31,255) −485.50 beats N=458 (−500.06) |
| tick_deadman base | first of `swing_low_prev`, `swing_low_now`, `buy_support_px` strictly < entry at the fill (N prints observed up to the fill, delivered by the tick); fallback the resting stop | a print base on 35/35 legs at N=255 (`swing_low_prev` 33, `swing_low_now` 2, `resting_stop` 0) |
| ratchet | `max(level, swing_low_prev @ new-high print)`, never lowered; the read is delivery-bounded by the TICK | ratchets per runner p50 1 / p90 1 / max 3; 24 of 32 runners ended on the tick stop |
| D2 reference | a print strictly above the partial FILL since the partial; D anchor = leg high | 8 of 32 runners ended on D2 |
| stale_tape bound | `chili_momentum_l2_confirm_window_s / 2` = 7.5 s — withholds D / D2 only; the walk, the f sell and a forced whole continue | the feature's own halt-gap rule at the END of the window; armed ticks disarmed 2 / 415 = 0.5% in the table (armed phase only — the runner walk was never stale-gated in the harness) |
| shipped latency | measured on every partial: `decision_to_submit_s`, `decision_to_fill_s`, `slippage_vs_decision_bid_usd`, `sell_rung` on `live_exit_verdict_runner_started` | the table priced the partial at the decision tick's bid; the shipped path needs the shrink pulse + certification + a rung-1 limit first (≥ 2 pulses on the 3.19-s cadence); the gap is now a number in the receipt, not an assumption |
| read timeout | 2000 ms = `_EVENT_TICK_MIN_SPACING_S` | nested-savepoint rollback |
| resting broker stop | unchanged on ratchets; shrunk once to R; no breakeven move | no in-place resize path exists (Fact 3) |
| attempts / backoff | 8 / 5 s (existing) for shrink, sell and the sibling's rung patience | `_EXIT_SUBMIT_*` |
| arming floor | 30 s at the four opinion sites; none for `momentum_break_bars` | F's instants included the 7 break exits without a floor |
| kill switch | none (LIVE + ON); named fallbacks `whole_cannot_split`, `whole_partial_failed`, `rearm`, `full_close`, `deadman_filled`, `no_equity_tape` / `entry_fill_anchor_missing` → `momentum_break_stop` | doctrine "no dark flags" |

## 8. Receipts

`live_opinion_exit_armed` (+ `armed_exit`, `superseded`); `live_exit_verdict_armed`; `live_exit_verdict_partial`;
`live_exit_verdict_partial_shrunk`; `live_exit_verdict_partial_submitted`; `live_exit_verdict_partial_failed{stage,
attempt, why, fallback}`; `live_exit_verdict_runner_started`; `live_tick_deadman_ratchet`; `live_tick_deadman_exit`;
`live_exit_verdict_exit{kind}`; `live_exit_verdict_unavailable{binding}` (once); `live_exit_verdict_unreadable{why}`
(on change; `why: stale_tape` carries `walks_and_executions_continue: true`); `live_exit_verdict_sibling_released`;
`live_exit_verdict_deadman_recovered`. Every one carries `derivation, as_of, phase, state, bid, bbo_source,
bbo_age_s, bbo_reason, tape_frontier_age_s, stale_tape_bound_s, opinion_exit_armed`. `live_exit_filled` and the
bailout submit carry `exit_verdict`. `live_exit_verdict_runner_started` additionally carries the shipped latency:
`decision_as_of, decision_bid, decision_to_submit_s, decision_to_fill_s, slippage_vs_decision_bid_usd,
sell_attempts, sell_rung`. `live_tick_deadman_exit` carries `stale` (the walk ran on a quiet tape).

Parity alphabet: `live_opinion_exit_armed`, `live_exit_verdict_armed`, `live_exit_verdict_partial`,
`live_tick_deadman_exit`, `live_exit_verdict_exit` in; the ratchet and the mechanics out.

## 9. Acceptance

The PR body carries the tick-by-tick acceptance table (scratchpad harness importing the SHIPPED
`exit_verdict.py`, one bounded fetch per leg, priced at the nearest NBBO bid ≤ the decision print).

## 10. Review fixes (2026-09-10, PR #1385 review; tests/test_exit_verdict_f_review_fixes.py)

| finding | fix | test |
|---|---|---|
| stale tick dropped the runner batch and advanced the frontier past it (major, ×2) | the runner walk (deadman + ratchets), the f sell and a forced whole run BEFORE the stale gate; stale withholds D / D2 only (§6 7.3) | the reviewer's two repros + the flight batch, a ratchet inside a stale batch, D2 waiting for the tape |
| stale gate withheld the already-decided f sell (minor) | `partial_sell_pending` is not stale-gated | `test_the_decided_f_sell_is_not_withheld_by_a_quiet_tape` |
| the f sibling was an unowned open order to the account certifier (minor; inherited by the OCO tranche) | `alpaca_ledger_position_sibling_order_ids` whitelists the sibling and the tranche under `position` (no new marker: the mig-374 index expression is untouched) | the certifier on a scripted ledger |
| acceptance priced the partial at the decision bid; the shipped path adds ≥ 2 pulses + a rung-1 limit (minor) | the latency and the slippage against the decision bid are measured on every partial (§8) | the runner-start receipt |
| four quote/flow stop-movers still lifted `pos["stop_price"]` outside the trail bypass (major) | the five writes (incl. the measured-move composite) are guarded by `_ev_trail_bypass`; telemetry unchanged (§4 5.7) | AST pin on the tick |
| 0.75 never evaluated; the receipt derivation cited the superseded in-memory run; four places said 11/31 (minor) | F(0.75) evaluated by linearity in the derivation, the config description and §0; one harness of record everywhere; the getattr fallback is the named 0.5 | derivation + fallback pins |
| 4×/8× rung literals duplicated (minor) | `_EXIT_LADDER_GUARD_MULT_*` + `_exit_ladder_guard_fraction`, used by the chokepoint and the sibling | numeric + source pin |
| the ratchet read's delivery bound at the print's own `observed_at` excluded the high print (minor) | `signed_tape_accel_features(available_by=tick as_of)` for the ratchet and the base read | the recorded read bounds |
| a Massive snapshot bid can still price the HELD decision tick (minor) | Build B (#48, `fix/held-tick-bbo-iqfeed-l1-first`) owns `_live_tick_bbo`; merged together | — |
| the 40-file run's log was 0 bytes (minor) | re-run, log in the PR body | — |
