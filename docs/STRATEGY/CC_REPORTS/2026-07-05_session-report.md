# CHILI momentum — overnight session report (2026-07-05)

## Headline
Dense-tape week A/B (SVRE/JEM/CELZ/TC/DXF/CLRO), the same 6-mover week throughout:

| Stage | TOTAL | Δ |
|---|---|---|
| Baseline (pre-session, deployed) | **+$148.62** | — |
| + PR #849 (MFE-target + conviction + anti-chase) | +$189.58 | +$40.96 |
| + PR #850 (conviction × entry-quality gate) | **+$278.24** | +$88.66 |

**Net: +$148.62 → +$278.24 (+87%).** All 5 changes default-ON, each kill-switchable, each week-validated, each adversarial-reviewed before merge. ⚠️ FSM is optimistic on the winner side (own-order impact not modeled) — the *direction* is solid; treat the winner magnitudes as an upper band.

## Per-mover (baseline → final)
| Mover | Baseline | Final | What changed |
|---|---|---|---|
| JEM | +197.14 | **+314.53** | MFE-target let the winner run + conviction sized the clean breakout up |
| CELZ | +48.17 | +48.17 | unchanged |
| SVRE | −5.66 | **−0.33** | anti-chase blocked the top-chase re-entries |
| TC | −13.38 | −13.38 | unchanged (not yet investigated) |
| DXF | −42.07 | **−49.38** | conviction amplified then entry-quality gate recovered most of it |
| CLRO | −35.58 | **−21.37** | same |

## What shipped
**PR #849 (`b3ab9eb`)**
1. **MFE-target LIVE** — first scale-out rr = the setup family's realized-MFE percentile (Sweeney/López de Prado), shrunk toward the existing adaptive rr as the small-sample prior. Kills the fixed rr_cap=6. → JEM +$197→+$314.
2. **Conviction A-setup sizing** — magic 0.5 floor → `clamp((via−A_floor)/(1−A_floor))`; a top A-setup sizes toward the 15%-equity cap.
3. **Anti-chase re-entry gate** — after a losing exit, block re-buying >1.5 **ATR** above the prior losing tranche's HWM (ATR unit, not the pathologically-wide prior stop). → SVRE −$7.02→−$0.33. Consolidated 2 overlapping chase mechanisms into 1.
4. **was_loss = whole-trade net** (correctness) — a scaled winner whose runner trails out below avg is no longer mis-tagged a loss. **Caught by the adversarial review before ship.**

**PR #850 (`8a5c5a6`)** — **conviction × entry-quality gate**. Root-caused the DXF/CLRO amplification: conviction sized up by **viability = the NAME** (DXF & JEM both 0.90) and overrode the front-side **entry-strength** tilt that had correctly shrunk the weak top-buys. Fix: multiply the conviction floor by `frontside_mult` → **edge = name × entry**. → DXF +$54.91, CLRO +$33.75; JEM/CELZ/TC/SVRE byte-identical.

## Your question answered: would Ross take DXF/CLRO?
**Yes — both.** Not a selection problem. DXF ($0.74→$1.10, +51%) = an explosive sub-$1 runner CHILI intentionally allows ("Ross trades sub-$1 runners"). CLRO ($4.73→$7.27) = squarely in range. The difference is **entry timing**:
- **DXF**: CHILI bought **1.10 = the literal HOD** + 1.00–1.06 on the fade → −$118.
- **CLRO**: CHILI bought **6.74 twice** near the 6.90 top, right before the drop to 6.15.
- **JEM (winner)**: bought the **breakout near the base** (3.47 @ VWAP 3.41) and rode the vertical UP, sold into 5.24.

CHILI **buys local tops into fades** (same shape as SVRE); conviction then amplified those. The entry-quality gate stops the amplification. The deeper, un-shipped lever is **entry timing** (buy the pullback, not the top).

## Open / next levers (NOT shipped — teed up for your review)
1. **DXF/CLRO/TC still net-losers at base size** — the entry-quality gate stops *amplifying* the top-buys but CHILI still *takes* them. Real fix = entry timing (buy pullback, not top). Big/risky lever.
2. **`vwap_dist_sigma` reads None in short replay windows** — the extension term in the front-side strength wasn't computed in a 30-min frame, so the gate is measured *conservatively*; need to confirm it computes LIVE on thin-frame names (else the gate is weaker live than shown — still net-positive, but investigate).
3. **TC −$13.38** unexamined.

## ⭐ FRONTIER FINDING (the strategic conclusion — the operator's 3-criteria scorecard)
North-star scorecard on the FSM replay: ① capture ALL Ross winners ② avoid ALL Ross losers ③ eliminate CHILI's own week losers. **Locked baseline (5 shipped changes): NET +$264.25** (10 tape-testable movers).
- **② MET** — CHILI declines the exact extension shape Fable 5 flagged (ZDAI −$25k, JEM07 both 0-entry).
- **① and ③ collapse to ONE hard problem**: the entry trigger is **confirmation-lagged**, so on a fast vertical it fires **at the top** (SVRE 7.54 vs Ross 6.98; DXF 4× at 1.05–1.10). CHILI catches the CLEAN vertical (JEM +$314) but tops-out the messy ones — and **no gate can tell will-run (JEM) from will-fade (DXF) at entry.**

**Proven with FOUR negative results** (the "challenge the diagnosis" discipline working):
1. Sticky-backside-bench OFF → JEM +314 → **−3** (bench is protective, not over-conservative)
2. Fresh-base un-bench → SVRE −0.33 → **−12**, CELZ +48 → **−20** (reverted clean)
3. Frontside gate can't shrink DXF (mid-vertical reads strong)
4. Forcing SVRE's early entry LOSES (CHILI can't HOLD the vertical)

**⇒ Gate-tuning is at its ceiling (~+$264).** Ross's remaining edge is execution/discipline (holding the volatile vertical) — genuinely hard to gate-replicate.

## ⭐ THE #1 LEVER (needs your steer): unblock the meta-label edge model
The principled way past the ceiling is the **meta-label model** (learn will-run vs will-fade from entry features → size/gate by p). It's blocked because its feature snapshot is **empty in prod**: `momentum_automation_outcomes.entry_regime_snapshot_json` = **2/942 recent equity outcomes**. BUT the capture **code WORKS** — `chili_test` (FSM) = **96/96 populated** with the full vector (rr/ofi/price/atr_pct/above_vwap/premarket/liq_mult…). So it's an **environmental/plumbing gap in prod, LOW-RISK to fix** (additive, no trading change). Needs your prod context: which runner/mode the prod paper-equity lane uses, whether the 15m OHLCV fetch fails at entry, whether `regime` is populated there. Fix → accumulate samples → train the edge model → **that** solves ① and ③.

## Method notes
- All A/B at dense TICK_STRIDE=2 (the sparse-tape artifact law: slope/flow signals degrade at high stride).
- Read-only against live trading throughout; no broker orders placed.
- Two adversarial multi-agent reviews run; the first caught a real regression (was_loss) that would have blocked a scaled-winner's re-entry.
