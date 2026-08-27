# Retest-Tolerant Stop (bailout dwell-confirm) — 2026-08-27

**Katayuan: NAKA-IMPLEMENT, IPINADALANG OFF, may nakasulat na flip criterion.**
Utos ng adversarial audit: i-flip lang bilang pakete kasama ang conditional
admission gate.

## Ang sukat na pinagmulan (1,206 labelled ignition, 2026-08-25 + 08-26)

- **95% ng CONTINUED na panalo ay nag-trade sa/mababa sa entry sa loob ng 60s**
  bago tumakbo. Ang "bumalik sa ilalim ng entry" ay microstructure jitter, hindi
  kamatayan.
- Ang entry-price fast-bailout (ang gawi ngayon) ay ang **pinakamasamang cell sa
  bawat table**: pinapatalsik ang 86–96% ng panalo; ito lang ang uniporme-
  negatibong panuntunan (E[pnl] −0.14 hanggang −0.19%/trade).
- Pinakamahusay na kombinasyon: **60s TULOY-TULOY na dwell sa ilalim ng entry AT
  lalim ≥1%** → panalo natatalsik 16.3%, pagkabigo natatalsik 63.0%.
- **2% hard backstop** para sa rip-then-collapse (n=77, mean −2.42% sa ilalim ng
  panuntunan); nasa LOOB ng sized stop (median 3.0%, p75 4.8%) kaya hindi
  ginagalaw ang sizing/R.
- Depth (AUC 0.60 @60s) at reclaim-time (AUC 0.65) ay PAREHONG mahina bilang
  separator — walang stop geometry na gumagawa ng edge; **ang seleksyon ang
  pinagmumulan ng edge, ang trabaho ng stop ay huwag itong sirain.**

## Ang panuntunan

`_bailout_dwell_confirm_holds` (live_runner.py) — kinokopya ang stop-side
flicker-confirm pattern (pending stamp / flicker-dodge / redispatch):

1. Ang trigger A (`breakout_failed_to_hold`) o B (`lost_vwap_flatten`) ay
   **nag-a-arm** ng `bailout_breach_pending_utc`, hindi na agad lumalabas.
2. `bid ≥ entry` → linisin ang stamp (`bailout_breach_flicker_dodged`) — ang
   dwell ay tuloy-tuloy.
3. Labas (reason `"bailout"`, hindi nagbabago — buo ang #1199 semantics) kapag
   dwell ≥ `chili_momentum_bailout_dwell_confirm_seconds` (60) AT
   `bid ≤ entry×(1−0.01)`.
4. Backstop: `bid ≤ entry×(1−0.02)` → labas agad.
5. Anumang nawawala/sirang input → gawi ngayon (ang exit ay hindi naha-harang).

## Ang XPON 08-26 walkthrough (audit-verified)

Bailout #1 nasagip, #2 nasagip (hindi umabot sa −1%), #3 wash + walang terminal
strike — ang session sana ay buhay pa sa 13:46 ignition.

## ⚠️ Mga kondisyon ng flip (audit MINIMAL CHANGE LIST)

1. **Pakete lang kasama ang conditional admission gate.** Sa unconditioned corpus
   ang panuntunan ay EV +0.02%/trade gross, ≤0 pagkatapos ng frictions. Flip
   kapag ang **admitted-set winner rate sa nightly replay ≥ ~25%.**
2. Ang 4 na dark bailout sibling ay dapat manatiling OFF (may guard test).
3. Sukatin muna ang viability-floor bailout (live_runner.py:38447) na firing rate
   sa winner retests — unwrapped na landas na nakakapag-bail pa rin.
4. Soak instrumentation: exit-fill slip lampas sa −2% backstop (p95 −3.0 hanggang
   −3.3%, worst −8.5%) at per-symbol-day pnl delta — **hindi katanggap-tanggap
   ang pooled means bilang acceptance** (DAIC-0825-chop day = −55% cumulative).
5. `_utcnow()` lamang (replay_v3 parity). ✅ ginawa.

## Mga artifact

Corpus: `scratchpad/ignition_corpus_20260826.csv`, `corpus2_0825.csv`,
per-second sa `scratchpad/ign/`; scripts `retest_shape.py`, `resim.py`.
