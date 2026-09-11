# EXIT VERDICT G — sell INTO the spike, the WHOLE position, on the tape's word

Planner: [44] / [21] / [47]. Shipped 2026-09-10 (PR #1385, amended the same day: Amendments 1–3 of
`BUILD_A_AMENDMENT_G_sell_into_spike_0910.md`). The first version of this PR (verdict **F**: PART on
the since-high verdict D, a runner under a tick deadman, a sibling f sell, a shrunk broker stop) is
superseded in full by the measurements in §0; nothing of the partial / runner / sibling machinery
survives in the code. The file keeps its name so the planner links stay valid.

## 0. Doctrine and the measurement

- **The tick always answers.** Exits are decided on PRINTS, never on a wall-clock bar or a quote-mid bar.
- **Sell INTO the spike, not after it.** Operator, 18:12Z: "hinayaan bumagsak kesa magbenta sa spike".
- **ALL at the trigger; buy again when it is viable again.** Operator, 18:35Z: "bakit kalahati lang kung
  mataas ang accuracy? di ba dapat LAHAT, tapos bili ulit kapag viable na ulit?"
- **No magic numbers, no dark flags.** Every value is a named derivation reported in the receipt; the
  machine is LIVE + ON on every equity leg; the only fallbacks are named ones.

Every walk below starts at the **ENTRY FILL** (the spike happens 3–90 s after the fill, inside the opinion
sites' 30-s floor), is print-priced, and uses the 60-min horizon as a measurement bound only.

| rule (35 opinion-exit legs / 7 d, `g_sell_into_spike_accel_rollover.py`) | P&L |
|---|---|
| actual (what the lane did) | −$697.87 |
| F′ = half on the first since-high verdict D, runner under the tick deadman | −$202.88 |
| **G** = half at the first ROLLOVER of `signed_tape_accel` while the print is above entry, else D | **−$2.32** |

G fired on the spike in 11 of 35 legs and was better in EVERY one (WYHG 09-08 09:07 −65→+9, 09:09 −46→+42,
MOBX −29→−9, TNON ×4 +2–3, FTFT +3); identical to F′ on the other 24.

| rule (ALL 78 live Alpaca legs / 14 d, winners included, `h_sell_all_vs_half_all_legs.py`) | 78 legs | 20 winners | 58 losers |
|---|---|---|---|
| actual | −$1,216.28 | +$747.32 | −$1,963.60 |
| G-half (runner under the monotone tick deadman) | −$59.25 | +$296.71 | −$355.97 |
| **G-ALL (100% at the G / D trigger, no runner)** | **+$157.52** | +$321.62 | −$164.11 |
| G-all + one tape-proven reclaim re-entry | +$271.45 | +$367.02 | −$95.60 |

Triggers: spike 28 / D 43 / deadman 7. Sell-all beats half by +$217 on the same legs with FEWER fills; the
runner loses money even with the winners in. The reclaim's +$114 is at print prices across 54 extra round
trips — not robust to small-cap spread (~0.5% ≈ $7.5/leg) — so re-entry goes through the normal entry path
(recycle #1374, ramp #1376, the [59] reclaim gate #1386), never a mechanical re-buy.

**Amendment 3 — why the runner stop was late by construction** (`i_post_spike_structure.py`, 71 triggered
legs): 83% make a NEW HIGH later, but the retrace before it is p50 **1.03× the spike** (p75 1.91×, p90 3.52×).
A swing low COMPLETES only after the bounce, so right after a sale at the spike top the "last completed swing
low" is the PRE-spike low — the stop gives back the whole spike before it fires. Half + breakeven beats
sell-all by +$117 at print prices, but 55/71 runners exit exactly at entry and ~0.4% slippage (≈ $3 each,
≈ −$155) puts that inside the noise. Decision: sell all; the 83% continuation is captured by RE-ENTRY.

## 1. The rule

All anchored at the entry fill; recycle = new leg = new anchor. On every HELD tick, in this order:

1. **Tick deadman, per print.** The batch `(frontier, as_of]` is walked in full; the first print ≤ the
   level ends the leg. Level = the last completed swing low in prints: base at the fill = the first of
   `swing_low_prev`, `swing_low_now`, `buy_support_px` strictly below the entry over the N most recent
   prints (fallback the resting broker stop); **MONOTONE ratchet on every held tick** — the same read at
   the tick, taken when the candidate is below the last print and above the level; it rises on every new
   completed swing low, never only on a new high, never lowers. Never withheld by the stale gate.
2. **G — the accel rollover.** `signed_tape_accel` over the N-print window (the same feature D reads,
   N = `chili_momentum_g4_reentry_tape_window_prints` = 255) crosses from > 0 at the previous DECIDED
   held evaluation to ≤ 0 now, while the LAST PRINT > the entry fill.
3. **D — the since-high verdict.** Over the prints strictly after the leg's high print (FIRST occurrence
   on a tied max; the high excluded; min = the feature's own floor, 3 / binding 4):
   `signed_tape_accel < 0 AND buy_share_delta < 0 AND swing_low_now < swing_low_prev`.

The EARLIER of G and D on the tape (G first when both are true on one tick) ⇒ the **WHOLE position at the
bid through the existing exit seam**. One fill. No partial, no runner, no second sale.
`exit_fraction = 1.0` is a **reported** binding value with the 78-leg derivation — not a knob.

The stale bound (`chili_momentum_l2_confirm_window_s / 2` = 7.5 s, the feature's own halt-gap rule at the
END of the window) withholds G and D only; `accel_prev` is not advanced on a withheld tick, so a rollover
that happened before a quiet spell decides on the first fresh tick with a readable window.

## 2. What runs it — from the fill, no arming

`_exit_verdict_active(sess, le)` = equity tape AND a readable `entry_filled_at_utc`. The machine arms on
the FIRST held tick after the fill (`live_exit_verdict_armed`). The four opinion sites (#1377's three
bailout-shaped sites + the 10-s bar elif) still call `_arm_opinion_exit` — as a **receipt**
(`live_opinion_exit_armed`, `armed_exit: exit_verdict_g_all`): the opinion wanted out, the tape decides.
Crypto (`-USD`) and an unreadable anchor are named unavailable once (`live_exit_verdict_unavailable`) and
keep the #1377 behaviour (`momentum_break_stop` on the bar elif) as the named fallback. The viability floor,
the USD caps, EOD and the operator flatten are untouched (any → `exited`).

Why from the fill and not from an opinion: the 78-leg table's `first_trigger(start=0)` walks from the fill;
the cited spikes (SKYQ 13:58 +2.6% in 21 s, PCLA 13:40 +1.1% in 10 s) are inside the opinion sites' 30-s
floor. Flagged in the PR as the one place the amended build goes past the original brief's "the sites arm".

## 3. The per-leg phase machine (`le["exit_verdict"]["phase"]`; the live FSM state never changes)

| phase | who may sell | enter on |
|---|---|---|
| (absent) | today's machinery | crypto / unreadable anchor (never arms) |
| armed | the verdict machine (deadman / G / D); the resting deadman; the bid-stop at `pos.stop_price`; USD caps; first-target whole (unchanged) | first held tick after the fill |
| exit_pending | the exit seam (the deadman-close handoff + the fill poll) | the trigger (`live_exit_verdict_fired` / `live_tick_deadman_exit`) |
| exited | — | `_complete_confirmed_live_exit` |

Edges (`exit_verdict._ALLOWED`, tested over the full product): absent→armed; armed→armed; armed→exit_pending;
armed→exited; exit_pending→exited; any→cleared on recycle. The decision is written ahead (`_commit_le`) BEFORE
the submit; `exit_pending` never reads the tape or decides again (never a second exit); only when the seam has
cleared the pending exit WITHOUT a fill and shares are still held is the SAME decision re-submitted
(`resubmit`, no new receipt).

Guards: the chandelier and the five quote/flow stop-movers (measured-move composite, OFI exhaustion lock,
tape-accel reversal, sell-into-strength ladder, ask-side pressure lock) keep their telemetry but may NOT
write `pos["stop_price"]` while the phase ∈ {armed, exit_pending} — i.e. on every equity leg the machine
judges (the G-all table had no quote/ATR stop lifts; a lift would make the bid-stop exit on a QUOTE as
`trail_stop`, pre-empting the print). The first-target whole exit stays reachable while armed, not once decided.

## 4. The held tick, in order (`_exit_verdict_tick`)

`tick_as_of = _utcnow()` once per tick right after `_live_tick_bbo` resolves, threaded into every read and
receipt. Reads (entry_gates, `bounded_fetchall(timeout_ms = _EVENT_TICK_MIN_SPACING_S · 1000 = 2000)` in a
nested savepoint that is ROLLED BACK): the batch `leg_prints_between` strictly after the FRONTIER TUPLE
`(observed_at, id)` — the entry fill on the first pass, the last WALKED print afterwards — and
`leg_prints_since_high` after the high print's tuple (no LIMIT). The N-print feature read
(`signed_tape_accel_features(window_prints=N)`) twice: the base at the fill (`as_of = entry_at`,
`available_by = tick`) once, and at the tick (G + the ratchet candidate) every pass.

1. batch unreadable ⇒ `live_exit_verdict_unreadable{why}` on change; **the frontier does not move**.
2. walk every print: crossing ⇒ `tick_deadman` (the walk stops AT the crossing print, which becomes the
   frontier); a strictly higher print moves the leg high (first occurrence) and restarts the since-high count.
3. the frontier = the last walked print's tuple — never the tick's `as_of` (review of #1385, major: a print
   observed after the last walked print but delivered after the tick is still read next tick).
4. the monotone ratchet ⇒ `live_tick_deadman_ratchet{old, new, print, print_at, source_key, ratchets}` on a move.
5. D over the since-high prints (unreadable ⇒ unreadable; the walk stands).
6. G from `accel_prev` (the previous decided evaluation) and the tick's accel.
7. stale ⇒ withheld (`withheld: stale_tape`), `accel_prev` untouched, receipt on change with
   `walks_and_executions_continue: true`; else the EARLIER of G and D ⇒ `_decide` (phase exit_pending,
   `ev["exit"]`, write-ahead) ⇒ `live_exit_verdict_fired`; no trigger ⇒ `accel_prev = accel_now`.

The elif in `tick_live_session` (max_loss_circuit < verdict < break < burst < opinion sites) submits the
WHOLE position through `_submit_live_market_exit` with `reason` / cid tag from `_EXIT_VERDICT_ACTIONS`
(`tick_deadman_stop`/`td`, `tape_accel_rollover`/`ta`, `tape_sellers_took_it`/`tv`), the tick's bid/ask/mid
and `extra={exit_verdict, trigger, exit_fraction}`; `_live_exit_submit_succeeded` books the outcome. With a
resting Alpaca deadman the seam is the deadman-close handoff: pulse 1 freezes the successor intent for Q and
returns `deferred / pre_place_blocked` (the 0.5-s continuation wake re-pulses); pulse 2 cancels the stop and
POSTs the close for Q as the owner transport's successor — CHILI-owned to the certifier by identity.

## 5. Binding values

| name | value | derivation / decision |
|---|---|---|
| exit_fraction | **1.0** — the WHOLE position at the trigger (`exit_verdict.EXIT_FRACTION`, reported on every receipt) | 14 d, 78 live Alpaca legs, winners included: all +$157.52 vs half −$59.25 vs actual −$1,216.28; +$217 vs half with fewer fills; Amendment 3: half + breakeven +$459.17 vs all +$341.75 at print prices but 55/71 runners exit at entry and ~$3 slippage each eats it. Not a knob; re-measure when a multi-hour runner day exists in the sample (unmeasured, not refuted). |
| G trigger | `accel_prev > 0 and accel_now <= 0 and last_print > entry_px`, every held tick | 11/35 legs on the spike, better in every one; 28/78 triggers. The scripts read N = 458 (that day's p50 of the 15-s window, a clock in disguise); shipped N = the named setting 255 (p50 at 108 decision instants) — the G table at N = 255 is an open question, re-run after close. |
| verdict floors | binding 4 (count halves), feature 3 | both reported; 0 evaluations bound at n == 3 |
| since-high anchor | FIRST print at the leg max since the fill, found BY THE WALK; window excludes the high; ties counted | never the feature dict's `prints_since_high` (newest occurrence) |
| window N | 255 = `chili_momentum_g4_reentry_tape_window_prints` (reused) | the G accel, the ratchet candidate and the base read all use it |
| tick_deadman base | first of `swing_low_prev`, `swing_low_now`, `buy_support_px` strictly < entry at the fill (N prints observed up to the fill, delivered by the tick); fallback the resting stop | a print base on 35/35 legs at N=255 (`swing_low_prev` 33, `swing_low_now` 2, `resting_stop` 0) |
| ratchet | MONOTONE, every held tick: `cand = first non-null of the three keys at the tick; level = cand if cand < last_print and cand > level` | `g2_monotone_swing_low_ratchet.py`; rises without a new high; the pre-trigger floor is the pullback low (proper before the spike — Amendment 3). The 78-leg G-all table walked the pre-trigger floor at the RESTING stop: the tick deadman before the trigger is the spec's floor, reported here, not measured by that table (tick vs ATR deadman −$304.93 vs −$468.88 was measured from the decision instant on the D arm). |
| frontier | the last WALKED print's `(observed_at, id)`; the entry fill on the first pass | never `as_of`; an unreadable batch leaves it (review of #1385, major) |
| stale_tape bound | `chili_momentum_l2_confirm_window_s / 2` = 7.5 s — withholds G / D only; the walk always runs; `accel_prev` not advanced | the feature's own halt-gap rule at the END of the window |
| shipped latency | decision tick → POST = one continuation pulse (0.5 s) + the cancel round-trip; priced at pulse 2's HELD-tick bid (the [48] envelope, IQFeed L1 first, never a snapshot) | `tests/test_exit_verdict_g_whole_exit_seam.py` on the real claim tables |
| read timeout | 2000 ms = `_EVENT_TICK_MIN_SPACING_S` | nested-savepoint rollback |
| resting broker stop | unchanged on ratchets; released by the whole-close handoff | the last-resort floor |
| arming | none needed: every equity leg with a readable anchor, from the first held tick after the fill | the measured walk starts at the fill; opinion sites are receipts |
| kill switch | none (LIVE + ON); named fallbacks `no_equity_tape` / `entry_fill_anchor_missing` → `momentum_break_stop` | doctrine "no dark flags" |

## 6. Receipts

`live_opinion_exit_armed` (+ `armed_exit`, `superseded`); `live_exit_verdict_armed{leg_high, n_since_high,
verdict, rollover, deadman, min_prints, window_s_binding, window_prints, prints_since_entry,
seconds_since_entry, trigger_order}`; `live_exit_verdict_fired{trigger, reason, accel_prev, accel_now,
prints_since_entry, prints_since_high, bid, exit_fraction = 1.0, exit_fraction_derivation, binding, verdict,
rollover, leg_high, entry_px, last_print, level, remaining_qty, decision_as_of}`; `live_tick_deadman_exit{level,
level_source, crossing_print, prints_scanned, batch_window, ratchets, resting_stop, remaining_qty, stale,
prints_since_entry, prints_since_high, exit_fraction, binding}`; `live_tick_deadman_ratchet{old, new, print,
print_at, source_key, ratchets, prints_in_batch}`; `live_exit_verdict_unavailable{binding}` (once);
`live_exit_verdict_unreadable{why}` (on change; `stale_tape` carries `walks_and_executions_continue: true`).
Every one carries `derivation, as_of, phase, state, bid, bbo_source, bbo_age_s, bbo_fallback_engaged` (the
[48] envelope), `tape_frontier_age_s, stale_tape_bound_s, opinion_exit_armed, exit_fraction,
exit_fraction_derivation`. `live_exit_filled`, the bailout submit and the whole-exit submit carry `exit_verdict`.

Parity alphabet: `live_opinion_exit_armed`, `live_exit_verdict_armed`, `live_exit_verdict_fired`,
`live_tick_deadman_exit` in; the ratchet and the mechanics out.

## 7. Review of #1385 (2026-09-10) — what survived Amendment 2 and how it is closed

| finding | fix | test |
|---|---|---|
| stale tick advanced `frontier_at` past unwalked prints (major, ×2) | the frontier is the last WALKED print's tuple; an unreadable batch never moves it; the walk runs before any gate; a crossing print inside a > 7.5-s gap exits on the stale tick | `test_exit_verdict_f_review_fixes.py`, `..._state_machine.py` |
| quote/flow stop-movers could lift the stop on the held phase (major) | every `pos["stop_price"]` write under `not _ev_trail_bypass`; bypass = every judged leg; nothing acts after the whole exit | AST pin + `test_nothing_after_the_whole_exit_acts_on_the_leg` |
| the stale gate withheld the decided sell (minor) | the decision is write-ahead and submitted on the same tick; exit_pending never re-reads | source pin + behavioural |
| the f sibling unowned by the certifier (minor) | no sibling; the OCO-tranche whitelist stays; nothing under `exit_verdict` is whitelisted | certifier on a scripted ledger |
| 0.75 never evaluated (minor) | exit_fraction = 1.0 reported with the 78-leg derivation; no knob; no partial-era number in any receipt | derivation pins |
| 4×/8× rung literals duplicated (minor) | one ladder (`_exit_ladder_guard_fraction`), one caller (the chokepoint); the sibling ladder is gone | numeric + source pin |
| ratchet read delivery bound excluded the high print (minor) | the base read is delivery-bounded by the tick; the ratchet read IS the tick read (last print inclusive) | recorded reads |
| a Massive snapshot bid could price the decision (minor) | #1384 merged: the HELD tick reads L1 first; every receipt carries the [48] envelope; the exit is priced by the chokepoint from the tick's bid | receipt pins |
| the acceptance table priced at the decision tick (minor) | the shipped latency is measured on the real seam (two pulses) | `test_exit_verdict_g_whole_exit_seam.py` |
| the 40-file run's log was 0 bytes (minor) | the neighbours are run for real; results in the PR body | — |
