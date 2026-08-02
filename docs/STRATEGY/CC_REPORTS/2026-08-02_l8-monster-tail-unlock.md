# 2026-08-02 — L8: pagbukas ng monster tail (#968, MERGED → b5be09e)

## TL;DR

Ang tunay na pumapatay sa mga monster window (JEM/JLHL/JZXN — ang Ross winning
days) ay hindi ang bench o ang dip geometry kundi ang **A2 schedule ×0.0**
(late 14:30-16:00 / afterhours 16:00-20:00 ET, plus closed bands) na may
kasamang **pending-park bug** na nagyeyelo sa buong session evaluation
pagkatapos ng unang blocked candidate. Ang JEM window ay 100% afterhours — ang
candidate nito ay na-park ISANG ORAS bago ang day high. Pagkatapos ng
dalawang-bahaging fix, ang JLHL ay nag-entry sa kauna-unahang pagkakataon sa
buong lever program: **+23.63 sa 4 entries (arm A) vs 0 entries (arm B)** —
eksaktong flag-attributed.

## Ang daan papunta rito (isang gabi, tatlong pivot)

1. **Bench bypass (unang L8)** — REFUTED ng sariling adversarial study bago pa
   ang disenyo (ang purong day-geometry ay baliktad ang selectivity sa live
   frame) at pagkatapos ay napatunayang inert sa FSM surface ng instrumented
   probe (maling trigger vocabulary; ang vwap_reclaim ay self-unbenched na ng
   bench). Na-revert sa loob ng PR.
2. **Post-detection dissection** — natagpuan ang A2 park: sa sched≤0, walang
   demote — ang FSM ay nananatiling naka-park sa `live_pending_entry` hanggang
   tape end (kaya `final_state=live_pending_entry` + tahimik na kamatayan; may
   print-filter gap din ang driver na nagtatago ng pending-state vetoes).
3. **Ang tunay na L8** (operator-approved ang policy): (a) park-bug fix —
   demote sa WATCHING gaya ng ibang pending vetoes (walang flag; bug fix); (b)
   kondisyonal na late/AH placement — ×0.5 kapag STRUCTURAL dip-reclaim trigger
   (measured FSM vocabulary: flush_dip_buy / raw_break / vwap_reclaim /
   abcd_break / double_bottom_break + _tick_ok) AT monster day (px/session_low
   ≥ 1.5). Ang random AH chop na pinagmulan ng ×0.0 (14d: 1W/11L −$72.65) ay
   nananatiling sarado. 14th roster flag
   `chili_momentum_late_ah_monster_placement_enabled`.

## Proof (2 arms × 6 windows @aa3a2b4, roots replay_957_l8a4/l8b4)

| Window | A (ON) | B (OFF) | Verdict |
|---|---|---|---|
| JLHL 07-09 | **4e +23.63** | 0e | **UNLOCK PROVEN** |
| JZXN 07-21 | 3e −5.07 | 0e | dokumentadong cost (crest leak sa monster floor; bounded) |
| CLRO / LHSW / PLSM | −11.37 / −253.12 / 0 | identical | event-for-event IDENTICAL (146/893/449) |
| TC 07-01 | 4e +24.21 | 4e −5.55 | HINDI lever — latent nondeterminism (tingnan sa ibaba) |

Netong flag-attributed: **+18.56** sa set, at bukas na ang dating
structurally-unplayable na monster tail.

## Dalawang malaking natuklasan sa daan

1. **Replay nondeterminism (TC ±$30)**: apat na tape reads
   (pipeline.py:519/:602/:784/:966) ay `ORDER BY observed_at` na WALANG
   tie-break; ang tape ay equal-timestamp-heavy (JEM: 287k/341k rows sa tie
   groups); ang equal-ts permutation = f(query plan + heap layout) = f(sink
   write history) (DELETE hindi TRUNCATE ang reset). Ang tick-rule aggressor
   imbalance ay sequence-sensitive → ~1e-5 qty diff → chaos amplification sa
   trail ratchet. Ang "byte-parity" sa kasaysayan ay steady-state heap cycle
   lang pala. FIX (nakahiwalay na task): tie-break `, id ASC` (tape-faithful),
   PYTHONHASHSEED pin, TRUNCATE reset.
2. **JEM replay-churn pathology**: sa gising na FSM (park fix), ang 100%-AH na
   1.19M-tick window ay nag-timeout kahit 7200s (buong pending chain kada tick
   sa demote-refire loop). Ang LIVE lane ay real-time-bounded — hindi apektado.
   Follow-up: pending-refire cooldown design, tapos solo JEM run. JEM =
   dokumentadong exclusion sa proof na ito.

## Susunod

- Determinism-fix PR (tie-break/hashseed/TRUNCATE) — pinakamataas na presyo sa
  integridad ng lahat ng susunod na proof.
- Pending-refire cooldown (churn bound) + solo JEM verification.
- Paper live-soak ng L8 events (`live_entry_late_window_monster_placement`) +
  L1/L4 counters kapag ACTIVE na ang PAPER lane ni Codex.
- Driver print filter: idagdag ang wait_/veto/deferred/pending_place para hindi
  na invisible ang pending-state kills.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
