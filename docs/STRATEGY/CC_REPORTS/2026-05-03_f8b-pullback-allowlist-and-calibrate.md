# CC_REPORT: f8b-pullback-allowlist-and-calibrate

## What shipped

1 commit, pushed to `origin/main`. The brief authorized up to 3 commits (gate, script, wire-up); I bundled them because they're one logical feature — reverting any one breaks the others.

| SHA | Subject | Files | LOC |
|---|---|---|---|
| `15e142e` | `feat(fast-path): F8b ticker allowlist + counterfactual delay calibration` | gates.py, scanner.py, scripts/calibrate-pullback-delay.py, app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json | +534 / −8 |

## Major finding — surfaced in Open Q4 territory ⚠

The brief anticipated this case explicitly: "If counterfactual optimization shows that even the optimum has negative or near-zero realized-equivalent return, that means {BTC, SOL} aren't actually a positive subset — the realized P/L pattern was noise at low n."

**That is the case for BTC.**

| Ticker | F8a-rerun-2 actual P/L (n=8 / n=13) | F8b counterfactual best (n=69 / n=43) |
|---|---|---|
| **BTC-USD** | **+5.66 bps** | **−0.75 bps** at delay=5s (boundary) |
| SOL-USD | +3.34 bps | +3.47 bps at delay=25s ✓ |

**SOL's edge replicates** in the counterfactual on a 5x larger sample. **BTC's does not.** Across all 10 candidate delays, BTC counterfactual mean ranges −0.75 bps to −5.03 bps — every value is negative. The +5.66 bps from F8a-rerun-2's n=8 actual BTC exits was noise.

This was Open Question 4 in the brief. Surfaced explicitly here for Cowork's strategic call.

## Subtask 1 — allowlist gate ✅

`gates.py:gate_pullback_ticker_allowed` rejects `volume_breakout_pullback_long` for tickers not in `PULLBACK_LONG_ALLOWLIST = {BTC-USD, SOL-USD}`. Reject reason carries the blocked ticker (`pullback_ticker_not_allowed:ETH-USD` etc.) so the postmortem is self-documenting.

Wired into `DEFAULT_GATES` AFTER the calibrated-edge gates (`gate_negative_edge_excluded`, `gate_calibrated_tradeability`) and BEFORE the price-sanity / capacity / budget gates. The ordering means a calibrated negative-edge or not-tradeable verdict still reports as the primary reject reason when it fires; the allowlist filter only becomes primary when the calibrated gates pass through (insufficient data or tradeable).

### Verification

15-min post-deploy soak window:

```
ticker   | attempts | paper_fills | rejected | distinct_reasons | sample_reason
AVAX-USD |        7 |           0 |        7 |                2 | min_score:score_below_threshold
BTC-USD  |        8 |           1 |        7 |                2 | capacity:pair_already_held
DOGE-USD |        5 |           0 |        5 |                3 | min_score:score_below_threshold
ETH-USD  |        8 |           0 |        8 |                1 | pullback_ticker:pullback_ticker_not_allowed:ETH-USD
SOL-USD  |        6 |           1 |        5 |                2 | capacity:pair_already_held
```

`pullback_ticker_not_allowed` reject reasons in the 30-min window:

```
ticker   | reject_reason                                            | n
ETH-USD  | pullback_ticker:pullback_ticker_not_allowed:ETH-USD      | 8
AVAX-USD | pullback_ticker:pullback_ticker_not_allowed:AVAX-USD     | 6
DOGE-USD | pullback_ticker:pullback_ticker_not_allowed:DOGE-USD     | 1
```

ETH/AVAX/DOGE blocked. BTC/SOL produce paper fills. Some AVAX/DOGE alerts fail `min_score` first (gate ordering puts min_score earlier) — this is correct: when a signal also fails min_score, that's reported as primary. The allowlist gate still runs (results land in `gates_json`), just isn't primary.

## Subtask 2 — counterfactual delay calibration script ✅

`scripts/calibrate-pullback-delay.py` (~280 LOC). For each allowlisted ticker over a configurable history window (default 14 days):

1. Fetch all `volume_breakout_pullback_long` alerts.
2. Fetch the actual closed-pullback hold-period distribution for that ticker.
3. For each alert × each candidate delay in `[5,10,15,20,25,30,45,60,90,120]` seconds:
   - Look up `best_ask` at `fired_at + delay` → hypothetical entry.
   - Sample a hold period from the empirical distribution (seeded RNG=42 for reproducibility).
   - Look up `best_bid` at `fired_at + delay + sampled_hold` → hypothetical exit.
   - `realized_equivalent_return = (exit_bid - entry_ask) / entry_ask`.
4. Rank candidates by `mean_return × n/(n+30)` (shrinkage damps high-mean-low-n cells; the constant 30 mirrors `MIN_SAMPLES_FOR_CALIB` in the calibration helpers).
5. Require min n=10 per delay/ticker; otherwise fall back to best-of-thin with a warning.
6. Boundary guard: if optimum is at `[5,...,120]` boundary, surface for re-run with expanded grid.

Output: `app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json` (committed).

### Per-ticker sweep tables

**BTC-USD** (boundary warning at lower edge):

```
delay_s    n   mean_bps   shrunk
      5   69     -0.75   -0.0001
     10   67     -0.80   -0.0001
     15   69     -1.70   -0.0001
     20   66     -1.16   -0.0001
     25   68     -1.27   -0.0001
     30   67     -1.01   -0.0001
     45   69     -2.20   -0.0002
     60   68     -2.09   -0.0001
     90   69     -5.03   -0.0004
    120   69     -1.61   -0.0001
```

**Every** delay produces negative mean. BTC counterfactual REFUTES the F8a-rerun-2 +5.66 bps result. Boundary warning exists but expanding the search downward (1s, 2s) is unlikely to cross zero given the consistent negativity across 10 candidates.

**SOL-USD**:

```
delay_s    n   mean_bps   shrunk
      5   42     -0.18   -0.0000
     10   42     +1.04   +0.0001
     15   42     -0.54   -0.0000
     20   44     +3.03   +0.0002
     25   43     +3.47   +0.0002  ← optimum
     30   41     -2.45   -0.0001
     45   43     +1.41   +0.0001
     60   41     -1.17   -0.0001
     90   44     +3.31   +0.0002
    120   42     +1.96   +0.0001
```

Multi-modal positive distribution: 25s=+3.47, 90s=+3.31 are essentially tied (within shrinkage noise); 20s=+3.03 also strong. SOL's edge is real but noisy — the optimum is meaningful, but the 30s default produced −2.45 bps so the calibrated 25s is materially better than the prior code default.

## Subtask 3 — calibration artifact wire-up ✅

`scanner.py` got:

- `_load_pullback_delay_calibration()`: reads JSON at lazy-import time, returns `dict[str, float]`.
- `get_pullback_delay_s(ticker)`: per-ticker lookup with fallback to `VOL_BREAKOUT_PULLBACK_DELAY_S = 30.0` for tickers not in the artifact.
- `_schedule_pullback_deferred` substituted: `delay_s = get_pullback_delay_s(ticker)` instead of the constant.

Live verification post-restart:

```
scanner: pullback delay loaded: BTC-USD=5s SOL-USD=25s
         (artifact=/app/app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json)
```

## Surprises / deviations

1. **BTC counterfactual is uniformly negative** despite F8a-rerun-2's positive realized P/L on n=8. **The brief's Open Question 4 case is real for BTC.** This is the most important finding; it changes F8b's strategic value. The allowlist still works (BTC gets fills); the calibration produced a defensible-by-data 5s delay; but the underlying signal isn't actually positive on BTC over the broader sample.

2. **The artifact path resolution.** Initial run wrote the JSON inside the container's writable layer (`/app/services/...`) instead of the host-bound `/app/app/services/...`. Fixed the script to use `app.__file__` to locate the package, which works correctly whether the script is run from inside the container or from the host. The committed artifact was copied via `docker cp` to the host bind mount; subsequent runs will write to the right path directly.

3. **Bundled all 3 subtasks into one commit.** Brief authorized up to 3 commits; I chose 1 because the gate, script, and wire-up are interdependent — partial revert would break the system. Standard "one logical feature = one commit" judgment.

4. **The script had to be run via `docker cp` + `/tmp/` invocation** because Git Bash's path translation (`C:/Program Files/Git/...`) corrupts `/app/scripts/` arguments to `docker compose exec`. PowerShell + `docker cp` worked; documented for future scripted invocations.

5. **The boundary warning for BTC=5s** triggered the script's defensive log line but I didn't expand the search. The counterfactual is uniformly negative across the full grid — expanding to 1s/2s wouldn't change the strategic picture (BTC negative either way). If Cowork wants the data anyway, re-running with `--candidates 1,2,3,4,5,7,10,...` is a one-liner.

## Constraints respected

- ✅ Default mode stays paper. No live-placement enable.
- ✅ No threshold tuning beyond the calibrated DELAY_S.
- ✅ No producer-side change to `fast_alerts`.
- ✅ No miner code changes.
- ✅ No migrations.
- ✅ Allowlist hard-coded in code (not parameterized via config) per brief.
- ✅ `models/trading.py`, `.env.example` untouched.

## Open questions for Cowork

1. **BTC's counterfactual refutes its actual-trade edge.** Three options:
   - **A.** Drop BTC from the allowlist (`PULLBACK_LONG_ALLOWLIST = {SOL-USD}`). Cleanest from the data; loses the positive-realized signal on n=8 actual exits.
   - **B.** Keep BTC in the allowlist; let realized P/L over the next 24h tie-break. If actual-trade BTC remains positive over n=20+, the counterfactual is wrong; if it drifts negative, drop.
   - **C.** Pivot to F9 (signal redesign). The fade hypothesis isn't holding up across the broader sample, even on the previously-supported subset.

   My read: **B** is honest and minimal-action. Continued soak data resolves the disagreement. F9 is the right move if BTC also drifts negative.

2. **SOL's optimum is multi-modal** (25s=+3.47, 90s=+3.31, 20s=+3.03). Picking 25s is defensible but slightly arbitrary. Worth a stability check on a longer history window (28 days when we have it) to see if the optimum is stable. Not blocking.

3. **Counterfactual hold-period sampling assumes the hold distribution is independent of entry timing.** If short delays produce different exit timings than long delays, that's a confounder. Could fix by: (a) using actual hold periods only from trades whose entry timing matches the candidate delay (impossible without n=many), OR (b) modeling hold as a function of delay. Out of scope for this calibration; flag for future statistical pass.

4. **The shrinkage constant (n/(n+30))** mirrors MIN_SAMPLES_FOR_CALIB. Defensible and documented inline. If Cowork prefers a different shrinkage schedule, easy to change in one line.

5. **Re-calibration cadence.** The script is manual-invocation. Could be scheduled (cron / weekly) but the brief said "manual re-run is fine for now." Surfacing because if F8b's verification soak lands, we'll want to recalibrate as more data accumulates.

## Recommendation for next NEXT_TASK

**Two paths, depending on Cowork's read of the BTC counterfactual:**

### Path A — Verify with 24h soak before deciding

One-line for the next brief: *Re-evaluate F8b after 24h of post-deploy soak: distinct realized exits per allowlisted ticker, aggregate vs per-ticker P/L, compare to F8a-rerun-2 baseline. If BTC drifts negative in actual trades too, the counterfactual was correct and F9 is next.*

### Path B — Pivot to F9 now

One-line: *Design and prototype a new fast-path signal class that is NOT the volume-breakout-pullback fade. Candidates from prior briefs: order-flow imbalance, microstructure squeeze with maker-rebate, cross-pair lead-lag. Operator design input required.*

My vote: **Path A.** A 24h soak is cheap and either confirms the counterfactual (drop BTC, keep SOL, continue paper-soaking F8b) or refutes it (BTC actual P/L stays positive → counterfactual is missing something → investigate before F9). F9 is structurally bigger and worth the day to confirm we're not abandoning a real signal prematurely.

## Verbatim verification SQL — for next review

```sql
-- 1. Allowlist gate efficacy (per-ticker fill counts)
SELECT
  e.ticker, COUNT(*) AS attempts,
  COUNT(*) FILTER (WHERE e.decision='paper_fill') AS paper_fills,
  COUNT(*) FILTER (WHERE e.decision='rejected') AS rejected
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > NOW() - INTERVAL '24 hours'
GROUP BY e.ticker ORDER BY e.ticker;

-- 2. pullback_ticker reject distribution
SELECT e.ticker, e.reject_reason, COUNT(*) AS n
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.reject_reason LIKE 'pullback_ticker%'
  AND e.decided_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 2 ORDER BY n DESC;

-- 3. Distinct realized P/L on the new (post-allowlist) cohort
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
    AND e.decided_at > '<deploy timestamp>'
)
SELECT e.ticker, COUNT(*) AS exits,
       ROUND(SUM(x.realized_pnl_usd)::numeric, 4) AS pnl,
       ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
              / COUNT(*))::numeric, 1) AS win_rate_pct,
       ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
GROUP BY e.ticker ORDER BY exits DESC;
```

```bash
# Re-run calibration on demand
docker cp scripts/calibrate-pullback-delay.py chili-home-copilot-chili-1:/tmp/
docker compose exec -T chili sh -c 'PYTHONPATH=/app python /tmp/calibrate-pullback-delay.py'

# Read the artifact
cat app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json
```

## What's next (per operator's queue)

- **Path A (recommended): F8b verification soak (24h)** — surfaced in this report's recommendation.
- **f-hygiene-5** — structural B fix from f-hygiene-4. Can run in parallel.
- **f-leak-3** — still conditional on next OOM event.
- **F9** — if Path A's 24h soak confirms BTC drifted negative.

F8a soak resumed on `fast-data-worker`. ~30s interruption for the restart; capture rate stays at 100%. `models/trading.py` and `.env.example` remain untouched.
