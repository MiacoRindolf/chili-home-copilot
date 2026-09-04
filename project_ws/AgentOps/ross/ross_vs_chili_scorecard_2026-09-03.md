# Ross vs CHILI — master scorecard (binuo 2026-09-03/04)

Pinagmulan: 79 na video sa `ross_video_evidence/` (transcript + panel screenshot), inilabas ng 26
ahente, pinagsama nang deterministiko ng `scratchpad/ross_master_build.py` →
[`ross_master_ledger.json`](ross_master_ledger.json). Ang bawat cross-reference ay tinanong sa
LIVE `chili` DB (sessions, events, viability, tape), hindi sa alaala.

## 1. Ang bilang

| | halaga |
|---|---|
| Video na na-proseso | 79 |
| Ross trade na naisulat (pagkatapos ng dedupe; 234 raw row) | 187 |
| Araw na may Ross P&L | 36 (2026-03-04 … 2026-09-02) |
| Ross gross sa mga araw na iyon | +$643,764 |
| Panalo / talo na trade | 85 / 42 = 67% |
| Symbol-day na na-cross-reference sa CHILI DB | 68 |
| Ross P&L sa mismong 68 symbol-day na iyon | +$428,050 |
| **CHILI P&L sa mismong 68 symbol-day** | **$0.00** |

Zero. Hindi maliit — **wala**. Walang isa mang trade ni Ross sa 68 na sinuri ang nasagot ng isang
CHILI fill.

## 2. BAKIT — ang mekanismo, hindi ang bilang

Inuri ko ang 68 ayon sa mekanismong nakasulat sa ebidensya (hindi sa hula):

| klase | n | Ross $ | ano ang nangyari |
|---|---:|---:|---|
| 1. Wala pang lane | 18 | 163,277 | Ang unang `trading_automation_sessions` row ay 2026-04-20 (paper) at 2026-06-06 (live). Lahat ng bago nito ay hindi kayang sagutin. |
| 2. Patay ang control loop | 30 | 157,880 | Buhay ang produser ng viability, patay ang konsumidor. Hal. FCUV 07-31: 2,030 `live_eligible` row habang patay ang loop 03:58→16:03Z. |
| 3. Kill-switch lockout | 1 | 23,729 | 06-15: isang −$38 na trail-stop ang nagtulak sa global realized PnL sa −64.93 vs −59.84 na cap; 9 oras na lockout; CUPR at JRSH parehong nawala. |
| 4. Huli o naka-freeze ang selection | 2 | 14,000 | NUWE 07-30: tumitibok ang loop pero naka-freeze ang `last_source_event_at` sa 09:00Z. WYHG 08-06: unang batch 12:37Z, tapos na ang entry ni Ross. |
| 5. Hindi nakumpirma ang arm | 1 | 39,000 | UPC 06-29 session 9498: `live_arm_requested` 12:38:45Z, `live_eligible=true`, risk allowed — pero walang `live_arm_confirmed`. |
| 6. Gate/basis sa runner | 8 | −13,190 | Backside bench, `no_bbo`, ortex coverage, `volume_below_1p5x_avg`. |
| 7. Huli ang universe admission | 1 | −716 | MSS 07-24: unang score 7 minuto pagkatapos ng pop. |
| 8. Iba | 7 | 44,070 | Tahimik na runner (0 decision event sa 5 min), late na halt detector. |

**Ang headline: 48 sa 68 (71%, $321k) ay hindi kalabanan ng estratehiya. Uptime iyon.** Hindi
natin natalo si Ross sa mga kasong iyon — wala tayo doon. Ang totoong labanan ng estratehiya ay
ang 8 kaso sa klase 6, at doon ay **nawalan si Ross ng pera** (−$13,190 net).

## 3. Apat na hinala na PINABULAANAN ng sukat (mahalagang huwag ayusin ang hindi sira)

Bago mag-PR, sinukat ko ang bawat "kandidatong bug" laban sa kasalukuyang DB. Apat ang bumagsak:

1. **"Runner price ≠ tape" (F4).** Ang WETO 08-17 at PFSA 08-18 ay nag-bench gamit ang presyong
   hindi tugma sa tape. Sinuri ko ang 400 pinakabagong `live_entry_backside_benched` event laban sa
   trade tape sa ±20 s: **351 na may tunay na `cur_px`, 2 lang ang hindi tugma (0.6%), at pareho ay
   after-hours na may 3-4 print lang.** Hindi na ito buhay. (Unang pagsukat ko ay nagsabing 9.8% —
   mali iyon: ikinumpara ko ang `cur_hod`, na *dapat* nasa itaas ng kasalukuyang saklaw.)
2. **Ortex coverage gate.** Tinanggihan nito ang 35 candidate ng SXTC 08-18 sa mismong dip ni Ross.
   Ngayon: `live_entry_ortex_coverage_unavailable` = 121 event, huli **2026-08-18**;
   `live_entry_ortex_neutral_fallback` = 6,066 event mula 08-18 hanggang ngayon. **Naayos na.**
3. **Tahimik na arm confirm.** 136 sa 3,379 arm sa 14 araw ang walang `live_arm_confirmed` — pero
   lahat ng 136 ay may `session_cancelled` na may dahilan (`by=automation_monitor`,
   `previous_state=live_arm_pending`), na dumating **1.9 s** (avg) pagkatapos ng request. Normal na
   supersede iyon, hindi ang tahimik na UPC-class na kabiguan.
4. **Universe/selection outage.** 1,001,890 `momentum_viability_history` row sa huling 18 oras sa 975
   simbolo (63% `live_eligible`); umaandar ang prescreen at ang fast-path rotation. Malusog.

## 4. Ang natitirang tunay na puwang

- **Backside bench = ang pinakamalaking gate na buhay pa.** 6,765 `live_entry_backside_bench_veto`
  sa 261 session sa 14 araw. Tama ang basehan ng presyo (§3.1) — ang tanong ay kung tama ang
  *patakaran*. Ang SDOT 06-26 ang malinaw na kaso: nag-latch ito sa `benched_at_hod=17.86` mula sa
  premarket spike na sinabi mismo ni Ross na kupas na, tapos ni-veto ang `deep_reclaim_tick_ok` ×5 at
  `abcd_break_tick_ok` ×5 sa eksaktong lebel niya. Ito ay A/B na tanong, hindi bug — kailangan ng
  replay sa hydrated tick.
- **Universe admission latency.** VTIX 07-27: natapos ang buong round trip ni Ross (13:17:15–13:19:15Z)
  **bago pa** namin nalaman na umiiral ang simbolo (prescreen run 1309 @ 13:31:36Z). Istruktural ito:
  ang mga pinakamagandang trade niya ay mga pangalang zero-to-hero sa loob ng minuto.
- **Tahimik na runner.** ZDAI 06-26 at 5 iba pa noong umagang iyon: 20 tick sa 5 minuto, **zero**
  decision event, tapos ni-reap ng auto-arm. Walang trigger_wait, walang veto, walang candidate — kaya
  walang masasabi kung bakit.

## 5. Ang hydration worklist

55 symbol-day ang minarkahang `hydrate_needed`. Pagkatapos alisin ang mga kaso ng klase 1-2 (walang
matututunan sa tick kung walang buhay na konsumidor), **ang mga sulit lang na i-replay ay ang mga
kaso kung saan BUHAY ang lane at may ginawang desisyon**: SDOT 06-26, ZDAI 06-26, ILLR 06-25,
UPC 06-29, IPST 08-17, WETO 08-17, PFSA 08-18, SLE 08-18. Walo. Nasa
`ross_master_ledger.json` → `hydration_worklist` ang buong listahan na naka-sort ayon sa Ross P&L.

## 6. Susunod

1. Hydrate ang 8 na kaso sa itaas (`scripts/historical_tick_hydrator.py --symbol-day SYM:DATE`), isa-isa,
   hindi sa oras ng merkado at **hindi sa 07:50–08:35Z** (Codex preflight window).
2. Replay ang FSM sa mga window na iyon at sagutin ang isang tanong: kung tinanggal ang backside bench,
   kikita ba o malulugi?
3. Ang uptime ang totoong lever. Ang $321k sa klase 1-2 ay hindi nangangailangan ng bagong estratehiya —
   kailangan lang na buhay ang lane at kumakain ang konsumidor.
