# CC_REPORT: trade-throughput & Massive ROI diagnosis

**Date:** 2026-06-04
**Trigger:** Operator direct ask — "monitor/patch/upgrade trading; it barely
trades; make it worth the $249/mo Massive subscription." Supersedes the queued
Phase 5I soak (passive watcher continues).
**Mandate (operator-set this session):** optimize for **profitable equity flow**
(not raw count); **no live-eligibility / gate changes without a further
go-ahead.** Everything below is read-only diagnosis + one read-only observability
script. No live trading behavior was changed.

---

> ## ⚠️ CORRECTION (2026-06-04, same-day follow-up)
>
> **Section 3 ("crypto bleed → eligibility leak") and Recommendation A
> (#1–#2, "crypto eligibility symmetry") are SUPERSEDED — do not act on them.**
> Deeper investigation disproved the leak:
> - The lifecycle eligibility gate is **sound** — it actively blocks
>   non-eligible patterns on BOTH crypto and equity (`pattern_lifecycle_not_eligible`
>   fired 706×challenged + 612×candidate on crypto in 7d; 0 non-eligible
>   placements in the last 24h).
> - The "by current stage" counterfactual was **confounded by reactive
>   demotion**: the challenged/retired patterns in §3 traded *while promoted*
>   and were demoted *after* losing. Crypto is net **+$466/30d on currently-
>   promoted patterns** — a normal drawdown week, not a structural leak.
> - **No eligibility change was made or should be made.**
>
> The validated root cause of thin equity flow is **certified-pattern supply**,
> addressed by **PR #308 (equity-native pattern miner)** — mining equity
> candidates from realized winners. See that PR + the activation runbook below.
> The other live levers (momentum-floor mismatch, signature bucketing) are
> scoped in the "Follow-up" section appended at the end of this report.

---

## TL;DR

1. **Massive is healthy and signal-rich — it is not the bottleneck.** Feed is
   live; it produced **15,561 equity alerts in 7d** (2,071 from trade-eligible
   patterns). The constraint is entirely downstream conversion.
2. **The honest ROI verdict: on the equity book alone, Massive does not pay for
   itself right now.** 30d equity gross PnL **+$70.74** vs prorated Massive cost
   **−$245.42** → **net −$174.68**. (Equity-only is a *lower bound* — the feed
   also powers regime detection + the scanner that seeds crypto.)
3. **"Just trade more" is the wrong fix and would lose money.** Realized edge is
   thin and the cost-aware gates are mostly doing real capital protection. The
   crypto book — which carries the volume — is **−$73.84 over 7d (22% WR)**.
4. **Two genuine, fixable problems (flagged, not flipped per mandate):**
   - A **crypto eligibility asymmetry**: the crypto path trades `challenged`
     (and, until reactive demotion catches them, `retired`/`shadow`) patterns
     live, while equity requires `promoted`/`pilot`. This is the bleed source.
   - The graduation pipeline, if driven by CPCV/composite, is a **trap**:
     8 of 10 top-CPCV shadow patterns are realized *losers*.
5. **Shipped (safe):** `scripts/analyze_massive_equity_value.py` — a durable
   Massive-ROI + funnel + crypto-leak + graduation monitor. Run it anytime.

---

## 1. Why equity barely trades (the funnel)

Equity = Robinhood (Massive-driven). Crypto = Coinbase/RH-crypto (Coinbase OHLCV
+ CoinGecko; does **not** consume Massive).

| Stage (equity, 7d) | Count |
|---|---|
| Alerts (supply) | 15,561 |
| …from trade-eligible patterns | 2,071 |
| Autotrader placed/scaled | **30** |
| Realized closes (14d) | ~11 |

Top rejection reasons on **trade-eligible** patterns (7d) and the verdict from a
read of the gate code + the prior 2026-05-29 AlgoTraderArchitect review:

| Blocker | 7d | Verdict |
|---|---|---|
| `selector:shadow_observation_signal_lane` | 1,186 | **By design.** `signal_lane='research_shadow'` machine-only exploration alerts; observation-only even on promoted patterns. |
| `non_positive_expected_edge` | 201 | **Legitimate.** Managed-exit overlay shows rejected setups are genuinely ~breakeven/negative after costs (stops not tighter than base, R:R below floor). |
| `pdt_guard:pdt_limit_reached:3>=3` | 182 | **Regulatory.** PDT 3-day-trade cap under $25k. Cannot bypass. |
| `stock_momentum_context_below_floor` | 96 | **Defensible.** Queue-pressure-conditional; demands gap% + rel-vol when many candidates compete. |
| `execution_stop_loss_too_wide` / `missed_entry_slippage` | 57 / 23 | Mostly protective; `favorable_pullback` slippage (10/7d, +EV) is the one evidence-backed tuning lever, pending replay. |

**Root structural cause:** only **5 patterns are trade-eligible** (3 promoted +
2 pilot), while 9 strong-looking patterns sit frozen in `shadow_promoted`. Supply
of *eligible* signal — not Massive data — is the binding constraint. The legacy
`$200` symbol price cap is **not** a factor (the funnel's dedicated check returns
zero positive-edge blocks).

---

## 2. Realized PnL reality (governs everything)

| Book | 30d | 7d |
|---|---|---|
| **Equity** | +$70.51, 27 trades, avg **+$2.61**, 37% WR | +$0.23, 4 trades, 75% WR |
| **Crypto** | +$308.31, 228 trades, avg +$1.35, 32.5% WR | **−$73.84, 72 trades, 22% WR** |

Equity has *better per-trade economics* but is starved. Crypto carries volume but
is **bleeding in the current `risk_off` tape**. Pattern 585 ("marquee alpha") is
**0/6 −$29.87 on recent equity** and net-negative on 7d crypto.

---

## 3. Crypto bleed — root cause (−$72.49 live, 61 trades, 7d)

Confirmed **live** money (real broker order IDs, `auto_trader_v1`). 70% of the
loss is two patterns:

- **585** (−$27.58): over-trades specific alts (re-entered ADA-USD 4× in one day,
  losing each time). No per-ticker realized-loss cooldown.
- **1267** (−$24.49, **0/18**): a `shadow_promoted` pattern reaching the live
  crypto path via the fast-path maker-only route.

**The structural finding:** crypto entry eligibility is *looser than equity*.
Last-36h live crypto placements were 15 `promoted` **+ 6 `challenged`**; earlier
in the week `retired`/`shadow_promoted` patterns also traded live and were only
demoted **after** bleeding (reactive, not preventive). The crypto regime gate
blocks only ~1.1% (538/47,322) — too permissive to stop dip-buy/mean-reversion
patterns firing into a downtrend.

---

## 4. Shadow → live graduation: realized-first, and mostly a trap

Ranking the `shadow_promoted` cohort by **realized** paper+live PnL (30d) instead
of CPCV completely reorders it:

| pid | name | gate | CPCV | payoff | live (n/$) | paper (n/$) | verdict |
|---|---|---|---|---|---|---|---|
| **1074** | Quad oversold bounce | ✗ | 7.89 | 2.87 | 4 / −1.66 | 260 / **+229.12** | strong paper, **gate not passed** |
| **1252** | Lower BB + MACD turn | ✓ | 5.16 | 4.21 | 1 / −0.25 | 15 / **+55.07** | **cleanest pilot candidate** |
| 1295 | Above upper BB | ✓ | 6.05 | — | 0 / 0 | 0 / 0 | untested, no evidence |
| 1267 | Extended pullback | ✓ | **9.65** | — | 18 / **−24.49 (0 win)** | 0 / 0 | **CPCV trap** |
| 1250 | Vol expansion + oversold | ✓ | 3.02 | 2.33 | 4 / −0.80 | 178 / −58.21 | paper loser |
| 1245 | Lower BB + MACD turn | ✓ | 5.76 | 2.27 | 5 / −6.70 | 318 / −69.72 | paper loser |
| 1247 | RSI overbought | ✓ | 4.60 | 1.18 | 3 / −2.48 | 82 / −84.94 | paper loser |
| 1248 | RSI near-oversold | ✓ | 4.12 | 0.87 | 7 / −0.06 | 500 / **−1,130.86** | severe paper loser |

**8 of 10 high-CPCV "candidates" are realized losers.** This is the documented
"CPCV/composite inversely correlated with realized PnL" landmine, live. Any
cohort-promote driven by CPCV/composite would promote losers and dilute the real
alpha — exactly the operator's instinct.

**Proposed pilot (for operator approval — NOT executed):**
- **1252** only, at pilot sizing: gate-passed, positive paper (+$55 / 15), payoff
  4.21, CPCV 5.16. The single evidence-clean graduation.
- **1074** as a watch-item: huge paper edge (+$229 / 260) but `gate=False`;
  re-run its certification (CPCV/DSR/PBO) first, then reconsider. Do not promote
  on paper PnL alone.
- Everything else: **keep in shadow or demote.** Do not graduate on CPCV.

---

## 5. Bugs / infra

- `_block_live_spot_short_unsupported(llm_snapshot=…)` TypeError (×5, last
  2026-06-03 19:12) and `_safe_float` NameError (×3, 2026-05-29): **already fixed
  in deployed HEAD** — both predate the ~1h-ago container restart; current code
  passes `llm_snap=llm_snap` and defines `_safe_float`. No action.
- `statement timeout` → `autotrader_desk … failed closed
  reason=desk_runtime_unavailable timeout_ms=1500`: the **HDD I/O contention**
  (operator-known; disk upgrade inbound) intermittently times out the 1500ms desk
  gate, which **fails closed and blocks trading** during stalls. Not fixing the
  I/O root cause; noting that it is a real (transient) throughput suppressor.
- **Deployment note:** the working tree carries a large uncommitted diff (218
  files / ~78k lines, parallel agent). Deploying it is an operator/Cowork
  decision, out of scope here.

---

## 6. Recommendations (prioritized; all require operator go-ahead to enact)

**A. Stop the crypto bleed (PnL-protective, highest $ impact):**
   1. Apply lifecycle eligibility to crypto entries symmetric with equity
      (require ≥ a minimum stage; stop trading `challenged`/`retired` live).
   2. Add a **proactive per-pattern realized-loss circuit breaker** for crypto so
      bleeders (1267-style 0/18) halt before the slow reactive demote — threshold
      derived from each pattern's own realized distribution, not a fixed number.
   3. Per-ticker re-entry cooldown scaled by recent realized loss streak (stops
      585's ADA-USD re-entry compounding).

**B. Grow profitable equity supply (the path to "worth it"):**
   4. Graduate **1252** only, pilot sizing (§4). Re-certify 1074 before considering.
   5. Replay-validate the `favorable_pullback` slippage allowance (10/7d, +EV).

**C. Observability (shipped):**
   6. `scripts/analyze_massive_equity_value.py` — schedule daily; it is now the
      single answer to "is Massive worth it / why no trade / what's bleeding."

**Do NOT:** weaken the edge/PDT/regime/momentum gates, reset breakers, or enable
CPCV/composite-driven cohort promotion.

---

## Verification
- All figures from read-only queries against live `chili` (2026-06-04) and
  `scripts/analyze_massive_equity_value.py` (run clean).
- No flags flipped, no patterns promoted, no gates changed, no breakers touched.

## Open questions for operator / Cowork
- Approve the 1252-only pilot? (Recommended.)
- ~~Approve a crypto eligibility-symmetry + per-pattern realized-loss breaker
  brief?~~ **WITHDRAWN — see the correction banner; the gate is sound, no leak.**
- Deploy disposition for the 218-file uncommitted working tree?

---

## Follow-up (appended 2026-06-04 same-day — supersedes the crypto-eligibility line)

After the correction above, the operator chose **"generate new equity alpha"**
as the durable fix for thin certified-pattern supply. Status of that work:

### Shipped — PR #308: equity-native pattern miner (dormant)
Mirrors the crypto miner but mines equity candidates FROM realized equity
winners (live + paper-shadow, indicators sourced from the linked breakout
alert). Adaptive exit seed (max_bars from observed holds; atr/target inherited
from parent). Flag-gated `brain_equity_miner_enabled=False`. 4 unit tests pass.
Read-only validation on live data: 209 winners → 168 signatures → ~5 spawnable
candidates.

### Activation runbook (a deploy — operator-run)
The running `chili-clean-recovery-*` stack is a custom baked-image deploy
(`chili-app:main-clean-7550b2f`), main-derived (no os-deploy divergence), and
the brain-worker runs **no migrations**. To activate:
1. Build a `chili-app` image from **current main** (now includes #308) in a
   clean main checkout.
2. Recreate **only** the brain-worker with that image + env
   `BRAIN_EQUITY_MINER_ENABLED=true`. Autotrader/broker stay on the old image →
   trading uninterrupted; reversible by recreating from the prior image.
3. Verify: `docker logs <brain> | grep equity_pattern_miner` → expect
   `spawned equity candidate id=… origin='equity_miner_auto'`. New candidates
   flow through the normal certification ladder before any live capital.

### Remaining live levers (need operator go-ahead — both are live-behavior)
- **Miner yield (#1):** the alert-join already mitigates trade-row snapshot
  sparsity; the binding limiter is *signature diversity* (only 6/168 signatures
  repeat ≥3×) driven by low equity trade volume + fine bucketing. Safe tuning:
  coarser equity signature bucketing and/or `min_winners=2` for equity (with
  overfit guards) — adjustable inside the miner, still cert-gated.
- **Momentum-floor mismatch (#3):** when the candidate queue saturates
  (pressure=1.0), `stock_momentum_context_below_floor` requires **5% gap + 2×
  relative volume** — a momentum-*surge* profile that structurally rejects the
  *mean-reversion* setups (oversold bounce, IBS, BB reversion) that ARE the
  equity book's style (today's rejects: ~0.8% gap, 0.16× vol). Adaptive fix:
  when saturated, rank candidates by **pattern expected/realized edge**, not a
  fixed gap/vol momentum proxy. No magic numbers; style-agnostic.
