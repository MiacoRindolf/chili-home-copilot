# AS101 — Algo Scalping Strategy (Warrior Pro grad course) — visual study

Studied 2026-06-24 from BOTH transcripts AND extracted video key-frames (5 videos, ~7h, 360p frames). Method: per-video agents read transcript + viewed frames → grounded notes → CHILI edge synthesis → adversarial verdicts checked against the **deployed** lane (etfrank worktree, not main).

## Core thesis
Trade ONLY where the HFT/maker algo SUPPORTS the move, never against it. Maker-taker algos provide liquidity inside a name's volatility band and **turn OFF when a stock breaks outside its standard-deviation band** (volatility risk dwarfs the fraction-of-a-penny rebate) — that withdrawal is exactly the clean, fast, tradeable retail window. The slow bleed ("death by 1,000 cuts") comes from trading the WRONG stocks (thick-L2 large-caps, tight ranges) into a predatory algo + PFOF slippage.

## Per-video key learnings
1. **Market Structure** — maker/taker rebate (~0.0024 maker / ~0.003 taker), PFOF (Citadel et al. pay because they extract ~$10-20/trade in slippage), latency arbitrage = the news→fair-value window. Avoid stacked-L2 large-caps (SIRI) + tight ranges.
2. **ATS / Dark Pools** — selection rule (load-bearing): trade **Day 1–2, strong catalyst, already up ~+50%, EXTREME RVOL, which requires LOW average daily volume**. Low ADV is what keeps HFT MMs absent. Dark-pool fill-or-kill no-reroute to probe hidden liquidity (matters only at 1k–10k share scale). MM must buy=sell daily → pulls offers / stop-hunts.
3. **Market Makers & HFTs** — parabolic setup RVOL **≥50x**; HFTs ~75% of volume. Entry = buy the **first micro-pullback** of a squeeze. Fake-catalyst (hacked-PR) spike **round-trips fully** to pre-move. Bull-trap via partial-fill L2 stacking. These big moves are rare (once/twice a month in a bear tape).
4. **Order Routing** — aggressive multi-route fan-out (dark-pool 1→2→3 then lit, ~1s each, ~5s budget); **algo flush** = a large market sell vaporizes the book → fills slip badly → sets up the flush-reversal dip-buy. Entry: cross the spread for a fast fill; exit: post on the ASK (maker), slice in quarters.
5. **Algo Scalping Strategy** — selection: up ≥10% (≥100% extreme), **ADV < 10M sh**, price < $20. Strategy 1 = breaking-news spike + micro-pullback (PRs at top/bottom of hour, premarket cleanest). Strategy 2 = algo-flush dip-buy / V-bounce above VWAP/20MA. Round-number magnets. Wait for a big L2 seller to thin (except first ~15s of a spike).

## Edge verdicts (vs the DEPLOYED lane)
**Already shipped (study VALIDATES CHILI) — reject as builds:**
- Premarket 4:00–9:30 window · maker/ask-side scale-out exits · per-symbol two-strikes fatigue · explosive RVOL/gap/float/low-price selection · first-micro-pullback entry · halt-resume dip-buy.

**Genuinely-new candidates (design-more, not rush-ship):**
- **ADV-ceiling selection filter** (grounded, risky): add `adv_max_shares` (~<10M) to the equity universe — the *causal* low-ADV→no-MM→edge mechanism. CHILI floors TODAY's $-vol but doesn't cap AVERAGE daily volume. Needs adaptive (no magic number) + careful not to drop legit float-rotation names.
- **Fake-catalyst guard** (grounded, net=yes): down-weight unverified/hacked-PR/unsolicited-buyout headlines for the halt-resume dip-buy (they round-trip fully). Implement as a soft credibility down-weight, not a hard veto.
- **flush-dip-buy trigger** + **red-volume-exhaustion veto** (verdicts incomplete — re-verify before any build).

**Conclusion:** AS101 mostly CONFIRMS CHILI's algo-scalping foundation is sound. No clean rush-shippable edge; the new ideas need proper design (queued, not force-shipped). Entry-pattern gaps more likely surface in HVM101/SCAL101/TOS101.
