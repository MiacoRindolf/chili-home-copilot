# Ang Graduation Ladder — mula napatunayang mekanismo patungo sa Ross-size na kita
**Petsa:** 2026-08-22 · **May-akda:** Claude (weekend breakthrough program)

## Bakit ito ang pinakamalaking dolyar
Ang lahat ng sukat ng linggo ay iisa ang sinasabi: **ang mekanismo ay Ross-grade
na; ang pagitan sa dolyar ay LAKI NA LANG ng taya.** Sa parehong mga tape:
SGLY +$643 at HUIZ +$1,588 sa $1k risk (kapantay per-share si Ross na kumita
ng $6.5k/$12.8k sa ~10× na sizing); sa araw na dinugo si Ross ng −$18k (YJ),
ang makina ay −$46 hanggang −$695 lang (risk-capped). Ang premarket band ay
PF 20.1. Ang scaling ay linear hanggang sa liquidity participation cap — sa
mga sinukat na tape, kinaya ng book ang 20× na laki nang buo ang fills.

## Ang limang kandado (kasalukuyang live config)
1. `chili_momentum_risk_loss_fraction_of_equity` = 0.01 (≈$130/trade sa $13k)
2. `chili_momentum_risk_max_loss_per_trade_usd` = 50 (fallback fixed cap)
3. `chili_momentum_risk_notional_fraction_of_equity` = 0.15
4. `chili_momentum_max_aggregate_risk_pct_of_equity` = 0.03
5. `chili_momentum_risk_liquidity_participation_fraction` = 0.01 (HINDI
   ginagalaw kailanman — ito ang pisikal na katotohanan ng exit)

## Ang ladder (ebidensya ang susi ng bawat baitang, hindi kalendaryo)

| Baitang | Risk/trade | Mga kandado (1/2/3/4) | GATE para umakyat |
|---|---|---|---|
| **G0 (ngayon)** | ~$130 (1%) | 0.01 / 50 / 0.15 / 0.03 | — |
| **G1** | ~$250 (2%) | 0.02 / 250 / 0.25 / 0.06 | **5 magkakasunod na trading days** na: (a) zero defect-class incidents (walang orphan/unadopted/naked position), (b) premarket PF ≥ 1.5 sa ≥ 8 entered trades, (c) lahat ng talo ay ≤ 1.2R (walang blow-through lampas sa slippage allowance) |
| **G2** | ~$500 (4%) | 0.04 / 500 / 0.40 / 0.10 | G1 gate ulit sa G1 size + **kabuuang realized ≥ +$1,000** sa G1 period + max drawdown ≤ 2× ang pinakamalaking single-trade risk |
| **G3 (Ross-entry)** | ~$1,000 (7.7%) | 0.077 / 1000 / 1.0 / 0.25 | G2 gate ulit sa G2 size + **isang $1k-risk paper rehearsal week** na PF ≥ 1.5 + operator sign-off |
| **G4+** | equity-scaled | ang fraction ay hindi na tumataas — ang EQUITY na ang lumalaki (compounding) | Ang liquidity cap na ang natural na ceiling bawat pangalan |

## Mga hindi nababagong tuntunin
- **Isang baitang bawat pag-akyat; dalawang baitang pababa sa anumang
  defect-class incident** (hindi sa ordinaryong talo — sa INCIDENT: orphan,
  unadopted fill, naked position, breaker miss). Ang ordinaryong red day sa
  loob ng risk caps ay HINDI dahilan ng demotion.
- Ang drawdown breaker at daily-loss caps ay nag-i-scale KASABAY (parehong
  fraction basis) — hindi hiwalay na dine-desisyunan.
- Ang bawat pag-akyat ay ISANG commit na nagpapalit ng defaults + CC report
  na nagsisita ng gate evidence (per-sha attribution, ang
  feedback_sizing_expectancy_code_drift na aral).
- Ang liquidity participation (kandado #5) ay hindi ginagalaw kailanman.

## Ang unang hakbang
Ang Lunes–Biyernes (08-24–08-28) ang unang G0→G1 evidence window: ang buong
bagong makina (stand-in, wake, fill-watch, 1R partial+breakeven, post-open
discipline, FTD/reverse-split signals) sa unang malinis na linggo nito. Ang
premarket probe + ang MFE events ang awtomatikong magsusukat ng gate criteria.

*(Nakaugnay: project-replay-ross-size-scaling-0821, project-exit-capture-1r-arm-0822,
project-premarket-first-trades-0821 sa memory; ang mga replay knob para sa
rehearsal ay nasa unang memo.)*
