# Ross-vs-CHILI 5-day replay scorecard (2026-07-08 … 07-15)

**Petsa ng run:** 2026-07-16 gabi (operator order: "gawin mo na ngayon habang di bukas ang market... most recent 5 days na meron tayong tape")
**Instrument:** `scripts/replay_v3_fsm_window.py` sa `codex/ross-replay-validation` worktree (main-based, Jul-13 build) — REAL FSM force-armed sa recorded tape; sim EQUITY $13k / RISK $130; FULL_MIRROR; ARM=on. Sinusukat ang **capture shape** (pasok/hold/exit sa tamang mga pangalan), hindi dollar-parity kay Ross (2,000–4,000 shares siya).
**Ross data:** transcript-extracted + web-verified catalysts, $2k-challenge recap series (Days 17–20). Evidence: `project_ws/AgentOps/ross_video_evidence/<videoId>/` (old tree). Buong replay log: session scratchpad `ross_replay_results.log`.

## Coverage

| Araw | Tape | Ross day |
|---|---|---|
| 07-08 | ✅ buo | **NO-TRADE day** (discipline benchmark; walang ni-replay) |
| 07-09 | ✅ mula 11:33Z | Day 17 — VRAX one-ticker day (big acct **+$24,359**; small-acct P&L hindi sinabi) |
| 07-10 | ❌ zero premarket (port-exhaustion) | walang challenge session (big-acct recap lang) |
| 07-13 | ✅ buo | Day 18 — PLSM, VEEE = **+$205.18** |
| 07-14 | ✅ buo | Day 19 — NXTC, UBXG = **+$2,140.99** |
| 07-15 | ✅ buo | Day 20 — ERNA, VIVS = **+$3,652.96** |
| 07-16 | ❌ unang tick 18:23Z (bridge sa maling DB 11:46→14:23 ET) | Day 21 — RUBI +$587.78 (HINDI replayable) |

## Resulta (CHILI replay sa mismong Ross windows)

| Window | Ross | CHILI replay | Fills | Diagnosis |
|---|---|---|---|---|
| VRAX 07-09 11:36–14:05Z | +$19k big-acct trade (entry 6.55→8); +$24.4k day | **−$1.06** (1 rt) | 35@8.07→8.04 | Pumasok LATE sa 8-break, na-wick out, tapos **200+ re-entry blocks** (chase-guard 109 + G4 escalation 103) habang tumakbo ang 8→13.19. Ang kilalang winner-killer pattern. |
| PLSM 07-13 12:06–15:00Z | +$2,363 → −$2,924 = **net −$561** | **−$10.84** (1 rt) | 90@5.23→5.11 | Na-miss ang morning ramp (extension-veto/benched sa buong ramp) PERO **iniwasan ang −$2,924 backside loser ni Ross** — dito NANALO si CHILI by discipline. |
| VEEE 07-13 13:18–16:00Z | +$766 (pullback ~11 → ~12) | **−$2.75** (2 rt) | 61@13.57; 30@12.60 | Si Ross bumili ng 11-pullback; si CHILI **chase sa 13.57 malapit sa top**. 373 chase-blocks + 228 escalation-blocks pagkatapos. |
| NXTC 07-14 10:31–13:44Z | +$1,400 (8.45 break → 9.50+; HOD 12.40) | **+$10.45** (4 rt) | 8.38/8.20/9.22/9.30 | Tamang pangalan, tamang direksyon, kumita — pero churn (4 pasok) at hindi nahawakan ang 8.45→12.40. Session nag-terminal (`live_finished`) bago matapos ang window. |
| UBXG 07-14 13:10–16:10Z | +$741 (halt-resume dip ~8.5–9.5 → 10.40) | **−$64.66** (3 rt, 3 bailouts) | 10.92/10.78/**312@11.05** | Si Ross binili ang RESUME DIP; si CHILI binili ang **post-squeeze chop-top** ×3 sa halt-riddled tape (9 suspected-halts). Pinakamalaking single loss ng replay set. |
| ERNA 07-15 11:03–14:15Z | +$874 (dip 9.37–9.60 para sa 10-break) | **−$5.72** (1 rt) | 17@10.56→10.23 | Si Ross bumili ng dip BAGO ang break; si CHILI bumili ng **10.56 extension top**, VWAP-flatten sa 10.23. |
| VIVS 07-15 11:23–14:35Z | ~+$2,779 net, 2 trades (1.91→3.64 spike) | **$0.00 — ZERO ENTRIES** | wala | 4,332 steps na `trigger_wait`; hindi kailanman naging entry candidate. Sub-$2 na presyo ($1.91) — pinaghihinalaang price/risk floor (`live_blocked_by_risk` ×4). **Ang pinakamalaking Ross winner ng 07-15 ay hindi man lang nasalang.** |

**TOTAL: CHILI −$74.58 sa 7 windows vs Ross +$6.0k (challenge, 3 araw) + $24.4k (big-acct VRAX day).**

## ①②③ verdict (ano ang gumagana, ano ang hindi)

**GUMAGANA:**
- **Selection/watch:** 7/7 tamang pangalan ang nasa tape at na-watch. Hindi na selection ang problema.
- **③ Loss discipline (ang exit-repair ng 07-09):** LAHAT ng losses maliit (−$1 hanggang −$65), zero bag-holds, mabilis ang cuts (bailouts/VWAP-flatten gumagana). Walang VTAK-class blowup.
- **② Avoid:** iniwasan ang PLSM backside loser ni Ross (−$2,924) at ang VIVS trade-3 loser.

**HINDI PA GUMAGANA (ang natitirang gap, sa pagkakasunod ng halaga):**
1. **ENTRY SHAPE — dip-buyer vs breakout-chaser.** Sa 4 sa 7 windows (VEEE 13.57, ERNA 10.56, UBXG 11.05, VRAX 8.07-late), si CHILI ay pumasok sa EXTENSION habang si Ross ay bumibili ng PULLBACK/dip bago ang break (9.37→10-break; 11-pullback; resume-dip). Ang anti-chase gates ay tama ang intensyon pero ang net ay: laging late entry na agad na-wi-wick out.
2. **RE-ENTRY LOCKOUT ang winner-killer pa rin.** VRAX: pagkatapos ng −$0.03 scratch, 212 blocks (chase 109 + G4 escalation 103) habang 8→13 ang takbo. VEEE: 601 blocks. Ang 07-09 leader-ignition-bypass ay hindi sapat ang lawak.
3. **Sub-$2 gate?** VIVS ($1.91, +90% spike, catalyst verified) — zero candidacy. Kung price floor nga, may buong klase ng Ross winners na hindi kailanman masasalang (kasama ang VTAK-class sub-$1 +138%).
4. **Halt-resume entries.** UBXG: ang resume-dip ang tamang pasok (Ross), hindi ang chop-top chase (CHILI ×3).
5. **Session terminal states** (NXTC/UBXG `live_finished` mid-window) — pinuputol ang natitirang oportunidad pagkatapos ng churn; suriin kung recycle/loss-cap ang nagpa-terminal at kung tama ang calibration niyon sa replay sizing.

## Susunod (pagkatapos ng paper activation)

- Ang paper lane (activation nakatakda 04:10 ET 07-17) ang unang LIVE test ng parehong code — asahan ang parehong signatures; ang paper data ang magko-confirm.
- Fix design order (walang bagong magic numbers, adaptive): (a) pullback-entry mode sa may-catalyst leaders (ang "first-dip" variant na naka-park sa salvage ay eksaktong ganito), (b) palawakin ang leader-ignition re-entry bypass, (c) i-audit ang price-floor gate vs catalyst+rvol names, (d) halt-resume dip entry (#597 lineage).
- Araw-araw na loop mula 07-17: buong tape + live paper + Ross recap extraction (parehong pipeline na ginamit dito).
