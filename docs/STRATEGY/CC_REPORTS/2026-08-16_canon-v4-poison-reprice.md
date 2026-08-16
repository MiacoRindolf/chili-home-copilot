# 2026-08-16 — Canon v4: ang massive_snapshot quote-poison reprice

## TL;DR

**Canon net: −1,416.20 → −492.26 (+924).** Ang `source='massive_snapshot'` NBBO
rows (once-a-minute HTTP poller) sa golden tape ay maling time-alignment —
**6.8% mean / 31.4% max divergence** vs iqfeed_l1 sa parehong minuto — at 20/22
canon windows ang may lason (400–950 rows bawat isa). Ang fix (PR #1029 → main
`097a870`): read-side exclusion sa replay (grid + sink mirror); hindi ginagalaw
ang archive rows. Ang buong 22-window re-canon ay tumakbo sa build `bca8785`.

## Ang ebidensya ng lason (VTAK|07-08, ang pinakamalaking per-cycle loss ng v3)

Sa 13:45Z: massive row bid/ask **1.53/1.54** habang ang tunay na market
(iqfeed_l1 + 100%-tick-embedded quotes + mismong prints) ay **1.20/1.21**.
Kinain ng 1.5s grid ang lason → fill model binili ang pekeng ask → **−425.53
(−3.3R) sa isang segundo** — imposible sa live. Kumpara: `massive_ws`
(websocket) ay 0.74% mean divergence — malinis, pinanatili.

## Canon v4 (21 ok + UPC excluded/timeout, fill-derived PnL)

| Window | v3 | v4 | Δ | Kuwento |
|---|---|---|---|---|
| LHSW\|07-06 | −252.29 | **+4.28** | +256.57 | "sakuna" → GREEN; 17e→4e |
| VTAK\|07-08 | −748.39 | **−105.09** | +643.30 | artifact tanggal; worst cycle −0.95R |
| CWD\|07-02 | −309.20 | −197.55 | +111.65 | 18e→8e |
| CLRO\|07-07 | −18.34 | +6.03 | +24.37 | 15e→4e |
| LHAI\|07-01 | −11.69 | −3.40 | +8.29 | |
| LUCY\|07-06 | −13.19 | −10.17 | +3.02 | |
| TVRD\|07-07 | −68.43 | **−98.88** | −30.45 | tinatago pala ng lason ang churn |
| JLHL\|07-09 | +19.20 | +1.28 | −17.92 | pinapalobo ng lason |
| CELZ\|06-30 | +99.56 | +31.69 | −67.87 | pinapalobo ng lason |
| GMM / JZXN / JEM | maliliit | maliliit | −2.11/−1.70/−3.21 | |
| **9 windows** | — | — | **±0.00 EKSAKTO** | TC, CLRO0702, TNMG, NVVE, VRAX, SUNE, PLSM, QTTB, HYFM — parity proof |

## Ano ang ibig sabihin

1. **Halos sarado na ang sakuna-class** (dating #1 ROI item): L13 symbol-day
   lockout (be74ee9) + poison filter. Walang natitirang single-cycle blow-through
   sa buong canon — lahat ng talo ay nasa loob na ng ~1R na pangako ng risk system.
2. **Ang bagong #1 na TUNAY na target: L2a churn** — TVRD (31 entries, −98.88)
   ang flagship case, kasama ang CWD residual (−197.55). Ngayon ay malinis na
   ang panukat para dito.
3. **Winner-press** (CELZ +31.69 na may 6e/7x) ang #2.
4. Ang 9 eksaktong-parity windows ay nagpapatunay na ang filter ay disiplinado:
   walang epekto kung saan walang lason.

## Operational notes

- Publish: canonical root `D:\CHILI-Docker\chili-data\replay_batch` (v3
  archived sa `archive/2026-08-09_v3_fbfa7f2/`) + kopya sa
  `E:\dev\replay-runs\canon_v4`.
- ⚠️ D: drive (Seagate HDD): sira ang git bulk-writes mula 08-14 ~13:00 PT
  (hangs + tahimik na partial writes); lahat ng code work ay nasa
  `E:\dev\chili-work-clone` na; backups sa `E:\backup`. Kailangan ng disk check.
- Deriver: BASELINE_TARGET 22→48 (bahagi ng #1029) para manatiling runnable ang
  canon cohort sa tabi ng mga bagong pins.
- CELZ/CLRO0707 receipt timeouts sa ilalim ng load — malinis sa tahimik na DB
  retry; UPC timeout pa rin @3h (konsistente sa v3 exclusion).

## Para kay Codex (relay ng operator)

1. Reseal mula main `097a870` (kasama #1026/#1027/#1029).
2. **Capture-side hiling:** i-tag/ihiwalay ang delayed-poll quote rows
   (`massive_snapshot`) sa bagong pins — o itigil na ang pagsulat ng mga ito sa
   parehong stream ng event quotes; ang mga bago nating golden pins (08-06+) ay
   may parehong kontaminasyon.
3. Live-side audit ng massive_snapshot sa momentum_nbbo_spread_tape ay nakalista
   bago ang Window 2 (Lunes).
