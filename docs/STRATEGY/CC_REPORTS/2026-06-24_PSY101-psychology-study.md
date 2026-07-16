# PSY101 — Trading Psychology: Developing the Trader's Mindset (Ted & Diane) — visual study

Studied 2026-06-24 (13 videos, transcripts + key-frames). Pure psychology (mindset→behavior, IFS parts-work, emotional regulation) — NOT trading mechanics. For an AUTONOMOUS system, the value is a **bias-mode checklist to guard against in code**, not human emotional regulation.

## Key model
Problematic mindsets → problematic behaviors. The failure map (the load-bearing slide):
`self-doubt/overconfident/fearful/reckless/scarcity/all-or-nothing/toxic-positivity` → `revenge trading / overtrading / hesitation / undertrading / rule-breaking / holding&hoping / blowing up the account`.
Discipline-first recovery: Ross's "one trade a day, get green and get out" → systematic GRADUAL scaling (number-of-trades, share-size, per-share target — never jump); rule-break → next-day no-trade reassess; daily "3 things right / 3 to improve" post-mortem.

## CHILI mapping — validates existing controls (the bias-modes are already coded against)
- revenge trading (re-arm a just-stopped name) → **reap-cooldown (#701) already shipped**
- overtrading → **daily_trade_count_budget (just shipped)**
- blowing up → **max-loss circuit (#769) + per-broker daily-loss caps (#727)**
- euphoric size-up after a run → **prior_day_pnl_damper (just shipped, sizes down after outlier WIN or loss)**
- hot-hand sizing bias → sizing is equity+ATR+stop+liquidity only (no streak input)

## Edge verdicts — NO high-value new edge (psychology validates discipline)
3 candidates, all LOW / covered:
1. **Post-win euphoria de-risk throttle** — largely COVERED by prior_day_pnl_damper (already handles outlier-win size-down). An intraday win-streak angle is marginal + a sizing change = regression risk for ~nothing. NOT shipped.
2. **End-of-day rule-adherence post-mortem scorecard** — observability/learning feature, not a trade edge. Backlog (low priority).
3. **Invariant TEST: per-trade size never depends on win/loss streak** — good hygiene (a test, not a behavior change); harmless to add later. Not a trading upgrade.

**Conclusion:** PSY101 CONFIRMS CHILI's risk/discipline architecture is sound (the just-shipped trade-count budget + prior-day damper close the last gaps). Shipping a marginal sizing tweak here would be exactly the overfit-churn the operator's rules warn against. The real remaining course value is SS101 (setups + scaling).
