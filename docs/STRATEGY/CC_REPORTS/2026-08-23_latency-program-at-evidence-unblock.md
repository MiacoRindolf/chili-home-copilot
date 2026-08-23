# 2026-08-23 — Latency program + evidence unblock

**14 PR merged (#1109–#1122). Deploy tree `0d1d3b2`.**

Dalawang bagay ang natapos ngayong weekend: (1) ang dispatch latency ng buong
open→monitor→close chain, at (2) ang pag-unblock ng golden-library scorecard —
ang tanging paraan para patunayan na net-positive ang isang pagbabago nang
hindi naghihintay ng araw-araw na live session.

At isang bagay ang natuklasan na mas mahalaga kaysa sa dalawa: **ang #1105 ay
hindi gumagana, at ang tunay na sanhi ng 9%-capture ay ibang lever.**

---

## 1. Latency: ang 08-17 cutover ay nagtanggal ng tick-speed dispatch

Ang order POST mismo ay 0.078s. Ang bagal ay nasa **pagpansin**. Nang ilipat ang
lane sa batch scheduler mode noong 08-17, ang retired na event loop pala ang
may hawak ng tick bridge: bawat crossing ng stop/target/watch-break ay
naghihintay na ng scheduler cadence.

**Sinukat na baseline (08-21, buong araw):**

| segment | sukat | target |
|---|---|---|
| arm → unang tick | n=400 **p50 36.67s** p90 73.20s | < 5s |
| candidate → submit | n=4 p50 70.53s p90 140.41s | < 10s |
| deadman phase gap | n=802 **p50 11.71s** p90 44.29s | ~0.5–1s |
| `live_deadman_stop_release_blocked` | **803 sa ISANG araw** | ang GAP ang dapat bumagsak |

⚠️ Itinatama nito ang lumang nota na "arm→tick p50 6.1s" — maliit na sample iyon.

**Naipadala:** #1109 session-cross tick bridge + batch-safe stop-confirm timer ·
#1110 bagong-high wake (trail/ladder) + 5s session refresh · #1111 exit-side
continuation + **P0: ang wake paths ay tinatapon ang sealed POST request** ·
#1112 arm-wake coverage (⚠️ `session_id` ay last-writer-wins at walang Alpaca
twin) + bridge loop-drain · #1113 batch-mode IQFeed pg-LISTEN rail para sa
tahimik na tape · #1114 isang outcome-scan para sa 3 day-state gate · #1115
ginawang tunay na independent ang LISTEN rail · #1116 bar-age meter.

**Na-verify:** binasa ang lahat ng 22 `WINDOW_ENV` key ng supervisor at
ineval ang eksaktong branch conditions — **lahat ng pitong rail ay aabutin** sa
deployed config.

---

## 2. Evidence unblock: naka-block ang scorecard mula 08-22

Tatlong depekto, lahat naipatupad ngayon:

- **#1118** — ang `mine_sink` ay bumabagsak sa import-time `Settings()` (walang
  `.env` sa batch host) → `coverage_unavailable` ang **bawat** window kahit
  maayos ang replay; + advisory-lock liveness probe (fail-closed) +
  `ReplaySessionRowVanishedError`
- **#1119** — progress output sa derive (15 minutong katahimikan = phantom hang)
- **#1120/#1121** — **transaction-poison**: ang "fail-open" na DB read sa session
  ng CALLER ay nag-aabort ng buong transaction. Pumatay ito ng buong replay run
  (`queued_live`, zero events, zero paliwanag). ⚠️ May umiiral nang helper
  (`optional_db_read.py`) — tatlong site lang ang hindi gumamit, **dalawa sa
  kanila akin** (#1096, #1102).
- **#1122** — **magkabuhol ang dalawang wake kill switch**: ang pagpatay sa
  stop-confirm ay tahimik ding pumapatay sa exit continuation.

---

## 3. Ang scorecard result

Canon params (`equity 13000 / rf 0.01 / robinhood_agentic_mcp`), 5 maihahambing
na window vs canon `8874b48`:

| Window | Luma | Bago | Size | Norm | Verdict |
|---|---|---|---|---|---|
| JEM 06-30 | +9.26 | +6.97 | 0.745 | +9.36 | PAREHONG DESISYON |
| LHAI 07-01 | −0.43 | −0.33 | 0.764 | −0.43 | PAREHONG DESISYON |
| UPC 06-26 | +10.13 | +7.60 | 0.750 | +10.13 | PAREHONG DESISYON |
| UPC 06-29 | −16.49 | −16.49 | **1.000** | −16.49 | PAREHONG DESISYON |
| WSHP 06-26 | −2.99 | −2.25 | 0.750 | −3.00 | PAREHONG DESISYON |

**Raw delta −3.98 · NORMALIZED delta +0.09.**

⚠️ Ang raw delta ay **dominado ng #1106 fatigue derate** (size ×0.75 sa hapon,
×1.000 sa premarket — kaya eksaktong pareho ang UPC 06-29). Ang normalized ang
sumusukat ng desisyon. Kung walang normalization, sasabihin ng scorecard na
"bumagsak ang lahat ng 25%" at maaaring i-rollback ang maling lever.

**Sa 5 window, walang isa mang desisyon ang nagbago.**

### Pero: kaya bang abutin ng replay ang mga na-ship?

| Verdict | n | Halimbawa |
|---|---|---|
| EXERCISED | 5 | #1106, #1091, #1098, #1089(b), #1093(b) |
| BAHAGYA | 3 | #1093(a), #1089(a), #1103 |
| **HINDI** | 7 | #1105, #1096, #1092, #1102, #1108, #1095, #1109/#1111 |

Ang `PAREHONG DESISYON` ay **hindi** ebidensyang walang epekto ang mga lever —
ebidensya lang na **hindi sila naaabot ng instrumento**. Ang replay ay
`post-selection-fsm` na may `selection_pipeline_executed=false`.

---

## 4. 🔴 Ang pinakamahalaga: #1105 ay no-op, at ang 9%-capture ay ibang lever

```
arm_r = max(0.5, min(arm_frac × rr, cap)),  cap = 1.0
produksyon (equity): arm_frac = 0.5, rr = class_aware_reward_risk() = 2.0
  luma: max(0.5, 1.0)           = 1.0
  bago: max(0.5, min(1.0, 1.0)) = 1.0     ← EKSAKTONG PAREHO
```

Ang premise ng PR ("sa 6R family target, 3R bago mag-arm") ay **mali** — walang
6R na dumarating sa ladder. Bumibindi lang ang cap kapag `rr > 2.0` (crypto).

**Ang tunay na sanhi**: `chili_momentum_exit_ladder_live = False`
(config.py:7247), sadyang OFF — *"the size-moving sell is not yet A/B-proven
net-positive"*. Sa live equity: kinukuwenta ng ladder ang desisyon, ini-emit ang
counterfactual, inilalapat ang stop ratchet — pero **hindi nagpo-post ng partial
sell**. **Hindi kumukuha ng partials ang lane.** Iyon ang 9% median capture at
ang 68.8R na naiwan.

**Susunod na hakbang**: ang lever ay ang pag-promote ng switch na iyon, at ang
hinihingi nito ay A/B proof mula sa golden library — na bukas na ngayon.
⚠️ Isang harang pa: ang replay driver ay nagse-seed lang ng `iqfeed_trade_ticks`
at `momentum_nbbo_spread_tape` — **walang `iqfeed_depth_snapshots`** — kaya
`stale_or_thin` agad ang ladder. Kailangang i-seed ang depth sa harness bago
maging makabuluhan ang A/B.

---

## 5. Iba pang natuklasan (may sukat, hindi pa naaaksyunan)

- **Conversion funnel** (08-17..21): arm 1,291 → fill 2 = **0.155%**.
  ⚠️ 5-araw na regression, hindi permanente — pre-gap (Hun09–Hul13) ay **73.1%**
  pend→submit at 296 fills. Natitirang buhay: `ortex_batch_manifest_reference_mismatch`
  **563/araw** at sumasabog — inaayos ng #1095, hindi pa nasusubok live.
  ⚠️ 433 DOA arms mula sa **19 simbolo lang** (VOGX: 76 arms / 75 `no_bbo` / 0 watched).
- **OHLCV staleness**: ang structural exits at ang 5m trail anchor ay
  nagdedesisyon sa bars na hanggang **3600s** luma — ang cache invalidation ay
  nakakabit LANG sa UI chart WebSocket, wala sa scheduler-worker.
  ⚠️ `cache_age_seconds` ay **bulag** (0.0 kada store) → #1116 bar-age meter muna.
- **Premarket selection cadence**: hindi pa nasusukat kailanman; #1117 duration
  meter. ⚠️ Ang scan list ay alpabetiko, kaya ang deadline ay magda-drop ng
  parehong tail kada cycle — huwag maglagay ng budget bago i-order ayon sa
  relevance.

---

## 6. Susukatin sa Lunes 08-24

Instrumento: `project_ws/AgentOps/latency_report_20260824.py` (naka-bake ang
baseline). Alarma 00:38 PDT; Windows backup task 00:55 PDT.

A) latency (arm→tick, candidate→submit, deadman gap) · B) conversion funnel §5b ·
C) ortex manifest mismatch (563/araw → ~0 kung gumagana ang #1095) · D) bar-age
(unang sukat kailanman) · E) premarket refresh `dur_s` + OVERRUN · F) Gate-6
shadow.

---

## Mga aral na naitala sa memory

- Ang golden batch ay may **canonical parameters** — ang pagpapalit ay gumagawa
  ng **pekeng regression** (halos nag-deklara ako ng P0).
- ⚠️ Huwag galawin ang worktree habang tumatakbo ang batch (build_sha mismatch).
- Ang scorecard ay **dapat i-normalize** para sa sizing habang ON ang derate.
- Ang "fail-open" na DB read ay hindi fail-open kung wala itong SAVEPOINT.
- Maghanap muna ng **umiiral na helper** bago magsulat ng bago — dalawang beses
  akong nag-ship ng bug na may nakahanda nang lunas sa repo.
