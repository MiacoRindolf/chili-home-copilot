# Ross live-stream study + lane activation — 2026-08-17 (Lunes)

**Utos ng operator:** panoorin ang Warrior Trading stream/room nang buo, i-compare ang bawat
galaw ni Ross sa lohika ni CHILI sa parehong minuto, itala ang lahat ng matututunan, at
ayusin ang anumang problemang lumitaw. **Resulta:** 27 aral, 6 na fix na na-merge at
na-deploy nang live (kasama ang sagot sa buong-programang "0 admitted" na misteryo), at
isang malinaw na ranked na improvement backlog.

---

## 1. Ang araw ni Ross (na-obserbahan via screencast + Live Captions + scanners)

| Oras (ET) | Pangyayari |
|---|---|
| 06:56 | Live; premarket scanners: IPST/IVF/TRUG/JFU/MYSZ/SLE |
| 07:0x | Sidelines: "Chinese stocks with no news... I'm going to sit on the sidelines" |
| ~08:00 | Unang trade: IPST breakout attempt = **−$12,854** (kontra sa sarili niyang extension warning) |
| 08:15 | Walang revenge trade; diagnosis: "attention is on other stocks?" |
| 08:25 | **Re-entry sa dip** (7.33) matapos kumpirmahin ng move: adds 7.55/7.75, scalp cycle |
| 08:33 | Realized **+$63,251**; conviction logic: "UCL had failed... this was still the leading gainer" |
| 09:10 | Giveback sa 8.50-break (−$10.6k, "a lot of topping tails") → ~$52.6k |
| 09:28 | **Nag-sign off BAGO ang open**: "Don't give back... chasing the ranges at the open... halts... challenging" |

**Ang buong kita ni Ross ay PREMARKET.** Hindi niya tinrade ang open.

## 2. Parity scorecard (CHILI vs Ross, sabay na sinukat)

**Selection: 6/6 na tugma** — lahat ng Warrior top gainers (IVF, IPST, TRUG, JFU, MYSZ,
SLE) ay nasa pipeline ni CHILI sa parehong minuto; ika-6 na sunod-sunod na selection hit.

**Rejection parity:** OABI (float 145M) at CRGO (51M) — float-vetoed ni CHILI, wala rin
sa 5-Pillars ni Ross; AGRZ ($0.41) — "too, too, too cheap" kay Ross = $1.00 floor ni
CHILI; YYAI (reverse split, 45M float) — kapwa umiwas; UCL — float-vetoed / Chinese+200MA
caution. **Ang mga gate ay hindi lang tumutugma sa pagpili — tumutugma sa pagtanggi.**

**Discipline parity:** ang IPST −$12.8k unang talo ni Ross ay isang extended-breakout
chase na hindi kukunin ng ATR anti-chase ni CHILI; ang VWAP-benching ni CHILI (SLE
backside) = ang "not comfortable below VWAP" ni Ross; ang post-loss na paghinto ni Ross =
ang 2-strike/cooldown natin.

**Conversion: 0 vs +$52.6k — ITO ang buong laban**, at natukoy ngayong araw kung bakit
istruktural na zero ito (sec. 3).

## 3. Ang mga pader na natagpuan at inalis (lahat MERGED + DEPLOYED live)

1. **#1036 — Leader starvation:** ang reap cooldown ay gumugutom sa board #1 (IVF +190%
   ay naka-sit-out habang mga laggard ang may slots). Board-#1 exemption sa arm walk +
   displacement newcomer walk.
2. **WINDOW_ENV v3.6 — ANG 0-ADMITTED ROOT CAUSE:** sa event-loop driver mode, ang
   `queued_live` ay WALANG dispatch path (walang branch sa `_on_tick`; patay ang IQFeed
   notify listener sa blangkong bridge pin). Lipat sa batch scheduler driver (10s) —
   unang `live_runner_started` sa kasaysayan ng lane makalipas ang ilang minuto.
3. **#1037 — v3 ask_side_pressure_lock:** ang "fixated on the level two, specifically
   the ask price" ni Ross, mekanisado: ask-wall building + bid-refill stall + book roll →
   ratchet-only exit tighten mula sa sarili nating IQFeed depth (2s cadence).
4. **#1038 — Premarket entry window:** TATLONG naka-stack na hard-coded RTH-only gate
   (window/instruction/tif) habang ang buong kita ni Ross ay premarket. Flag-gated
   premarket acceptance na sarado pagsapit ng 09:30 (walang crossover) + premarket
   spread-gate tighten.
5. **WINDOW_ENV v3.7 — Ortex tilt off:** isang SIZING tilt na naging VETO (lahat ng entry
   ay bumagsak sa `ortex_batch_coverage_unavailable` — walang Ortex data/credentials).
   Tilt never veto.
6. **#1039 — Time-share runner escape (Option C, desisyon ng operator):** ang alpaca
   adaptive-risk sizing ay nangangailangan ng capture provider na tanging ang sealed
   service ni Codex ang nakakapag-install. Escape sa likod ng time-share flag: legacy
   sizing na may TUNAY na per-trade hard cap (1% ng equity, ENFORCED bilang reservation
   veto), serial posture, sealed-lane 3-key fence, replay inertness, fail-closed
   ceilings. Dalawang adversarial verification pass (2 blocker + 4 major + 3
   depth-of-defense na isyu — lahat inayos); 20/20 bagong tests.

**Estado pagkatapos:** ang unang na-block na entry ay `live_entry_midday_deweighted` —
puro signal-quality gating na lang ang natitira. Papalitan ng tamang capture integration
ang escape sa Codex reseal (Aug 19+).

## 4. Ranked improvement backlog (mula sa 27 aral)

1. **Re-entry pathway pagkatapos ng stop-out** (Aral #23, ang pinakamalaki): ang −12.8k
   → +63k ni Ross ay ang dip re-entry nang may kumpirmasyon. Tugma sa replay finding na
   ang re-entry lockout ay winner-killer. SUKATIN muna: gaano katagal sana na-lock ng
   post-loss cooldown natin ang 7.33 dip re-entry; leader/confirmation-aware cooldown.
2. **Consolidation-duration bilang setup maturity** (#14, #21): "the longer consolidated
   without rolling over, it's good" — sana ito ang pumigil sa maling-timing na unang
   entry ni Ross. Walang ganitong konsepto si CHILI.
3. **Premarket entry soak** (bukas): unang araw ng #1038 sa totoong premarket — bantayan
   ang spread-gate/tickbreak behavior sa manipis na tape.
4. **Halt-aware trigger suppression:** ang IPST continuation fire ay pumutok habang
   suspected-halt (stale-window tick stats). May halt detection na; idugtong sa trigger.
5. **Subscription lag sa sariwang HOD names** (MNDR 0 ticks, APUS 4 ticks): ang
   universe/L1 subscription ay hindi humahabol sa bagong sumulpot na pangalan. (Nuansa:
   wala rin sa 5-Pillars ni Ross ang mga ito — mababa ang priyoridad maliban kung
   magsimulang mag-trade si Ross ng ganitong klase.)
6. **Volume-on-red-candle distribution tell** (#18) + **attention-rotation** (#16, may
   leader-focus na tayo — ang rotation DETECTION sa pagitan ng mga leader ang kulang).
7. **Ortex batch capture** (operator-side credentials) para maibalik ang squeeze-fuel
   tilt bilang tilt.
8. **Hygiene:** stale session rows (ended_at NULL sa lumang araw); WETO no_bbo probes;
   tape-accel replay mirror (minanang parity gap ng buong exit stack).

## 5. Mga runbook/gotcha na naitala sa memory

Post-reboot IQFeed revival (CHARTS auto-login → bridges pagkatapos); window relaunch
routine (kill → linisin orphans → bump epoch → ACCEPT verify → watchdog pids);
`venue='alpaca'` sa session queries; `ts` sa events table; time-of-day-dependent tests —
laging i-verify sa pristine main sa parehong wall-clock (5 beses nangyari ngayong araw,
lahat pre-existing).

---
*Mga kaugnay: PR #1036–#1039; project_ws/AgentOps/ross_live_monitor/2026-08-17_notes.md
(buong minutong log na may verbatim captions); memory: project_zero_admitted_rootcause_0817.*
