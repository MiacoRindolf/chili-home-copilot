# CHILI Crypto Night-Shift Lane — Implementation Design
**Coinbase spot, long-only, 24/7 Ross-style momentum (shared FSM with equity lane)**
Status: DESIGN — implementation-ready. Baseline to beat: 255 trades / −$200 net / 30d (gross ≈ breakeven, fees are the loss).

---

## 0) The diagnosis, stated mechanically

The lane currently runs Ross's *trigger* (pullback-break on `momentum_neural/live_fsm.py`) without Ross's *catalyst*, and pays taker both ways on Coinbase retail tiers. Two independent failures:

1. **Cost failure**: gross PnL ≈ 0 means realized WR ≈ 33% on a 2:1 — exactly the no-fee breakeven. Every basis point of fees is pure loss. At the observed ~0.5–0.6% blended taker per side, 255 round trips × ~$150 notional ≈ **$420–460/30d in fees** — more than the entire net loss. *(Pull exact realized fees + spread from Coinbase fills and `trading_venue_truth_log` before locking these estimates.)*
2. **Selection failure**: naked breakouts on whatever shows momentum mean-revert in crypto (TS-momentum is real, XS gainer-chasing is not — evidence B−). A 33% WR at 2:1 cannot pay *any* fee level. Cost cuts alone make the lane breakeven; only catalyst-gated selection makes it positive.

Fix both, in that order: execution flip (deterministic, immediate) → catalyst rail (build, measured in Replay Lab).

---

## 1) SELECTION — the crypto-native scanner

Replace "anything moving" with a catalyst taxonomy. Each signal below lists: formula, data source, evidence grade, and how it wires into the existing `momentum_neural` pipeline (`universe.py` → `viability.py` → `entry_gates.py`).

### S1. Short-liquidation burst — squeeze ignition (PRIMARY ENTRY FEEDER) — evidence B
The crypto analog of low-float + halt-resume: the forced buyer is the liquidated perp short. Mandatory taker flow hits spot via arb within seconds.

- **Feed**: Bybit `allLiquidation` public WS (every liquidation, 500ms batches) + Binance `!forceOrder@arr` (largest-per-symbol-per-second; ignition detector only, not a magnitude meter). Free.
- **⚠️ Build risk #0 — geoblocking**: Binance.com (and possibly Bybit) may 451/403 US IPs even for public market data. **Step 0 of the build is a reachability probe from this box.** Fallback: Coinalyze free API (aggregated liq/OI/funding, REST) — adequate for S2 regime gates, *degraded* for S1 ignition (polling latency). If no WS is reachable, S1 ships in regime-confirmation form (60s REST polling) and the design's expected impact drops a tier.
- **State**: per-symbol rolling 60s and 300s sums of **BUY-side** liquidation notional (side=BUY in forceOrder = short force-bought = upward flow).
- **Signal**:
  ```
  ignite(sym) = liq_buy_60s(sym) > z_floor × p99(liq_buy_60s, same symbol, trailing 14d)
                AND spot last > rolling 15m high           (Coinbase WS price bus — already have)
                AND funding(sym) ≤ 0                       (S2 — crowded shorts = fuel)
  ```
  `z_floor` = ONE documented setting (`chili_crypto_liq_burst_z_floor`, base 4.0), self-tuning toward the percentile that historically preceded continuation (Replay Lab, see §5/B4). No other magic numbers — baseline is same-symbol, same-feed.
- **Symbol map**: static `perp_symbol → coinbase_product_id` table (e.g. `1000PEPEUSDT → PEPE-USD`), maintained in `universe.py`; unmapped perps are dropped.
- **Action**: `ignite` symbols are pushed into the candidate queue exactly like the equity tape burst feeder (`nbbo_tape.tape_running_up_symbols`); entry still requires the standard 1m pullback-break/reclaim on the **Coinbase spot** book — the burst is a feeder + tilt, never a market-buy-now.
- **Second entry mode (free reuse)**: long-cascade flush → V-reversal. When SELL-side liq burst spikes then **stops** (60s sum decays below baseline) while price holds a higher low → this is structurally the **halt-resume dip-buy** we just shipped for equities (#597). Same FSM trigger, new arming condition. Evidence B (practitioner consensus, no public horizon stats — measure in-house before sizing above base).

### S2. Funding + OI regime — fuel gauge and veto (GATE/MULTIPLIER, not entry) — evidence B+
- **Feed**: Binance `premiumIndex` + `openInterestHist` REST (free, 5m granularity) or Coinalyze aggregated. Poll 1–5 min; staleness > 15 min ⇒ gate degrades to neutral (no tilt, no veto — never trade on stale regime data as if fresh).
- **States** (per symbol):
  ```
  squeeze_fuel   = funding ≤ 0  AND  OI_pctile(30d) ≥ 80          → +size tilt, S1 prerequisite
  crowded_long   = funding ≥ p95(funding, 90d) AND OI_pctile ≥ 80 → NO new longs (BIS: carry extremes predict crashes)
  oi_quality     = breakout w/ rising 30m OI → continuation (full size)
                   breakout w/ falling 30m OI → squeeze-exhaust (half size or skip)
  ```
- **Wire-in**: a `crypto_regime` block in `viability.py` exactly like the Ross-scorer bridge (±tilt), plus a hard veto in `entry_gates.py` for `crowded_long`.

### S3. Session/time-of-day scheduler — evidence A (best evidence per dollar: $0)
- **Prime windows**: 13:00–17:00 UTC (US/EU overlap; vol+volume peak) and Sun 23:00 UTC → Mon 23:00 UTC (documented trend-persistence window, Sharpe ~1.6 vs 0.8).
- **Penalty windows**: Saturday all day; Sunday US morning (mean-reverting chop — our pullback-break trigger is the wrong tool there).
- **Mechanization**: risk-budget weight per UTC-hour bucket = blend of literature prior × our own realized per-hour expectancy percentile (recomputed weekly from our own fills — adaptive, no hardcoded hour list once data accumulates). Weight multiplies max concurrent slots and per-trade risk. Weekend buckets additionally require the S-Risk depth gate (§4) to pass *at entry time*.
- This is the actual "night shift" schedule: the lane runs 24/7 but risk concentrates where momentum physics works.

### S4. Hour-adjusted RVOL + universe floors — evidence B−
- `RVOL_h(sym) = $vol(last 5m) / median($vol, same UTC hour, trailing 14d)` — hour adjustment is mandatory or S3's periodicity manufactures fake spikes. Computable from our own Coinbase WS/1m-bar history.
- Floor = adaptive percentile-within-scan-batch (consistent with the equity Ross scorer), base reference RVOL_h ≥ 3.
- **Universe gates** (in `universe.py`):
  - Coinbase-listed (given — curation is itself a filter)
  - **has a liquid perp on Binance/Bybit** — this replaces "low float" as the squeeze prerequisite (no perp = no forced buyer = no S1/S2 signal exists for it)
  - mcap floor: adaptive percentile with base reference ~$100M; below floor ⇒ catalyst (S1/S5) strictly required
  - spread/depth gate from the crypto NBBO tape (reuse `nbbo_tape.recent_spread_median_bps` / `read_spread_profile` once crypto rows land in the same table)
- **Rank by the symbol's own trailing state (time-series), never by cross-sectional league table.** 24h-gainer lists are a universe filter only.

### S5. Coinbase listing lane — evidence B
Announcement RSS/X watcher → new pair tradeable **first hours only** with the standard trigger; day-2+ tagged decay (no entries). +41% avg pop, −28% avg decay profile. Bolt-on, low effort, low frequency.

### S6. Rotation regime (BTC.D / ETH-BTC) — evidence C+
Portfolio-level multiplier on total alt-momentum risk budget (not per-trade): BTC.D > 60% ⇒ alt budget × ~0.5; falling BTC.D + rising ETH/BTC ⇒ × 1.0+. Computable in-house from data we already have. Current state (June 2026): headwind — budget low.

### S7. Unlock blacklist — evidence B+ (avoidance, not signal)
DefiLlama/Tokenomist free calendars → daily cron → veto longs in `[T−7d, T+3d]` for unlocks > ~1% circulating supply (90% of unlocks drift negative; team unlocks −25% avg).

### S8. Whale alerts — SKIP (evidence D)
Predicts volatility (r≈0.47), not direction; stale by minutes-to-hours. Noise at 1m horizon.

### P&D veto — evidence B (well-studied thresholds)
Tag and refuse: `price ≥ +90% vs 12h MA AND volume ≥ +400% AND no catalyst tag (S1/S2/S5)`. The Coinbase low-cap tier *is* the pump-and-dump hunting ground (median pumped mcap $2.7M, 95.7% < $60M). This is the literature's canonical signature used as a veto, not a strategy.

---

## 2) EXECUTION POLICY — maker-first (the deterministic half of the fix)

**Rule 0: never taker/taker again.** It is mathematically dead at every reachable tier (table below).

### Entry — post-only join-the-bid with bounded chase
- Plan via existing `coinbase_maker_pricing.plan_post_only_buy_limit` (already built). Coinbase `post_only` **rejects** crossing orders (no repricing) ⇒ chase = client-side cancel/replace loop joining the new best bid each tick. **Never bid below best bid** (queue position is everything; deeper = pure adverse selection).
- **Chase cap**: cumulative chase distance ≤ `chili_crypto_maker_chase_frac_of_stop` (base 0.30) × structural stop distance. Exceeded ⇒ either abandon (default) or taker-fallback (rule below). A signal too fast to fill maker is *exactly* the signal whose taker fill is most expensive — abandoning is a feature.
- **Rate budget**: Coinbase Advanced private REST ≈ 5–10 rps; cap the chase loop at ~2 reprices/sec/order and listen on the WS `user` channel for fills (no REST polling). Reuse `stuck_order_watchdog.py` semantics for orphaned post-only orders.
- **Honest cost not in the fee table — adverse selection**: the best empirical study (232k maker orders, Binance BTC perp) shows passive fills are anti-correlated with subsequent returns (markouts negative in every regime; fill probability ~30% during genuine fast moves, and the fills you get skew to failed breakouts). This is real and bounded; the taker fee is paid on 100% of trades. We will *measure our own* maker-fill markout in `execution_audit`/`trading_venue_truth_log` and treat it as an added F term, not pretend it's zero.

### Taker fallback — the only time taker entry is allowed
```
taker_ok = expected_target_distance ≥ chili_crypto_taker_edge_multiple × (taker_roundtrip_fee + observed_spread)
```
Base multiple 3.0. At the $10K tier this means taker only for A+ signals targeting ≥ ~2.5–3% — i.e., S1 ignitions with full squeeze-fuel confirmation, nothing else. This is the existing `risk_policy.max_fee_to_target_ratio` philosophy, made side-aware; extend that code path rather than adding a parallel gate.

### Exits — asymmetric by construction
- **Target (2R / scale-out): always maker.** Post the limit sell immediately on entry fill (or use Coinbase `trigger_bracket_gtc` to attach TP/SL natively). It rests above market; adverse selection works *for* us (selling into strength). Zero chase problem.
- **Stop: always taker.** A protective stop crosses by construction. Never post-only a stop. No exceptions — this is a safety rule, not an economics rule.
- **Trail/bailout (FSM `live_trailing` → exit, `bailout`): taker.** Same reasoning.

### Breakeven math (the table that governs everything)
`EV = w·(T − F_win) − (1−w)·(S + F_loss)` ⇒ `w* = (S + F_loss) / (T − F_win + S + F_loss)`.
S = 1% stop, T = 2% target (the lane's actual regime). F per *round trip*, win-path vs loss-path:

| Tier (30d vol) | Policy | F_win | F_loss | Breakeven WR | Verdict |
|---|---|---|---|---|---|
| <$1K (0.60/1.20) | taker/taker | 2.40 | 2.40 | 113% | impossible |
| $1K (0.35/0.75) | taker/taker | 1.50 | 1.50 | 83% | dead |
| $10K (0.25/0.40) | taker/taker | 0.80 | 0.80 | **60%** | dead — this is roughly today's lane |
| $1K | maker-in/maker-target/taker-stop | 0.70 | 1.10 | 62% | dead — tier matters even maker-first |
| $10K | maker/maker-target/taker-stop | 0.50 | 0.65 | **52%** | marginal |
| $50K (0.15/0.25) | maker/maker-target/taker-stop | 0.30 | 0.40 | **45%** | viable IF WR > ~50% |
| Kraken $50K equiv | same policy | ~0.19 | ~0.28 | ~40% | cheaper everywhere |
| Coinbase CFM perps (BTC/ETH only) | any | 0.04–0.08 | +funding | **~35%** | fee problem solved, majors only |
| Binance.US (0/0.02) | maker/maker | 0.00–0.04 | 0.04 | **~33–35%** | fee-free; book depth + counterparty TBD |

**Read the table honestly**: maker-first + the $50K tier moves breakeven from 60% → 45%. Our current realized WR is ~33%. **Execution alone makes the lane breakeven-ish (fees stop bleeding), it does not make it profitable.** Selection (S1–S4) must lift conditional WR above ~45%, or realized R above 2 via the trail — that is the measurable bet, and Replay Lab (B4) is where it gets measured before sizing up.

### Fee-tier engineering
- Both sides of every round trip count; tiers re-rate **hourly** on trailing 30d volume. $10K tier needs ≥ ~$20 avg notional at current trade count; $50K needs ≥ ~$98. Current ~$100–200 notionals already imply $50K–100K/30d *if concentrated on Coinbase* — verify from fills, then treat **holding the $50K tier as a constraint** in the sizing policy (don't let the adaptive sizer shrink notionals below the tier-holding floor while trade count is stable).
- Coinbase One: no help (simple-trade-only, capped, spread-embedded). Fee-upgrade program: not at our scale. Stable-pair churn: unverified that it still counts — do not build on it.

---

## 3) REUSE vs CHANGE in the shared lane

| Component | Verdict | Detail |
|---|---|---|
| FSM (`live_fsm.py`: pullback-break/reclaim → entered → scaling_out ⇄ trailing → exited/bailout) | **REUSE unchanged** | The state machine is venue-agnostic. The post-flush V entry reuses the halt-resume dip-buy arming path (#597) with S1 flush-detection as the arm condition. |
| Structural stops | **REUSE** | Stop geometry is price-structure logic. One change: minimum stop distance gains a fee floor — `S ≥ F_loss/(3w_assumed − 1)` — so the adaptive geometry can never select a stop too tight to pay its own fees. |
| 2:1 target | **KEEP at 2:1, with the fee floor above** | At $50K-tier maker-first, 2:1 breakeven is 45% — survivable. Do NOT widen to 3:1 globally (3:1 breakeven 34% but trigger hit-rate drops too; no evidence our entries reach 3R at compensating frequency). Wider targets are enforced only on the taker-fallback path via the 3× edge multiple, which naturally forces taker trades to be the ≥2.5–3% movers. |
| Scale-out + cushion-adaptive trail | **REUSE; exits become maker where resting** | Scale-out limit = maker. Trail executions = taker (safety). |
| Entry execution | **CHANGE — the core change** | Market/IOC entry → post-only chase per §2. `coinbase_maker_pricing.py` + `venue/coinbase_spot.py` post_only support already exist; the work is the chase loop + WS fill listener in the runner. |
| Selection (`universe.py`, `viability.py`, `entry_gates.py`) | **CHANGE — catalyst-required** | Equity lane: Ross scorer (RVOL/gap/float). Crypto lane: S1/S2 catalyst tags + hour-adjusted RVOL + has-perp + mcap floor + P&D veto + unlock blacklist. Same bridge pattern as `ross_momentum.py` (±tilt into viability), so the shared pipeline shape is preserved. |
| Burst feeder | **REUSE pattern, new source** | Equity: `nbbo_tape.tape_running_up_symbols`. Crypto: same queue contract fed by (a) liq-burst igniter, (b) crypto NBBO running-up scan once the crypto tape lands. |
| Spread-stability gate | **REUSE directly** | Crypto NBBO rows land in the same table ⇒ `recent_spread_median_bps`/`read_spread_profile` work as-is per product. |
| Replay Lab (`replay_v2.py`) | **REUSE + extend** | Record liq/funding feeds alongside ticks; replay answers the one question no public study answers: continuation horizon/hit-rate after short-liq bursts **on Coinbase spot**. |
| Risk caps | **SHARED aggregate** | Crypto lane draws from the same equity-relative aggregate at-risk cap as the equity lane (one budget, §4), with a crypto correlation haircut. |

---

## 4) RISK — crypto-specific guards

1. **Correlation guard (the big one)**: alt momentum candidates are one trade in disguise — beta-to-BTC during cascades → ~1. Rules: (a) crypto lane positions count against the **shared** aggregate at-risk cap with the equity lane; (b) **cluster cap** — all entries triggered within the same N-minute liquidation-burst window across symbols count as ONE correlated position for at-risk purposes (a cascade igniting 5 alts is one bet on the squeeze, not five); (c) BTC crowded-long state (S2 on BTC itself) halves the whole lane's budget — when BTC flushes, every alt long is wrong simultaneously.
2. **Weekend liquidity gate**: volume −20–25%, thin books, overshoot is amplified in alts. Hard depth gate at entry: visible book depth within 1× stop distance ≥ k × intended notional (adaptive k, percentile of that symbol's weekday depth). Saturday + Sunday-US-morning hour-buckets get the S3 penalty weight regardless.
3. **Manipulation/rug filter (yes, on a regulated venue)**: P&D veto signature (§1) + mcap floor + catalyst-required below floor. Coinbase listing is curation, not immunity — the low-cap tier is actively hunted.
4. **Unlock blacklist** (S7): veto window `[T−7d, T+3d]`, supply > ~1% circulating.
5. **Feed-degradation policy** (no dark failure): liq WS down > 60s ⇒ S1 lane stops *arming new* burst entries (existing positions manage normally); funding/OI stale > 15 min ⇒ S2 tilts/vetoes go neutral. Log loudly (`[crypto_catalyst_rail]`), never trade on stale catalyst data as fresh.
6. **Stale-quote guard**: Coinbase WS price bus heartbeat; quotes stale > 5s ⇒ no entries, trail falls back to last-good structural stop (taker).
7. **Existing breakers unchanged**: drawdown breaker before sizing, kill switch before any automated trade — both apply to this lane exactly as everywhere else (hard rules 1–2).
8. **Measurement honesty**: per operator's no-dark-flags rule, everything ships live and on — but **size is evidence-gated, not flag-gated**: each new signal lane starts at the adaptive base risk unit and earns size via realized expectancy percentile in the shared sizing policy. Live + small + measured ≠ dark.

---

## 5) PRIORITIZED BUILD LIST

### Quick wins (days; deterministic payoff)
| # | Item | Expected impact (30d, honest range) |
|---|---|---|
| QW1 | **Maker-first execution flip**: post-only chase entry (reuse `coinbase_maker_pricing.plan_post_only_buy_limit`), maker 2R/scale-out exits (or `trigger_bracket_gtc`), taker stops, WS `user`-channel fills, stuck-order watchdog hookup | Fees ~$420–460 → ~$115–150 at current volume/tier ⇒ **+$250–350/30d**; flips −$200 to ≈ breakeven before any selection gain. Partially offset by maker adverse selection + missed fills (measure, don't assume zero) |
| QW2 | **Side-aware fee gate**: extend `risk_policy.max_fee_to_target_ratio` to win/loss-path fees; taker-fallback 3× edge multiple; stop-distance fee floor | Kills the worst trades outright; **+$30–80** and prevents regression |
| QW3 | **Fee-tier audit + tier-holding constraint** in sizing (verify current tier from fills; hold ≥$10K, target $50K) | 0.40→0.25 taker / 0.25→0.15 maker if currently below; **+$50–150** depending on where we actually sit |
| QW4 | **Hour-of-day scheduler + weekend depth gate** (S3, S-Risk 2): UTC-hour risk weights, Sat/Sun-AM penalty, prime-window concentration | Evidence A, cost $0. Cuts the chop-regime losers; **+$30–100**, tightens variance |
| QW5 | **Unlock blacklist + P&D veto** (S7 + veto): daily cron + scanner tag | Low frequency, high severity avoided losses; unquantifiable ex-ante, cheap insurance |

### Builds (1–3 weeks; the actual edge)
| # | Item | Expected impact / note |
|---|---|---|
| B0 | **Feed reachability probe** (hours, gates B1): Bybit `allLiquidation` + Binance `!forceOrder@arr` + `premiumIndex` from this box; pick primary/fallback (Coinalyze) | Decides B1's shape. Do first. |
| B1 | **Liquidation/funding recorder + burst igniter** (S1): WS consumers → `trading_crypto_liq_ticks` + `trading_crypto_funding_oi` (pruned like the NBBO tape, mig NNN idempotent); per-symbol rolling sums; perp→Coinbase map; feeder into candidate queue; flush-detector arming the dip-buy path | The catalyst-supply fix — the only signal class with a mechanical forced buyer. Direction: should lift conditional WR toward/above the 45% bar. Magnitude honestly unknown (evidence B, no public Coinbase-spot backtest) — B4 measures it |
| B2 | **Funding/OI regime gates** (S2) into `viability.py`/`entry_gates.py`: squeeze-fuel tilt, crowded-long veto, OI-direction size multiplier | Evidence B+; regime-level, so impact is loss-avoidance + sizing quality |
| B3 | **Universe rework** (S4): hour-adjusted RVOL, has-perp gate, adaptive mcap floor, TS-not-XS ranking | Prerequisite plumbing for S1/S2 to have a clean candidate set |
| B4 | **Crypto Replay Lab calibration**: crypto NBBO tape rows (planned) + recorded liq feed → replay measures burst→continuation horizon/hit-rate, maker-chase fill rate + markout, realized spread by hour | Closes the named evidence gap; sets `z_floor`, chase fraction, and the sizing curve from OUR data. The lane does not size up past base until this reports |
| B5 | **Listing lane** (S5): announcement watcher, first-hours-only arming | Bolt-on; episodic +EV events |
| B6 | **Venue decision memo** (operator decision, not code): Coinbase CFM nano perps for the BTC/ETH slice (~35% breakeven WR, same broker, CFTC-regulated) vs Kraken Pro / Binance.US spot for alts. Needs accounts/keys — operator action like the Alpaca lane | If signal WR proves >36% in B4, venue migration alone is worth more than everything above; flagged early because of account lead time |

### Sequencing
QW1–QW5 ship together as one execution-policy change-set (live immediately — pure cost reduction on an already-live lane). B0→B1→B4 is the critical path for the edge; B2/B3 ride alongside; B5/B6 follow evidence.

### Success criteria (30d after QW set + B1)
- Realized round-trip fee % of notional: < 0.45% blended (from fills, not estimates)
- Maker entry fill rate and post-fill 5s/60s markout: measured and published in the brain desk summary
- Lane net PnL: ≥ breakeven post-QW; positive only counts if B4's burst-conditional WR > breakeven bar at the realized tier
- Zero taker/taker round trips (audited in `execution_audit` — any occurrence is a bug)

---

**Evidence-grade summary, restated bluntly**: session timing (A) and fee math (arithmetic) are certain — ship on them. Liquidation-squeeze continuation on Coinbase spot specifically is mechanism-strong but horizon-unquantified (B) — ship live at base size, let Replay Lab set the sizing curve. Funding extremes (B+), unlocks (B+), P&D signature (B) are gates/vetoes, properly cheap. Rotation (C+) is a budget dial. Whale alerts (D) are excluded. The one claim this design refuses to make: that any selection upgrade is *proven* to lift WR above 45% — that is precisely what B4 exists to measure before the lane earns size.