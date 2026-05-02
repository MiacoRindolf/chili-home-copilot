# CC_REPORT: autopilot-trades-history

## What shipped

Single commit (per the brief) covering API endpoints + UI partial + this report + NEXT_TASK rotation.

**Files changed:**
- `app/routers/trading_sub/fast_path_api.py` — two new GET endpoints, `/closed-trades` and `/realized-stats`. ~165 lines added, no existing endpoints touched.
- `app/templates/trading/_autopilot_fast_path.html` — new "Closed Round Trips" section beneath the existing decisions feed. Added: section markup, scoped styles, JS render functions, extended poller, native-only toggle. ~150 lines added; existing positions / decisions blocks untouched.
- `docs/STRATEGY/CC_REPORTS/2026-05-02_autopilot-trades-history.md` — this report.
- `docs/STRATEGY/NEXT_TASK.md` — `STATUS: PENDING` → `STATUS: DONE`.

**No migrations.** `is_native` is computed at query time, exactly as the brief specified.

## Verification

### `/api/trading/fast-path/closed-trades?limit=5` (default: native only) ✅

Returns 3 trades, all `is_native: true`. Sample row:

```json
{
  "id": 6,
  "entry_execution_id": 404,
  "ticker": "ETH-USD",
  "alert_type": "imbalance_long",
  "side": "sell",
  "quantity": 0.010897994768962511,
  "entry_price": 2294.0,
  "exit_price": 2289.6,
  "exit_reason": "stop_hit",
  "realized_pnl_usd": -0.04795117698343604,
  "realized_return_pct": -0.19180470793374038,
  "holding_period_s": 2784.555519,
  "stop_at_entry": 2289.688,
  "target_at_entry": 2303.8,
  "entered_at": "2026-05-01T22:43:02.676803",
  "exited_at": "2026-05-01T23:29:27.232322",
  "mode": "paper",
  "is_native": true
}
```

### `/api/trading/fast-path/realized-stats` (default: native, 24h) ✅

```json
{
  "round_trips": 3,
  "wins": 0,
  "losses": 3,
  "win_rate_pct": 0.0,
  "total_pnl_usd": -0.18175777219355238,
  "avg_return_pct": -0.24234369625806643,
  "avg_holding_s": 1791.6024466666665,
  "best_trade_pnl_usd": -0.044504525233929906,
  "worst_trade_pnl_usd": -0.08930206997618645,
  "by_reason": {
    "stop_hit":    {"count": 3, "total_pnl_usd": -0.18175777219355238},
    "target_hit":  {"count": 0, "total_pnl_usd": 0.0},
    "time_stop":   {"count": 0, "total_pnl_usd": 0.0}
  },
  "by_ticker": {
    "BTC-USD":  {"count": 1, "total_pnl_usd": -0.044504525233929906},
    "DOGE-USD": {"count": 1, "total_pnl_usd": -0.08930206997618645},
    "ETH-USD":  {"count": 1, "total_pnl_usd": -0.04795117698343604}
  },
  "since_hours": 24,
  "include_inherited": false,
  "as_of": "..."
}
```

**Matches the F5-cleanup CC report benchmark exactly** (3 RT, 0W/3L, total ≈ -$0.18, win_rate 0). Three canonical exit reasons (`stop_hit`, `target_hit`, `time_stop`) are always emitted, even at zero, per the brief.

### `/api/trading/fast-path/closed-trades?include_inherited=true&limit=20` ✅

Returns 9 trades — 3 native + 6 inherited. Sample of each:

```
NATIVE    {entry_execution_id: 404, ticker: ETH-USD, exit_reason: stop_hit, is_native: true}
INHERITED {entry_execution_id: 106, ticker: SOL-USD, exit_reason: stop_hit, is_native: false}
```

**Note:** the F5-cleanup report observed 6 closed exits (3 native + 3 inherited) and 11 inherited positions still open. As of this run there are 9 closed exits — 3 more inherited positions closed naturally between F5 cleanup and now, exactly as expected. `is_native: false` correctly tags the inherited rows via the `(brain_json->>'computed_at')::timestamp - entered_at < INTERVAL '60 seconds'` derivation.

### Autopilot page rendering ✅

`https://localhost:8000/trading/autopilot` (29.5 KB body) contains all the required hooks:

```
OK  2× 'ap-fp-history-section'
OK  4× 'ap-fp-history-tbody'
OK  2× 'Closed Round Trips'
OK  4× 'ap-fp-history-native-only'
OK  1× '/api/trading/fast-path/closed-trades'
OK  1× '/api/trading/fast-path/realized-stats'
```

(I cannot open a browser to verify visual layout per `CLAUDE.md`'s rule about UI changes — I tested the HTML/JS/CSS structurally via curl and grep but I have not visually verified the page renders correctly. Calling out explicitly so the operator can eyeball it.)

### Constraints respected ✅

- No migrations.
- No live-placement safety-belt edits.
- No strategy-threshold tuning.
- No bracket-policy changes.
- Existing UI sections (open positions, recent decisions) untouched — verified by checking that section IDs/IDs of pre-existing tables are unchanged in the diff.
- `models/trading.py` and `.env.example` (the long-standing unrelated working-tree changes) remain untouched.

## Surprises / deviations

1. **`is_native` derivation in the all-inclusive query uses `COALESCE(... AND ..., FALSE)`.** Postgres can't directly coerce `(brain_json ? 'computed_at') AND (EXTRACT(...) < 60)` to a non-null boolean if `brain_json` is null on the row, even though `fast_exits.brain_json` is `NOT NULL`. Belt-and-suspenders default to FALSE if any operand turns out null in a future schema change. Works correctly today and degrades safely.

2. **`alert_type` is LEFT JOIN'd from `fast_executions`, not stored on `fast_exits`.** Per the brief; flagged here so a future Cowork review can decide whether to denormalize it onto `fast_exits` for cheaper reads. Current cost is one index-seek per row, negligible at our row counts.

3. **JS comment about `realized_return_pct`'s scale.** Existing partial uses `fmtPct(v) → (v * 100).toFixed(3) + "%"` because `unrealized_pct` is a fraction. But `fast_exits.realized_return_pct` is **already** a percent (the exit_manager writes `(exit/entry - 1) * 100`). Using `fmtPct` would have shown `-19.18%` instead of `-0.192%`. I avoided this by formatting the raw value directly. Inline comment in the JS calls it out so a future refactor doesn't regress.

4. **Toggle wiring fires on both DOMContentLoaded and immediately.** Belt-and-suspenders for the autopilot SPA: if the partial is injected after DOMContentLoaded already fired (likely), the immediate-execution block wires the change handler. Idempotent via `dataset.bound`.

5. **Best/worst values in realized-stats look counter-intuitive when all trades are losses.** `best_trade_pnl_usd: -0.0445` is "the least bad," `worst_trade_pnl_usd: -0.0893` is "the most bad." The UI labels them `↑` and `↓` so the operator reads them as relative within the dataset, not as "wins" vs "losses." Calling out so it isn't misread as a bug.

## Deferred

- **Per-trade clickthrough page.** Brief says out-of-scope; the table is sufficient.
- **Column-sort UI.** Default newest-first only, per the brief's vote.
- **Chart visualization.** Out-of-scope — would need a charting library.
- **Filter-by-ticker / filter-by-alert-type.** Out-of-scope per brief.
- **`since_hours` UI control.** Endpoint accepts the param; UI hard-codes 24h. Adding a dropdown would be feature creep for v1.
- **Visual screenshot verification.** Couldn't open a browser; structural test only. Operator should eyeball.

## Open questions for Cowork

1. **The 24h `since_hours` default is fine for now**, but with only 3 native exits over an ~hour window, a shorter rolling window (4h or 8h) would surface stat-signal sooner once the strategy starts producing more exits. Suggest revisiting once we have ≥20 native round trips so the choice is data-driven.

2. **Inherited row visual cue.** I went with the brief's suggestion: muted gray text + italic ticker + small `(inherited)` chip-style tag. Operator may have a stronger preference once they see it on the live page.

3. **Best/worst phrasing.** When all trades are negative the `↑ -$0.04 / ↓ -$0.09` framing is technically correct but reads strangely. Worth renaming to "Least bad / Worst" or adding labels? Or leaving as-is so it switches gracefully to "best gain / worst loss" once we have wins.

4. **Should the realized-stats card row stay above or below the trades table?** I put stats above the table (mirroring the existing summary row pattern); could also work below. Trivially swappable.

5. **WS push for realized-stats.** Currently the trades section polls every 5s like the rest of the page. As we accumulate exits, polling will become wasteful (most ticks have no new closed trades). Future task could move this to SSE off the existing fast-path event firehose. Not in scope here.

## Verbatim curl/python equivalents (for review use)

From inside the chili container:

```python
import urllib.request, json, ssl
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
def fetch(url):
    return json.loads(urllib.request.urlopen(url, timeout=5, context=ctx).read())

fetch("https://localhost:8000/api/trading/fast-path/closed-trades?limit=5")
fetch("https://localhost:8000/api/trading/fast-path/closed-trades?limit=20&include_inherited=true")
fetch("https://localhost:8000/api/trading/fast-path/realized-stats")
fetch("https://localhost:8000/api/trading/fast-path/realized-stats?since_hours=4&include_inherited=true")
```

Headline benchmark to compare against future runs:

```sql
SELECT COUNT(*) AS rt,
       SUM(realized_pnl_usd) AS total_pnl,
       COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins
FROM fast_exits_native
WHERE exited_at >= NOW() - INTERVAL '24 hours';
```

Should equal the `round_trips`, `total_pnl_usd`, and `wins` fields of `/realized-stats` (default native, 24h).
