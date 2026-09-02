# Stop-out forensics: stop distance vs spread / 1-min noise (Alpaca live-mode, 21 days)

**Date:** 2026-09-02 · **Branch:** `seam/stop-noise-floor-0902` (off `origin/main` 9fad8adf9) ·
**Dataset:** `docs/STRATEGY/CC_REPORTS/2026-09-02_stop-noise-floor-forensics.csv` (16 rows, 204 columns) ·
**Script:** `scripts/research/stop_noise_forensics_0902.py` (pure helpers + file-only CLI) ·
**Tests:** `tests/test_stop_noise_forensics_0902.py` (DB-free; AST guard that the CLI never imports a DB layer).

No production code was changed. Nothing was launched, killed, or ordered. All DB reads were
read-only, `statement_timeout='30s'`, one symbol + a bounded window each.

## 0. TL;DR (raw findings, not a recommendation)

* **Population is tiny.** 21 days of `mode='live'` × `execution_family alpaca_*` yield **11 exits with a
  `live_exit_filled` event**, of which **8 are stop-distance-driven** (`stop`/`trail_stop`/`deadman_stop`
  + the `max_loss_circuit` bailout). 3 are fast-bail / lost-VWAP bailouts (not stop-distance). 5 more
  entries have **no exit fill event at all** (operator flatten / unpriced / process death) and are kept as
  unreadable rows, not dropped.
* **"Breached by a single wide-quote tick" is NOT what the tape shows.** In 7/8 stop-driven exits the bid
  stayed at/below the stop for **≥28 of the 60 s after the breach** (CANF 60/60, AUUD 60/60, SDOT 58/60,
  SSM 49, LIDR2 46, UPC 45, LIDR1 28); only JLHL (9/60, dip 0.24 % below stop) looks like a shallow
  shake. The breaches were 1–9 % flushes (min bid within 60 s: CANF −5.3 % below stop, AUUD −7.0 %,
  SDOT −8.8 % post-halt, SSM −2.3 %, UPC −1.8 %).
* **But 5/8 recovered above entry inside 30 min** (CANF +7.8 %, LIDR1 +9.7 %, JLHL +4.6 %, SSM +4.2 %,
  AUUD +3.6 %, SDOT +7.9 %; UPC and LIDR2 did not). The stops were inside the *flush* range, not inside the
  *spread*: initial stop distance was **1.2–4.7 × the entry spread** (median 2.9×) but only
  **0.27–1.17 × the pre-entry 1-min true range** (median 0.93×; CANF 0.27×, AUUD 0.30×).
* **Spread floors (F1, k = 1.5/2/3) change almost nothing:** net vs baseline sim **+0.00 / +1.16 / +10.44 $**
  at same qty (3–5 of 8 stops touched, 0 extra loss). The stops already clear the spread.
* **1-min-TR floors (F2, m = 1/1.5/2) are where the money is, and it is mostly SIZE, not survival:**
  net vs baseline **+54.76 / +79.98 / +94.78 $** at same qty, **+116 / +153 / +169 $** with risk-first
  requantisation (qty shrinks 1.5–7.5×). Stop-outs "avoided": 3/5/5 of 8 — but what happens next is the
  **#1277 burst exit** (4 of 5), which sells the bounce at ≈ entry −1 % to −3 % (CANF 4.23 vs entry 4.34;
  JLHL 7.34 vs 7.40), i.e. the wider stop converts a −2.9R loss into a −0.3R loss, not into a win. Only SSM
  reaches the target in the sim (and SSM's real target exit was lost to the 31-deferral stale-BBO plumbing,
  not to the stop).
* **The viability tighten is a magic number sitting inside the spread.** `live_runner.py:40951`
  `tighter_stop = max(_live_stop_c4, avg * 0.995)` — a fixed 0.5 % under entry with no spread/tick/TR
  floor. All 3 tightens in the window (CANF 4.3183 = 1.08 × spread; LIDR2 1.6616 = 0.84 × spread; CDTG
  1.3631) were **breached within 0.1–4 s** of being set. Flooring them alone would not have saved CANF
  (the flush went 5 % through the *initial* stop too) — but it is the one place in the stack where a
  stop is set by a constant rather than a measurement.
* **Fill slippage past the stop is the second-largest cost:** CANF filled **4.57 %** below its stop
  (7.7 s breach→fill, through a 335-print/46k-share flush second), AUUD 6.13 % (14.4 s), SDOT 2.53 %
  (post-halt gap). Median breach→fill = 14.2 s (range 7.7–37 s).

## 1. Mechanism inventory (what already exists in `origin/main` 9fad8adf9)

| layer | where | how the distance is set | spread / noise floor? |
|---|---|---|---|
| Initial software stop | `live_runner.py:33527,33543` → `paper_execution.stop_target_prices` | `entry × (1 − max(0.003, atr_pct × stop_atr_mult))`; `atr_pct` = `entry_stop_atr_pct` frozen at sizing | **0.3 % hard floor** (magic); vol floor below |
| Vol floor | `paper_execution.effective_stop_atr_pct` (`live_runner.py:35201`) | `atr_pct ≥ vol_floor_mult(0.5) × expected_move_bps(15 m OHLCV)/mult`, capped 0.15 | measured (15 m frame), slow/blind on fresh names |
| Structural stop | `structural_or_vol_floored_atr_pct` (`live_runner.py:35248`) | pullback low if further than the vol floor; structure-capped floor ×1.25 | n/a |
| #1278 own-tape noise floor | `risk_policy.stop_noise_floor_decision` + `_own_tape_noise_floor_pct` (`live_runner.py:21440,35213`) | stop ≥ median 30 s high-low range (10 buckets, 900 s lookback) | **flag `chili_momentum_stop_noise_floor_enabled` OFF** — rejected by replay A/B 09-02 (LIDR entry lost below min size, SLE −1.07) |
| Deadman (broker) stop | `_place_deadman_stop…` (`live_runner.py:12419`) | quantised copy of the software stop, 0.5–4 c below it | **inert premarket** — `live_deadman_stop_inert_until_rth` on 9/11 exits here |
| Viability tighten (C4) | `live_runner.py:40938-40964` | `max(live_stop, avg × 0.995)` when viability < 0.85 × admission | **none — fixed 0.5 %** |
| Breach confirm | `live_runner.py:44637-44672` | bid ≤ stop on two reads ≥ 1 s apart; flicker dodge on recovery | time filter only |
| L2 classify / chop hold | `live_runner.py:44684-44735` | `classify_stop_breach` (ladder OFI/refill); hold ≤ 2 ticks / 2.5 s when CHOP | held: CANF `stale_or_missing_l2` → BREAKDOWN at 2.07 s |
| Trail (cushion + volnorm + ride-lock) | `live_runner.py:41416-41812` | composed via `max()`; **`trail_noise_floor_clamped`** clamps the candidate to `hwm × (1 − vn_dist)` (name's live realised vol) | yes, for the trail only (fired 3× here) |
| Breakeven floor | `live_runner.py:41418` | `avg` after a partial, else the placed stop | n/a (Alpaca lane cannot partial, #1264) |
| Tape-accel reversal lock | `live_tape_accel_reversal_exit` | adaptive stop near HWM (SSM: 4.02116 above entry) | flow-confirmed, unclamped by design |
| Burst-window exit (#1277) | `_burst_window_fires` / `burst_window_decision` (`live_runner.py:22349-22450`) | bid ≥ 1.5 % over the 60 s lookback low arms; exit 45 s later (+ latency) — no "above entry" condition | n/a |
| Max-loss circuit | `live_bailout max_loss_circuit` | unrealised < −2 × structural risk (qty × stop dist) | scales with stop distance |
| Fast-bail / lost-VWAP / viability bail | `_bailout_dwell_confirm_holds`, viability floor | price-level rules, not stop distance | n/a |
| Tick size | `quantize_alpaca_equity_limit_price` on the entry ask only | no minimum stop distance in ticks anywhere | none |

## 2. Data pulls (read-only, bounded)

```
-- sessions: mode='live' AND execution_family LIKE 'alpaca%' AND created_at >= now()-22d  → 4,081 sessions / 252 symbols
-- exits: trading_automation_events WHERE event_type IN ('live_exit_filled','live_bailout','live_partial_exit','live_entry_filled') → 12 live_exit_filled (1 = operator_flatten BRNX, excluded), 5 live_bailout
-- per-session events (17 sids), risk_snapshot_json (momentum_live_execution) and runtime snapshots
-- iqfeed_trade_ticks  : symbol='X' AND observed_at in [entry−6 min, exit+31 min], per-second min/max price, min/max bid, ask
-- momentum_nbbo_spread_tape: symbol='X' AND observed_at in [entry−3 min, exit+2 min], per-second min bid / max ask / spread_bps
```
SDOT's first tick pull timed out at 30 s (08-21 partition); a narrower [14:40, 15:20] window succeeded.
SDOT has no NBBO tape (5 buckets) → no spread measures. Ledger stop-model fields (`entry_stop_atr_pct`,
`structural_stop_price`) are **overwritten on recycle** (CANF's ledger shows the 11:19 second cycle), so initial
stops were taken from `viability_degraded_tighten.old_stop` / `momentum_mfe_realized.stop_distance` /
`stop_breach_pending_confirm.stop_price`, which are cycle-exact.

## 3. Dataset (11 exits with a fill event)

| sid | sym | entry (UTC) | px | qty | initial stop (dist %) | deadman | tightens | exit reason | breach->fill s | stop@breach | breach bid | spread@breach bid/ask (bps) | dist / spread | init dist / entry-spread | init dist / 1m TR | fill | slip vs stop % | pnl $ | MFE R (bid) | bid<=stop s/60 | min bid 60s (dip % below stop) | post max +5/+15/+30m vs entry % | post min 30m vs stop % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 14825 | SDOT | 2026-08-21 14:45:59 | 17.360 | 14 | 16.8738 (2.80) | 16.76 | - | deadman_stop | 11.6 | 16.7600 | - | -/- (-) [none] | - | - | - (halt gap) | 16.3200 | 2.53 | -14.56 | - | 58 | 15.23 (8.81) | 3.1 / 3.1 / 7.9 | -11.6 |
| 19216 | RDHL | 2026-08-31 12:43:49 | 1.440 | 205 | 1.3300 (7.64) | 1.31 | - | bailout/lost_vwap_flatten | - | - | - | -/- (-) [none] | - | 11.00 | 2.75 | 1.4300 | - | -2.05 | 0.00 | - | - (-) | -0.7 / -0.7 / -0.7 | - |
| 19261 | GYGY | 2026-09-01 08:42:05 | 1.430 | 584 | 1.3862 (3.06) | 1.38 | - | bailout/breakout_failed_fast_bail | 8.7 | 1.3862 | 1.40 | 1.38/1.39 (72) | 4.37 | 4.37 | 1.46 | 1.3800 | 0.44 | -29.20 | 0.23 | 21 | 1.38 (0.44) | -2.1 / 3.5 / 12.6 | -8.4 |
| 19299 | WETO | 2026-09-01 10:21:12 | 7.750 | 177 | 7.6188 (1.69) | 7.59 | - | bailout/breakout_failed_fast_bail | 15.7 | 7.6188 | 7.65 | 7.65/7.71 (78) | 2.19 | 4.37 | 2.24 | 7.6500 | -0.40 | -17.70 | -0.15 | 2 | 7.61 (0.11) | -1.3 / -0.9 / 4.0 | -0.9 |
| 19315 | SSM | 2026-09-01 10:44:37 | 4.010 | 433 | 3.9725 (0.94) | 3.97 | tape-accel lock 4.02116 @10:46:45 (above entry) | stop | 18.0 | 4.0212 | 3.94 | 3.94/3.99 (126) | -0.22 | 1.88 | 0.94 | 3.9500 | 1.77 | -25.98 | 3.20 | 49 | 3.93 (2.27) | 4.2 / 4.2 / 4.2 | -3.3 |
| 19337 | AUUD | 2026-09-01 11:10:55 | 1.110 | 551 | 1.0981 (1.07) | 1.09 | - | bailout/max_loss_circuit | 14.4 | 1.0981 | - | 1.04/1.05 (96) | 1.19 | 1.19 | 0.30 | 1.0301 | 6.13 | -44.01 | 0.00 | 60 | 1.02 (7.04) | 3.6 / 3.6 / 3.6 | -14.4 |
| 19394 | LIDR | 2026-09-01 12:26:47 | 1.650 | 150 | 1.6141 (2.18) | 1.61 | - | stop | 17.7 | 1.6141 | 1.61 | 1.60/1.61 (62) | 3.59 | 3.59 | 0.72 | 1.6173 | -0.19 | -4.91 | -0.28 | 28 | 1.60 (0.85) | 3.0 / 9.7 / 9.7 | -2.7 |
| 19415 | LIDR | 2026-09-01 12:59:15 | 1.670 | 143 | 1.6230 (2.81) | 1.62 | 13:03:22.879 viability 1.6230->1.6616 (via 0.6741->0.4641) | trail_stop | 37.0 | 1.6616 | 1.64 | 1.64/1.67 (61) | 0.28 | 4.70 | 1.17 | 1.6500 | 0.70 | -2.86 | 0.21 | 46 | 1.64 (1.30) | 0.6 / 0.6 / 0.6 | -9.1 |
| 19457 | UPC | 2026-09-02 08:40:22 | 5.396 | 314 | 5.3085 (1.62) | 5.29 | - | stop | 9.2 | 5.3090 | 5.24 | 5.24/5.29 (95) | 1.74 | 2.92 | 1.09 | 5.2400 | 1.28 | -48.97 | 0.05 | 45 | 5.21 (1.83) | -2.5 / -1.8 / -1.8 | -4.5 |
| 19463 | JLHL | 2026-09-02 10:47:35 | 7.396 | 149 | 7.2177 (2.42) | 7.18 | - | stop | 14.0 | 7.2181 | 7.20 | 7.20/7.22 (28) | 8.92 | 4.47 | 0.92 | 7.2700 | -0.70 | -18.83 | -0.15 | 9 | 7.20 (0.24) | -0.4 / 4.6 / 4.6 | -9.3 |
| 19471 | CANF | 2026-09-02 11:10:20 | 4.340 | 355 | 4.2654 (1.72) | 4.25 | 11:10:55.220 viability 4.2654->4.3183 (via 0.76->0.6417) | stop | 7.7 | 4.3183 | 4.28 | 4.12/4.15 (73) | 0.72 | 2.49 | 0.27 | 4.1199 | 4.57 | -78.13 | 0.13 | 60 | 4.09 (5.26) | 1.4 / 7.8 / 7.8 | -13.9 |

Column notes: *dist / spread* = (entry − stop@breach) ÷ (ask − bid) at the breach second (NBBO tape,
≤ 10 s before the breach event); *init dist / entry-spread* uses the median NBBO width over the 60 s before
entry; *init dist / 1m TR* uses the median of the five 1-min true ranges before entry (halt bars skipped);
*MFE R (bid)* = max bid between entry and breach ÷ initial stop distance; *bid<=stop s/60* = seconds in the
60 s after the breach whose bid-low was ≤ the stop; *post max* uses bid highs after the fill; *post min 30m vs
stop* uses bid lows. GYGY/WETO/RDHL are not stop-distance exits; their "stop@breach" is the stop in force at
the bail. SSM's stop@breach is the tape-accel reversal lock (4.02116, *above* entry), placed after the
target exit at 10:46:45 was deferred 31× on stale BBO; its initial 3.9725 stop was never hit.

**Unreadable / no exit-fill rows (kept, not dropped):**

| sid | sym | entry | why unreadable |
|---|---|---|---|
| 14842 | COIW | 08-21 14:55:59 9.91 ×177, stop 9.8231 / deadman 9.80 | 3 breach confirms 15:08–15:12 (2 flicker-dodged), deadman re-place rejected ("stop price must be less than current price"), operator flatten; no `live_exit_filled` |
| 15152 | XPON | 08-24 13:00:26 8.17 ×51, deadman 7.84 | `max_loss_circuit` bail 13:20 (−58.14 unrealised), deadman certification failed, `live_emergency_exit_unpriced` 16:39 — fill price unknown |
| 15344 | BDRX | 08-25 09:03:37 1.51 ×880, deadman 1.46 | no exit event in session (see memory: BDRX +4.0R round-trip / ghost position) |
| 16534 | CDTG | 08-26 11:41:24 1.37 ×191, stop 1.3185 / deadman 1.31 | 15 breach confirms 11:42–11:45 (2 chop-holds), viability tighten 1.3185→1.3631 at 11:43:47 breached 0.1 s later; runner process died 11:45 (deploy #1179); operator manual exit 191 @ 1.16 at 12:00:28 (−15 %) |
| 18035 | AEMD | 08-27 22:37:55 3.34 ×95 (after-hours), deadman 3.12 inert | viability bail 22:42 (0.34) never filled; 10 halt/resume cycles; no exit event |

## 4. Counterfactual floors (8 stop-distance-driven exits)

Rules: **F1** stop distance ≥ k × median entry spread (k ∈ {1.5, 2, 3}); **F2** ≥ m × median pre-entry
1-min true range (m ∈ {1, 1.5, 2}); **F3** = max(F1, F2). Applied **widen-only** to the initial stop
(unbounded — no same-cycle structural stop survives recycle in the ledger, see §2) and to the viability
tighten **bounded by the stop in force before it** (the floored initial stop). Simulation: per-second
bid-based walk from the real entry through 30 min after the real exit; stop breach on bid-low ≤ stop,
priced with the trade's own observed breach→fill latency (bid at breach + latency); target = entry +
`applied_target_r` × new distance; the #1277 burst rule (bid ≥ 1.5 % over the 60 s bid-low → exit 45 s + 12 s
later) is ON for every trade; other exit layers (fast-bail, L2 hold, trail) are ignored. Risk-first
requantisation: qty′ = ⌊qty × old dist ÷ new dist⌋ (constant $ risk).

| rule | changed | stop-outs avoided N | avoided $ (baseline sim loss) | then: target / burst / wider-stop / open | extra loss $ (wider stop, vs baseline) | net $ vs baseline same-qty | net $ vs baseline risk-first qty | net R (same qty) | net $ vs ACTUAL same-qty |
|---|---|---|---|---|---|---|---|---|---|
| F0_unchanged (sim calibration) | 0/8 | - | baseline sim -207.00 vs actual -238.25 | - | - | - | - | - | +31.25 |
| F1_k1.5 | 3/8 | 1 | -2.86 | 0 / 1 / 2 / 0 | +0.00 | +0.00 | +9.28 | +0.00 | -7.14 |
| F1_k2 | 4/8 | 2 | +14.55 | 1 / 1 / 2 / 0 | +0.00 | +1.16 | +17.96 | +0.07 | +37.41 |
| F1_k3 | 5/8 | 2 | +14.55 | 1 / 1 / 3 / 0 | +0.00 | +10.44 | +42.81 | +0.64 | +43.55 |
| F2_m1 | 6/8 | 3 | -70.65 | 1 / 2 / 3 / 0 | +0.00 | +54.76 | +116.24 | +2.09 | +83.97 |
| F2_m1.5 | 7/8 | 5 | -101.43 | 1 / 4 / 2 / 0 | +0.00 | +79.98 | +153.24 | +3.90 | +106.05 |
| F2_m2 | 7/8 | 5 | -101.43 | 1 / 4 / 2 / 0 | +0.00 | +94.78 | +169.46 | +5.31 | +120.84 |
| F3_k1.5_m1 | 6/8 | 3 | -70.65 | 1 / 2 / 3 / 0 | +0.00 | +54.76 | +116.24 | +2.09 | +83.97 |
| F3_k2_m1.5 | 7/8 | 5 | -101.43 | 1 / 4 / 2 / 0 | +0.00 | +79.98 | +153.24 | +3.90 | +106.05 |
| F3_k3_m2 | 7/8 | 5 | -101.43 | 1 / 4 / 2 / 0 | +0.00 | +94.78 | +169.46 | +5.31 | +120.84 |

"stop-outs avoided" counts trades whose floored stop was **not** hit before the target/burst/horizon;
"avoided $" is their **baseline-sim** loss (SSM's baseline sim is +17.41 because the sim reaches the target
the plumbing missed, which is why F1_k2's "avoided $" is positive). "wider-stop" = still stopped at the
floored stop; its extra loss is ≤ 0 by construction and was **0.00 in every rule** because every wider stop
that was hit was hit inside the same flush and filled at the same post-latency bid. SDOT contributes no
measure (halt gap: pre-entry TR undefined, no NBBO). Per-trade detail (all 204 columns) is in the CSV.

Per-trade view of the two rules that matter (same qty → risk-first qty, $):

| trade | actual | baseline sim | F1_k3 | F2_m1 | F2_m1.5 |
|---|---|---|---|---|---|
| SSM (init 0.94 %, TR 1.00 %) | −25.98 | +17.41 target | +27.85 → +17.37 target | +18.57 → +17.37 target | +27.85 → +17.37 target |
| AUUD (1.07 %, TR 3.60 %) | −44.01 | −44.08 stop | −44.08 → −17.36 stop | −44.08 → −13.04 stop | −38.57 → −7.56 stop |
| LIDR1 (2.18 %, TR 3.03 %) | −4.91 | −4.50 stop | unchanged | −4.50 → −3.21 stop | −4.50 → −2.13 burst @1.62 |
| LIDR2 (2.81 %, TR 2.40 %) | −2.86 | −2.86 burst | −2.86 (tighten floored to 1.64, still burst first) | −2.86 | −2.86 → −2.22 burst |
| UPC (1.62 %, TR 1.48 %) | −48.97 | −52.11 stop | −52.11 → −50.62 stop | unchanged | −52.11 → −37.84 stop |
| JLHL (2.42 %, TR 2.64 %) | −18.83 | −26.28 stop | unchanged | −18.83 → −17.19 stop | −8.40 → −5.07 burst @7.34 |
| CANF (1.72 %, TR 6.45 %) | −78.13 | −85.20 stop | −85.20 → −70.56 stop | −39.05 → −10.34 burst @4.23 | −39.05 → −6.93 burst @4.23 |
| SDOT (2.80 %, halt) | −14.56 | −9.38 stop | no measure | no measure | no measure |

Calibration: the baseline sim reproduces AUUD (−44.08 vs −44.01), LIDR2 (−2.86), UPC (−52.11 vs −48.97),
CANF (−85.20 vs −78.13) and is early on JLHL (sim breaches at t+46 s on a 1-second bid dip the runner's ~2 s
read cadence skipped; runner breached at t+76 s). SSM differs by +43.39 because the sim takes the target the
live plumbing deferred — that gap is exit plumbing, not stop geometry, and is excluded from the "vs baseline"
columns.

## 5. What the tape says about the hypothesis

1. **The stop is not inside the spread; it is inside the 1-min range.** Initial stop ÷ entry spread:
   1.19 (AUUD), 1.88 (SSM), 2.49 (CANF), 2.92 (UPC), 3.59 (LIDR1), 4.37 (GYGY, WETO), 4.47 (JLHL), 4.70
   (LIDR2), 11.0 (RDHL). Initial stop ÷ pre-entry 1-min TR: 0.27 (CANF), 0.30 (AUUD), 0.72 (LIDR1), 0.92
   (JLHL), 0.94 (SSM), 1.09 (UPC), 1.17 (LIDR2), 1.46 (GYGY), 2.24 (WETO), 2.75 (RDHL). Six of eight
   stop-driven trades had a stop shorter than one pre-entry 1-min bar.
2. **Breaches persist.** Median 46 of 60 post-breach seconds with bid ≤ stop; median dip 1.8 % below the
   stop within 60 s. The ≥1 s two-read confirm and the L2 chop-hold are not the binding constraint — the
   only single-tick-shaped breach (JLHL, 9 s, 0.24 % dip) was also the one the sim shows as a 1-second dip
   the runner sampled late.
3. **Recovery is real but not capturable by a stop change alone.** 5/8 recovered above entry within
   30 min, yet in the sim the #1277 burst rule exits the bounce at −1 % to −3 % vs entry in 4 of the 5
   "avoided" trades (it arms off the flush low, not off entry). The wider stop converts −2 to −3R into
   −0.3 to −0.5R; it does not produce the +2R the tape offered later (CANF 4.70 = +8 % at t+15 min, LIDR1
   +9.7 %).
4. **Sizing dominates the dollar result.** At constant $ risk, a TR-scale stop shrinks AUUD 551→163/108 sh,
   CANF 355→94/63 sh, UPC 314→228, JLHL 149→136/90. Two-thirds of the F2 gain comes from that, exactly the
   #1278 mechanism the 09-02 replay A/B rejected (entry lost below min size on LIDR; SLE runner nicked).
   This dataset cannot see either failure mode (no winners, no min-size floor in the sim).
5. **Two constants set stops with no measurement behind them:** `avg * 0.995` in the viability tighten and
   the `0.003` floor in `stop_target_prices`. The tighten fired 3× in the window and was breached within
   0.1–4 s every time; in 2/3 (LIDR2 → −9 %, CDTG → −15 %) the viability call was right and the tighten
   saved money; in CANF it cost ~7 $ of extra slippage vs the initial stop and the trade recovered +8 %.
6. **Slippage past the stop** is 1.3–6.1 % on the four largest losses (CANF, AUUD, SDOT, SSM) with 7.7–18 s
   breach→fill; a floor does nothing about that.

## 6. Caveats (read before acting)

* n = 8 stop-driven exits, 3 of them on 09-02 premarket, 6 in premarket with an inert deadman. No winner
  in the sample: any floor's cost on winners (the #1278 failure) is invisible here.
* Simulation is per-second bid-low/bid-high from `iqfeed_trade_ticks` embedded quotes, not the runner's
  sampled read; it ignores fast-bail, lost-VWAP, L2 hold, trail ratchets, halts and the 2.0 s BBO ceiling,
  and prices burst exits at the bid-low of the exit second. Treat its $ as directional.
* "Never wider than the structural stop" was applied to tightens only; the initial-stop floors are
  unbounded because the ledger's structural fields are overwritten on recycle. The F2 widenings reach
  3.6 % (AUUD), 6.5–12.9 % (CANF) of entry, which is where risk-first sizing drops the position to 47–94 sh.
* The event-study population problem from memory applies: the sim enters where CHILI entered (spent-leg
  fills per the 09-01 forensics); replay A/B enters elsewhere.
* Pre-entry 1-min TR on a +30 % name (CANF 0.28 = 6.5 %) is the *flush* scale, not the *spread* scale — a
  floor keyed to it is a different lever (position size) than the one the gap statement describes.

## 7. Files

* `docs/STRATEGY/CC_REPORTS/2026-09-02_stop-noise-floor-forensics.csv` — 16 rows × 204 columns (all
  measures + every rule's stop/tighten/outcome/exit/qty/$ per trade).
* `scripts/research/stop_noise_forensics_0902.py` — `spread_units`, `one_minute_true_ranges`, `floor_stop`,
  `latency_fill`, `simulate_with_stop` (pure) + file-only CLI.
* `tests/test_stop_noise_forensics_0902.py` — 12 DB-free tests incl. AST guard.
