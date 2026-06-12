# 2026-06-12 Evening Enhancement Program — Shipped + Weekend Queue

Operator directive: "min/max today's attainable profit, enhance CHILI toward it;
more profitable than Ross; di na sobrang takot at di na lang nagttrade."
Goal anchor: **$1k/day floor by mid-July** (expectancy × cycles × R-size).

## Shipped + deployed tonight (`main-clean-94328a3` scheduler + web)

### Batch 1 — throughput + risk hygiene (#675)
- **A2** watch reap 1800s → 300s base / 600s for tick-armed forming setups
  (triggers that ever fire do so in 29s median; dead watches squatted ~32
  slot-hours/day)
- **A4** live crypto arming OFF (`CHILI_MOMENTUM_CRYPTO_LIVE_ARM_ENABLED=0`):
  0/17 live crypto winners ever; paper/alpaca crypto UNAFFECTED (crypto gates
  moved off the shared eligible list to the live-pick filter)
- **A6** up to 3 arms per pass (74 fresh candidates vs 6 armed in the open burst)
- **EOD flatten**: equities flatten via the operator-flatten chokepoint 5 min
  before the close (QH was saved manually by 8 seconds on a FRIDAY)

### Batch 2 — exit execution honesty (#676)
- **Marketable-LIMIT exit ladder**: bid−guard → 4×guard → market floor
  (30/70 exits had filled WORSE than the planned stop, −15.7R ≈ $428/wk);
  kill-switch/operator flatten stay immediate-market
- **Limit repeg** after 20s unfilled (knob)
- **5m-EMA structural runner trail**: ≥1R runners anchor the stop to the 5m
  EMA9 (ATR wick buffer) instead of the bps band — the BATL 39%-capture fix;
  ratchet-only + breakeven floor preserved; live caller caches EMA per minute

### Batch 3 — booking truth + selection + chase suppression (#677)
- **Booking truth**: `_finalize_stale_exited_sessions` — exited/cooldown
  sessions idle >20 min walk the legal FSM chain to `live_finished`, firing
  the outcome writer (waterfall c0: **$195 of today's exits never booked; the
  day reported −$70 vs −$265 broker truth**)
- **A0 selection re-rank**: arm queue ordered by the Ross sub-score (AUC
  0.58–0.63; ross≥0.8 hits 53% vs 25.4% base) — the composite viability score
  is statistically DEAD (AUC 0.515) and stays only as eligibility floor
- **Chase suppression** (−$286 controllable block): `extended_verticality`
  ATR-scaled EMA9-extension skip on all success paths; VWAP fail-CLOSED on
  deep-reclaim; ask-heavy L2 `imbalance5 < −0.4` halves the risk fraction

### Also today (the incident chain, all with regression tests)
mode-scoped session cap (#665) · crypto US-session pause (#666) · twins don't
eat slots + UI dedupe (#667) · WS subscribe-on-listen (#668) · fresh-tape arm
gate (#669) · RH extended-session fallback (#670) · cap-breach no-flatten
(#671) · exit fill-by-size + broker-zero escape (#672) · cushion risk ladder
(#664) · paper quote sanity (#657) · autopilot money cockpit (#663)

## The evidence base (3 workflow studies, persisted)
- `2026-06-12_profitability-correlation-study.md` — gate counterfactuals
  (every gate PAID; loosening = −980R/wk), winner/loser DNA, throughput
  funnel, exit quality
- Min/max waterfall: mechanical ceiling NEGATIVE (−$358); same rules on the
  right 5 names +$718 → **selection is the lever**; artifacts in
  `scripts/_mm_*.csv`
- Selection alpha: novelty/spread/unextended/degenerate-history pillars
  measured (stacked screen 43.4% hit = 1.71× lift); artifacts in
  `scripts/_alpha_*.csv`

## WEEKEND QUEUE (Sat–Sun, crypto pivot + the big builds)
1. **Crypto profitability program**: B7 counterfactual rerun (backoff-aware),
   Alpaca fee math (0.15/0.25% vs Coinbase 0.6%), dead-trigger removal
   (`momentum_ok_rel_vol` 0/15), paper-intensive on live weekend tape; crypto
   live re-enables ONLY on positive paper evidence
2. **S2**: 10s pattern engine from WS ticks + tick-flow confirmation
   (aggressor imbalance, tape speed) + failed-break tick bailout tuning
3. **S1**: streaming burst detector (burst-to-armed <30s; CUPR was lifted
   AFTER Ross had already scalped it)
4. **Selection pillars**: novelty / explosive_spread / unextended /
   degenerate_history into ross_momentum percentile pillars (A1–A4 of the
   alpha synthesis); 5m structure-health filter
5. **Event lane**: calendar-admitted names bypass price-band/float (SPCX was
   INVISIBLE while Ross made +$41.5k on it); validate vs today's SPCX run
6. **Data gaps**: float/corporate-actions nightly cache, append-only
   selection-truth snapshots, IQFeed depth widening, integrity-gate
   `price_discontinuity` surfacing (doubles as the A4 pillar), container
   egress audit (web/backtest/fast-scan have no outbound internet)
7. Burst partial (+0.5R) stays PAPER-only until a week of evidence

## Ops notes
- Host loopback socket exhaustion (WSAENOBUFS) recurs under heavy test/exec
  churn; Docker engine crashed once tonight and self-recovered (restart
  policies brought all containers back; postgres healthy). Route heavy DB
  work through `docker exec` when it recurs.
- Daily-loss caps restored to adaptive after the close (the $1k absolute was
  2026-06-12 only). Kill switch clean.
