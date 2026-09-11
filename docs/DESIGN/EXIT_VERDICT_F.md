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
   level ends the leg. **[65] (2026-09-11, + the review of #1419):** the level is set ONCE = the leg's
   own resting stop **at the fill** (`position.stop_price_at_fill`, stamped by the fill handler — the stop R
   is defined by; `exit_verdict.tick_deadman_fill_base`). Not the stop at the first readable verdict tick:
   a C4 viability tighten or an A2 displacement can lift `position.stop_price` to `avg × 0.995` before it.
   Named fallbacks: a leg with no stamp reads the current stop (`resting_stop_source` says so); an
   unreadable stop or one not below the entry ⇒ level `None` (the chandelier + bid-stop manage the leg).
   **No pre-trigger ratchet** (`no_pre_trigger_ratchet_until_completed_swing_facts_wired`): the rolling
   count-half minimum is not a completed low and raising the floor to it was measured to cost money; the
   candidate is shadow-recorded on `live_exit_evaluation`. Receipt context only: the old base (the first
   of `swing_low_prev`, `swing_low_now`, `buy_support_px` strictly below the entry over the N prints at the
   fill, `count_half_context`) and the [62] ledger's completed-cycle median (`cont_context` — the #1419
   draft base; the review measured it to be the scanner's cold-start noise). Never withheld by the stale
   gate. See §8.
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
| armed | the verdict machine (deadman / G / D); the resting deadman; the bid-stop at `pos.stop_price`; USD caps | first held tick after the fill |
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
`trail_stop`, pre-empting the print). The 2026-09-11 operator amendment neutralizes fixed first-target profit exits for live-engine equity legs with a readable fill anchor, including the actual Alpaca PAPER lane. The target transition and old SCALING_OUT submit both yield to this ownership, even on stale/unreadable tape; independent hard protection and fallback stops remain. Entry target/fee/plan context is retained. Existing submitted orders retain exact reconciliation ownership. Unsupported live legs and DB-paper/ReplayV2 retain their named non-G target fallback; those simulators do not yet implement the held G/D evaluator.

## 4. The held tick, in order (`_exit_verdict_tick`)

`tick_as_of = _utcnow()` once per tick right after `_live_tick_bbo` resolves, threaded into every read and
receipt. Reads (entry_gates, `bounded_fetchall(timeout_ms = _EVENT_TICK_MIN_SPACING_S · 1000 = 2000)` in a
nested savepoint that is ROLLED BACK): the batch `leg_prints_between` strictly after the FRONTIER TUPLE
`(observed_at, id)` — the entry fill on the first pass, the last WALKED print afterwards — and
`leg_prints_since_high` after the high print's tuple (no LIMIT). The N-print feature read
(`signed_tape_accel_features(window_prints=N)`) twice: the base at the fill (`as_of = entry_at`,
`available_by = tick`) once (count-half CONTEXT since [65]; the binding base is the fill-stamped resting
stop, and the ledger context is `le["tape_cycle_state"]` with no new read), and at the tick (G + the shadow
count-half candidate) every pass.

1. batch unreadable ⇒ `live_exit_verdict_unreadable{why}` on change; **the frontier does not move**.
2. walk every print: crossing ⇒ `tick_deadman` (the walk stops AT the crossing print, which becomes the
   frontier); a strictly higher print moves the leg high (first occurrence) and restarts the since-high count.
3. the frontier = the last walked print's tuple — never the tick's `as_of` (review of #1385, major: a print
   observed after the last walked print but delivered after the tick is still read next tick).
4. [65] no ratchet: the rolling count-half candidate is recorded on the evaluation receipt
   (`observations.ratchet{candidate, source_key, level, moved: false, binding}`); the level never moves
   after the base. `live_tick_deadman_ratchet` is no longer emitted.
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
| window N | 255 = `chili_momentum_g4_reentry_tape_window_prints` (reused) | the G accel, the shadow count-half candidate and the count-half context read all use it |
| tick_deadman base ([65]) | the resting stop AT THE FILL (`position.stop_price_at_fill`), set once; named fallbacks: no stamp ⇒ the current stop (`resting_stop_source`), unreadable / not below the entry ⇒ `None` | §8 (the one run of record): 14 d (81 legs / 34 symbol-days) +$461 [+170, +818] print, +$293 [+44, +607] bid, +$278 [−61, +743] bid+15.3 s vs the old base + ratchet; 2026-09-11 (22 legs) +$50 [−69, +229] / −$4 [−123, +116] / +$260 [+10, +519] |
| ratchet ([65]) | none before the trigger — named fallback `no_pre_trigger_ratchet_until_completed_swing_facts_wired` | the rolling count-half ratchet on this base costs +$363 [+89, +769] print over 14 d; a completed-low ratchet waits for #1408's facts to be wired |
| count-half context | first of `swing_low_prev`, `swing_low_now`, `buy_support_px` strictly < entry at the fill; receipt only | the OLD base: p50 0.23 R (today) / 0.31 R (14 d) below entry; 43/56 of its 14-d floor exits printed back above entry within 5 min |
| ledger context ([65] review) | `cont_context`: the median of the [62] ledger's completed-cycle depths, dollar and scale-free, with each cycle's close print index, `max_cycles` / `cycles_truncated` and the last feed's cause; receipt only | the #1419 draft base: its median came from the scanner's cold-start cycles on 17/18 (today) and 24/34 (14 d) of the legs where it bound; `CYCLE_LEDGER_MAX_CYCLES` = 16 truncated the ledger at 4/81 fills |
| trail authority ([65] review) | a readable armed leg: `binding = tick_verdict`, `deadman = static_at_fill`, `trailing_floor = None` (named fallback) — G and D are the profit protection; the chandelier and the quote locks stay off | the measured configuration has no chandelier; the pre-[65] bypass was paired with the monotone ratchet, which this base no longer has |
| frontier | the last WALKED print's `(observed_at, id)`; the entry fill on the first pass | never `as_of`; an unreadable batch leaves it (review of #1385, major) |
| stale_tape bound | `chili_momentum_l2_confirm_window_s / 2` = 7.5 s — withholds G / D only; the walk always runs; `accel_prev` not advanced | the feature's own halt-gap rule at the END of the window |
| shipped latency | decision tick → POST = one continuation pulse (0.5 s) + the cancel round-trip; priced at pulse 2's HELD-tick bid (the [48] envelope, IQFeed L1 first, never a snapshot) | `tests/test_exit_verdict_g_whole_exit_seam.py` on the real claim tables |
| read timeout | 2000 ms = `_EVENT_TICK_MIN_SPACING_S` | nested-savepoint rollback |
| resting broker stop | released by the whole-close handoff; rests `live_runner.deadman_stop_buffer` = `max(0.25% avg, 25% R, 1¢)` (the named `DEADMAN_STOP_BUFFER_*`) BELOW `position.stop_price` and is inert in premarket | the last-resort floor; the software bid-stop at `position.stop_price` (1-s bid confirm) sits at the base until C4 / A2 lift it |
| arming | none needed: every equity leg with a readable anchor, from the first held tick after the fill | the measured walk starts at the fill; opinion sites are receipts |
| kill switch | none (LIVE + ON); named fallbacks `no_equity_tape` / `entry_fill_anchor_missing` → `momentum_break_stop` | doctrine "no dark flags" |

## 6. Receipts

`live_opinion_exit_armed` (+ `armed_exit`, `superseded`); `live_exit_verdict_armed{leg_high, n_since_high,
verdict, rollover, deadman, min_prints, window_s_binding, window_prints, prints_since_entry,
seconds_since_entry, trigger_order}`; `live_exit_verdict_fired{trigger, reason, accel_prev, accel_now,
prints_since_entry, prints_since_high, bid, exit_fraction = 1.0, exit_fraction_derivation, binding, verdict,
rollover, leg_high, entry_px, last_print, level, remaining_qty, decision_as_of}`; `live_tick_deadman_exit{level,
level_source, crossing_print, prints_scanned, batch_window, ratchets, resting_stop, deadman_base, ratchet,
remaining_qty, stale, prints_since_entry, prints_since_high, exit_fraction, binding}` — `deadman_base` (also on
`live_exit_verdict_armed.deadman.base` and `live_exit_filled.exit_verdict.deadman.base`) = `{level, base_source,
binding, fallback_reason, statistic, resting_stop, resting_stop_source, stop_price_now, entry_px, risk_R,
risk_R_basis, distance_R, cont_context, ledger_lag_s, count_half_context}`, where `cont_context` = `{binding:
false, premise, no_candidate_reason, n_cycles, depths, depths_pct, close_print_index, cont_depth_p50,
cont_candidate, cont_candidate_distance_R, cont_depth_pct_p50, cont_candidate_pct, cont_candidate_pct_distance_R,
ledger{day, expected_day, through, n_prints, n_cycles_total, max_cycles, cycles_retained, cycles_truncated,
max_cycles_binding, pullback_frac, caught_up, caught_up_scope, feed_reason, feed_budget_hit, feed_reads,
feed_fed}}`; `live_exit_trail_authority{bypass, binding, fallback_reason[, deadman, deadman_level,
trailing_floor, trailing_floor_fallback, profit_protection]}` (on change); `live_exit_verdict_unavailable{binding}` (once);
`live_exit_verdict_unreadable{why}` (on change; `stale_tape` carries `walks_and_executions_continue: true`).
Every one carries `derivation, as_of, phase, state, bid, bbo_source, bbo_age_s, bbo_fallback_engaged` (the
[48] envelope), `tape_frontier_age_s, stale_tape_bound_s, opinion_exit_armed, exit_fraction,
exit_fraction_derivation`. `live_exit_filled`, the bailout submit and the whole-exit submit carry `exit_verdict`.

Parity alphabet: `live_opinion_exit_armed`, `live_exit_verdict_armed`, `live_exit_verdict_fired`,
`live_tick_deadman_exit` in; the (retired) ratchet and the mechanics out.

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

## 8. [65] The tick deadman base (2026-09-11) — the leg's own resting stop at the fill

Day 1 of #1385 + #1407 (2026-09-11, 22 legs): 18 exits were `tick_deadman_stop` within 20–60 s; 16/18 printed
back above the entry within 5 min, 18/18 within 15 min. The old base (the count-half low of the 255 prints at
the fill) sat p50 0.23 R (today) / 0.31 R (14 d) below the entry (R = entry − `position.stop_price`) — inside the
pullbacks the tape itself continues from — and the rolling count-half ratchet could lift it ABOVE the entry on
the first held tick (TNON 22129 09:26:30Z: 6.76 → 7.2586 on entry 7.13, out 2 s later).

**Rule.** ONCE: `level = position.stop_price_at_fill` — the resting stop the fill handler set (and stamped),
the stop R is defined by, checked on EVERY print. No pre-trigger ratchet. Named fallbacks: a leg with no stamp
reads the current stop with `resting_stop_source = position.stop_price_no_fill_stamp_named_fallback`;
`fallback_reason ∈ {resting_stop_unreadable, resting_stop_not_below_entry}` ⇒ `level = None` (the trail
authority names `deadman_level_unproven`; the chandelier and the bid-stop manage the leg).

**Why not the ledger's continued-pullback median (the #1419 draft).** The draft's base was
`max(stop, entry − median(hi − pb_low))` over the completed cycles of the [62] ledger. The review measured what
that median is: at a cold start the scanner seeds `hod = spike_low` = the ledger's first print, so a one-tick dip
and a one-tick new high complete a "cycle", and the ledger starts at the day start / our first subscribed print,
p50 100 min before the entry. On 17/18 (today) and 24/34 (14 d) of the legs where the draft's candidate bound,
most of its cycles closed within 60 s of the ledger's first print (cold-start depth p50 1.19% / 1.55% of the high,
against 4.96% / 8.93% for the later cycles); without them the resting stop binds on 16/18 and 21/34. The depths
are also dollars (a runner's premarket $0.08 cycles put the level 0.16 R under an $8.84 entry) and the ledger
keeps at most `CYCLE_LEDGER_MAX_CYCLES` = 16 rows (4/81 fills truncated). The median therefore travels in the
receipt as `cont_context` (`binding: false`, dollar and scale-free, each cycle's `close_print_index`, the
truncation, the last feed's `feed_reason` / `feed_budget_hit` — `caught_up` describes the last feed call only),
and the next re-measure can separate the cold-start cycles without a clock. Context reasons:
`no_candidate_reason ∈ {no_tape_cycle_state, tape_cycle_ledger_other_day, tape_cycle_ledger_not_caught_up,
entry_unreadable, no_completed_cycles, cont_candidate_not_below_entry}`.

**Measurement — the one run of record** (read-only; `scripts/deadman_base_replay_65.py --legs-json today=…
--legs-json 14d=… --tape-cache …` over the scout's `t65_*` legs and tape cache; the SHIPPED count_v1 features,
scanner, base and context; the floor checked on every print; the software bid-stop at `position.stop_price`
with its 1-s confirm; the broker stop in RTH (zoneinfo); the C4 viability lifts at their actual event times —
**observable up to the actual exit only**: a variant that holds longer cannot see a C4 lift that would have landed
later, so the no-C4 variant bounds C4; G every 25 prints for the first 400 then 100, D every 100; 60-min
horizon; priced print / bid / bid 15.3 s later — the measured decision→submit p50). Paired diffs are summed per
symbol-day, 2000× cluster bootstrap (`random.Random(65)`), 90% CI:

| sample | old base + ratchet (S0_live) | [65] base, no ratchet (N1_c4, shipped) | paired diff (print / bid / bid+15.3 s) |
|---|---|---|---|
| 14 d, 81 legs / 34 symbol-days | −429.64 / −662.00 / −751.55 | +31.45 / −369.43 / −473.32 | +461 [+170, +818] / +293 [+44, +607] / +278 [−61, +743] |
| 2026-09-11, 22 legs / 5 symbol-days (14 TNON) | +197.92 / +151.59 / +18.16 | +248.00 / +147.24 / +278.41 | +50 [−69, +229] / −4 [−123, +116] / +260 [+10, +519] |

| paired diff (A − B) | 14 d print | 14 d bid | 14 d bid+15.3 s | today print | today bid | today bid+15.3 s |
|---|---|---|---|---|---|---|
| N1_c4 − S0_live (shipped − old) | +461 [+170, +818] | +293 [+44, +607] | +278 [−61, +743] | +50 [−69, +229] | −4 [−123, +116] | +260 [+10, +519] |
| N1 − S0_live (no C4) | +461 [+166, +814] | +300 [+38, +619] | +301 [−42, +798] | +1 [−149, +159] | −48 [−191, +90] | +246 [+10, +491] |
| N1_c4 − N1 (C4 on the shipped base) | −0 [−32, +30] | −7 [−38, +16] | −23 [−85, +16] | +49 [+0, +115] | +44 [+0, +95] | +14 [+0, +42] |
| N1_c4 − N4a_c4 (shipped − #1419 draft) | −48 [−134, −2] | −45 [−114, −3] | −26 [−82, +12] | −50 [−100, +0] | −46 [−102, +0] | +24 [−25, +98] |
| N1_c4 − N4p_c4 (shipped − scale-free median) | −37 [−123, +12] | −42 [−112, +0] | −27 [−82, +7] | −44 [−88, +0] | −43 [−93, +0] | +24 [−25, +98] |
| N1_c4 − N1_ratchet_c4 (the ratchet's cost) | +363 [+89, +769] | +178 [−49, +514] | +269 [−80, +772] | +37 [−55, +172] | +35 [−46, +151] | +292 [−8, +591] |

Where the base sits, `(entry − base)/R`: old p50 0.23 R (today) / 0.31 R (14 d); shipped 1.0 R by construction
(one 14-d leg has no broker-stop record, so no resting stop: `resting_stop_unreadable`); the draft 0.48 R / 1.0 R.

The draft was ahead of the shipped base by +$48 print / +$45 bid over 14 d (CI just clear of 0) on 3–4
symbol-days, all one way: a tighter floor on the legs where the cold-start median happened to sit above the
resting stop — chosen by when the ledger started, not by a measured rule. Its scale-free form is inside the noise
at print (+37 [−12, +123]). That a floor between 0.3 R and 1 R may carry an edge is the next re-measure's
question, on a candidate derived without the cold-start cycles (`cont_context.close_print_index`).

**Why the scout's first table differed.** The scout's sim floored every variant at the broker deadman stop
(`live_deadman_stop_placed.stop_price`), which rests `max(0.25% avg, 25% R, 1¢)` BELOW `position.stop_price`
and is inert in premarket; live exits at the software bid-stop first. Modelled here, the conclusion holds.

**Open.** (1) C4 (`viability_degraded_tighten`, a scanner-score opinion with the literals 0.85 / 0.995) still
lifts the SOFTWARE bid-stop; since the fill stamp it can no longer reach the deadman base. On the shipped base its
effect is inside the noise both ways (14 d −0 / −7 / −23, today +49 / +44 / +14). Whether "the tick deadman is
the only software stop authority" should cover it — as the #1385 review made it cover the quote stop-movers — is
the operator's call. (2) A completed-low ratchet from #1408's facts. (3) Re-measure on ≥ 3 new sessions that are
not dominated by one name: `python scripts/deadman_base_replay_65.py --window new=FROM,TO --tape-cache DIR`.

## 9. Review of #1419 (2026-09-11) — findings and how they are closed

| finding | fix | test |
|---|---|---|
| the binding `cont_depth_p50` was the scanner's cold-start noise (major) | the base is the resting stop at the fill; the median is `cont_context` (`binding: false`) with each cycle's `close_print_index`; re-measured (§8) | `test_a_cold_start_ledger_never_sets_the_level_the_review_repro`, `test_a_cold_start_ledger_is_context_the_fill_stop_binds_and_the_micro_pullback_holds` |
| the depth was dollars, not scale-free: a runner's premarket cycles put the level 0.16 R under the entry (minor) | the ledger never sets the level; the context carries `depths_pct` and the scale-free candidate beside the dollar one | `test_a_runners_premarket_ledger_never_sets_the_level_and_the_context_is_scale_free` |
| the base read the stop at the first READABLE tick, so a C4 / A2 lift could freeze a print deadman at `avg × 0.995` (minor) | the fill handler stamps `position.stop_price_at_fill`; the base reads it (named no-stamp fallback); `stop_price_now` reports the lifted stop | `test_a_c4_lift_before_the_first_readable_tick_never_reaches_the_base`, `test_a_leg_filled_before_the_stamp_reads_the_current_stop_and_names_it`, `test_the_fill_handler_stamps_the_stop_at_the_fill_once` |
| the trail authority still named the (now static) deadman as the trailing authority (minor) | `binding = tick_verdict`, `deadman = static_at_fill`, `trailing_floor = None` + the named fallback, `profit_protection = [G, D]`; behaviour unchanged (the measured configuration has no chandelier) | `test_the_trail_authority_says_nothing_trails_on_a_readable_leg`, `test_trail_authority_requires_this_ticks_successful_read_and_recovers` |
| the design doc quoted two different runs; §8 omitted the other-day reason (minor, ×2) | §5 and §8 quote the one run of record; the reason set is complete | — |
| the derivation-prose test pinned the CI digits (minor) | it pins what the derivation must say and THAT it carries a paired CI (regex), never the digits | `test_the_module_is_pure_and_the_derivations_carry_the_measurement` |
| `CYCLE_LEDGER_MAX_CYCLES` = 16 sat in the decision window with a false "never truncates" premise (minor) | the ledger is context only; the premise is corrected in `tape_cycles.py`; the receipt carries `max_cycles`, `cycles_truncated`, `max_cycles_binding` | `test_the_16_row_cap_is_reported_when_it_truncates_the_ledger` |
| the replay's C4 was truncated at the actual exit while the text said "as it is today"; C4 is an opinion stop-mover (minor) | the replay and §8 say C4 is observable up to the actual exit only; the no-C4 variant bounds it; C4 can no longer reach the deadman base (the stamp); its bid-stop lift is the operator's call (§8 Open 1) | `test_a_c4_lift_before_the_first_readable_tick_never_reaches_the_base` |
| the replay's RTH window (and its tape start) were EDT clock literals; the buffer inversion copied the runner's literals (minor) | zoneinfo America/New_York for both; the runner's `deadman_stop_buffer` / `DEADMAN_STOP_BUFFER_*` are named once and imported by the replay | `tests/test_deadman_base_replay_65.py`, `test_the_deadman_stop_buffer_is_one_named_formula_the_replay_imports` |
| `tape_cycle_ledger_not_caught_up` could come from one transient feed failure, and the receipt dropped the cause (minor) | the ledger no longer decides; the context carries `feed_reason`, `feed_budget_hit`, `feed_reads`, `feed_fed` and `caught_up_scope = last_feed_call_only` | `test_the_context_carries_the_last_feeds_own_cause` |
