# Compounding / Scaling Engine — replicate (and exceed) Ross's $583 → $18.8M curve

**Status:** DESIGN (2026-06-09). Goal: make CHILI grow a small account the way Ross Cameron
did — $583.15 (Jan 2017) → $100k in 44 days → $1M (May 2019) → $18,810,638 (Dec 2025,
CPA-audited) — via continuous compounding, liquidity-aware size scaling, and base-hit
consistency. CHILI should match this AND beat it (systematic, no tilt, multi-name, 24/7).

## Ross's model (researched 2026-06-09)

1. **Compound continuously** — never reset/withdraw early; let the account grow ("never met
   a six-figure trader who started with $500 at the start of each month"). Lesson 5: delay
   gratification until 5-6 figures.
2. **Scale share size WITH the account, capped by LIQUIDITY** — 100 → 1,000 → 10,000 shares
   on the same setups, but "between 100 and 10,000 shares is where you scale without slippage;
   you can't move 500,000 shares in 1-2 min." Size is bounded by what the NAME can absorb.
3. **Base hits, dollar-defined risk** — 10-15¢/share, fixed max-$-loss/trade (exit at it
   regardless of the technical stop), ~$1,800/day avg across 20,000+ trades. Consistency, not
   home runs.

## CHILI today — the AUDIT (what already exists)

✅ **Compounding is ALREADY BUILT** (`risk_policy._account_equity_usd` + `_equity_relative_cap`):
- Sizing basis = **live buying power × margin-multiple**, read per trade per venue (Robinhood
  for equities, Coinbase for crypto). Grows as the account grows → automatic compounding.
- Per-trade RISK = `loss_fraction_of_equity` (1% of equity). Per-trade SIZE = `notional_
  fraction_of_equity` (15%). Daily breaker = `daily_loss_fraction_of_equity` (5%). All
  fractions of LIVE equity → "scales UP as equity grows and DOWN in drawdown (auto-de-risk)."
- Concurrency = `open_risk_fraction / loss_fraction` (basis-INDEPENDENT count — growing equity
  scales per-trade SIZE, not the slot count).
✅ **Giveback halt** (stop after giving back 50% of the day's peak) + **daily-loss cap** (5%).
✅ **Risk-first sizing** (`compute_risk_first_quantity`): qty = max-loss / stop-distance, capped
   at the notional ceiling — a tighter stop buys more size at constant risk (Ross-style).

So Ross's principles #1 (compound) and #3 (dollar-risk + base hits) are **already in CHILI.**
The operator's worry ("consistent pagpapalago/scaling") is structurally handled. The gap is #2.

## The GAPS

### Gap 1 (KEY) — Liquidity-ceiling sizing: size by the NAME's liquidity, not just % of equity

The notional cap is a flat **15% of equity**, name-agnostic. This is FINE at a small account,
but **it breaks exactly as CHILI compounds to a large account** — which is the whole goal:
- 15% of a $1M account = **$150k** notional. On a thin $5 low-float Ross name that is **30,000
  shares** — more than the book can absorb. CHILI could enter (crossing/sweeping) but **cannot
  EXIT cleanly** on a stop-out → the catastrophic thin-book sweep (the very thing the spread
  gate + the 0-fills root cause are about).
- This is precisely Ross's "you can't move 500,000 shares in 1-2 min" — he caps size at the
  name's liquidity. **CHILI must too, or it outgrows the small-cap universe as it compounds.**

**Design:** add a per-name **liquidity cap** and size at the MIN of the two:
```
position_notional ≤ min( equity_relative_notional_cap,
                          liquidity_participation_fraction × name_dollar_volume )
```
- `name_dollar_volume` = the name's daily dollar-volume (already computed: the liquidity-bias
  #552 `snapshot_dollar_volumes` + the NBBO tape). For a sharper bound, use recent per-minute
  dollar-volume × an exit-horizon (a few minutes) — the v2 refinement.
- `liquidity_participation_fraction` = the ONE documented knob (e.g. **1% of daily $-volume** ≈
  a few minutes of an active name's volume = exitable without major impact; institutional
  participation guidelines are 1-10% of ADV, 1% is conservative for fast small-cap exits).
- Adaptive, per-name, from data we ALREADY have — no magic $ cap. At a small account the
  equity cap binds (unchanged behavior); as the account compounds, the LIQUIDITY cap binds on
  thin names → CHILI scales up only as far as each name can absorb. **This is the scaling
  enabler.**

### Gap 2 (optional) — Daily-profit-goal pacing (lock the win)

Ross hits a daily $ goal then reduces/stops (don't give back a good day). CHILI has the
REACTIVE giveback halt; a PROACTIVE daily-goal would lock gains harder:
- After realized daily PnL ≥ `daily_profit_goal_fraction × equity`, reduce per-trade size
  (e.g. ×0.5) or stop arming new sessions for the day.
- ⚠️ Tradeoff: CHILI is systematic + tireless — a hard daily stop leaves money on the table on
  a strong trend day. Recommend SOFT (size-down, not stop) and configurable, default off; the
  giveback halt already covers the downside. Lower priority than Gap 1.

## Why CHILI can EXCEED Ross

- **Compounding is automatic** — no withdrawal temptation (Lesson 5 is enforced by code, not
  willpower).
- **Liquidity-aware scaling** (Gap 1) — never outgrows the names; sizes each trade to what it
  can cleanly exit, at any account size.
- **Multi-name + 24/7 + no tilt + sub-second** — structural edges Ross (one human) can't match.
- The constraint is no longer the math — it's (a) working EXECUTION (the Alpaca lane, P0 live /
  P1 paper) and (b) PROVING the edge live (0 clean fills so far). The scaling engine is ready
  the moment those land.

## Phased build

- **P1 (the key): liquidity-ceiling sizing.** Add `liquidity_participation_fraction` + a per-name
  `equity_relative_notional_cap`-vs-`liquidity_cap` MIN in `compute_risk_first_quantity` /
  the live-runner sizing. Feed `name_dollar_volume` from the snapshot/NBBO tape. Unit-test the
  MIN + the small-account-unchanged / large-account-capped cases. Validate in the replay
  (does a $1M-account run stay within small-cap liquidity?).
- **P2 (optional): daily-profit-goal soft pacing.** Config knob, size-down after the goal.
- Validate both in replay + the Alpaca paper lane before live.

See `project_momentum_zero_fills_root_cause`, `project_momentum_lane`, `MOMENTUM_LANE.md`,
`ALPACA_LANE.md`, `feedback_adaptive_no_magic`.
