# EXIT VERDICT F — the tape answers the exit, in prints since the leg's own high

Planner: [44] / [21] / [47]. Shipped 2026-09-10 (this PR). Build A of the judge-merged spec of the same
date; §1, §4, §6, §10 of that spec are reproduced here as the design of record, with the two places the
code proved the spec wrong marked **DEVIATION**.

## 0. Doctrine and the measurement

- **The tick always answers.** Exits are decided on PRINTS, never on a wall-clock bar or a quote-mid bar.
- **Mechanism, not binary.** Chart = boundary (sell all). Tape = moment (sell PART). Reclaim = come back.
- **No magic numbers, no dark flags.** Every value is a named derivation reported in the receipt; the
  machine is LIVE + ON; the only fallbacks are named ones.

7 days, 35 opinion-exit legs since 2026-09-03, in-memory re-run (N = 255, STEP = 100):

| rule | P&L of the same legs |
|---|---|
| actual (what the lane did) | −$697.87 |
| D = whole on the first since-high print verdict | −$232.62 |
| F(0.5) = half on D, runner under the tick deadman + D2 | −$152.79 |
| **F(11/31)** | **−$129.60** |

The brief's 34-leg scripts: actual −$690.79 → D −$297.77 → F −$209. 16 of 34 opinion exits fired at
n = 0 prints since the leg's high — the name was AT its own high print when we left. The tick deadman
beat the ATR deadman −$304.93 vs −$468.88 on the same legs.

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
block is under `if not _ev_trail_bypass:` with `_be_floor` never breakeven while bypassed. 5.8 the first-target
condition carries the phase guard.

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
(7.5 s) ⇒ no action in any phase, receipt on change; `since_high_verdict(rows)`; `frontier_at = as_of` only in
armed/runner. 7.5 Runner: `walk_runner_prints` per print in order — print ≤ level ⇒ `tick_deadman_stop`;
new high ⇒ `runner_high`, `saw_new_high`, ratchet `level = max(level, swing_low_prev @ N prints)` (receipt on a
move only); after the batch `saw_new_high and fired` ⇒ `tape_sellers_took_it_d2`. Exit priced at the tick's
bid; the trigger is the print. 7.6 Reads: symbol-scoped, `observed_at <= :as_of`,
`(available_at IS NULL OR available_at <= :as_of)`, `ORDER BY observed_at ASC, id ASC`, no LIMIT on the
since-high read, `bounded_fetchall(timeout_ms = _EVENT_TICK_MIN_SPACING_S · 1000 = 2000)` in a nested savepoint
that is ROLLED BACK (the `SET LOCAL` never leaks); a timeout ⇒ `unreadable{timeout}` (fail-open).

## 7. Binding values (§10)

| name | value | derivation / decision |
|---|---|---|
| sell_fraction | 11/31 = 0.3548 of the CURRENT position; named fallback 0.5 | 1 − runner_beats_partial_share, share = 20/31 over the 31 runner legs (N=255, STEP=100, 2026-09-10). Binomial 95% CI ≈ [0.47, 0.81] includes 0.5; P&L linear in q with ΣR (−72.96) > ΣD (−232.62) ⇒ no interior optimum. |
| verdict floors | binding 4 (count halves), feature 3 | both reported; 0 evaluations bound at n == 3 |
| since-high anchor | FIRST print at the leg max; window excludes the high print; ties counted | never the feature dict's `prints_since_high` (newest occurrence) |
| tick_deadman window N | 255 = `chili_momentum_g4_reentry_tape_window_prints` | F(255) −152.79 beats N=458 (−170.35) and N=322 (−159.83) |
| tick_deadman base | first of `swing_low_prev`, `swing_low_now`, `buy_support_px` strictly < entry at the fill; fallback the resting stop | no print base on 7/35 legs at N=255 |
| ratchet | `max(level, swing_low_prev @ new-high print)`, never lowered | new-high prints per runner p50 0 / p90 4 / max 18; ratchets p50 0 / p90 2 / max 7 |
| D2 reference | a print strictly above the partial FILL since the partial; D anchor = leg high | −152.79 vs "new LEG high" −157.14 |
| stale_tape bound | `chili_momentum_l2_confirm_window_s / 2` = 7.5 s | the feature's own halt-gap rule at the END of the window |
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
(on change); `live_exit_verdict_sibling_released`; `live_exit_verdict_deadman_recovered`. Every one carries
`derivation, as_of, phase, state, bid, bbo_source, bbo_age_s, bbo_reason, tape_frontier_age_s,
stale_tape_bound_s, opinion_exit_armed`. `live_exit_filled` and the bailout submit carry `exit_verdict`.

Parity alphabet: `live_opinion_exit_armed`, `live_exit_verdict_armed`, `live_exit_verdict_partial`,
`live_tick_deadman_exit`, `live_exit_verdict_exit` in; the ratchet and the mechanics out.

## 9. Acceptance

The PR body carries the tick-by-tick acceptance table (scratchpad harness importing the SHIPPED
`exit_verdict.py`, one bounded fetch per leg, priced at the nearest NBBO bid ≤ the decision print).
