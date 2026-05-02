# NEXT_TASK: autopilot-trades-history

STATUS: DONE

## Goal

Add a "Trades History" section to the autopilot page so the operator can see past fast-path round trips (closed paper trades) the momentum scalper has executed. After this task:

1. **The autopilot page shows closed round trips** alongside the existing open-positions table — same `_autopilot_fast_path.html` partial, new section below the open positions.
2. **Realized P/L stats are on the page** — total realized P/L, win rate, win/loss count, average return, average holding time. At-a-glance answer to "is the strategy actually working?"
3. **Native vs. inherited toggle.** Default view shows F5-native trades only (using the `fast_exits_native` view); a toggle lets the operator include the 11 inherited bootstrap positions for completeness.
4. **Auto-refresh on the same 5s cadence as the existing sections** (consistent UX; no need to reinvent the polling pattern).

This is operator-tooling, not strategy work. Pure read-only UI on top of `fast_exits` + `fast_exits_native`.

## Why now

After F5 cleanup, we have the data: 3 native round trips closed, 5 floating-green positions still open, and 11 inherited bootstrap positions whose exits accumulate in the same table over time. The operator currently has no UI surface for any of this — they have to run SQL via dispatch to see it. F6 (signal half-life mining) is going to take the longer development cycle; in the meantime, having a real-time trades view lets the operator watch the strategy actually work and gives both Cowork and the operator a tighter feedback loop on whether the realized data is trending toward "edge" or "noise."

This is also a small, well-scoped task that exercises the existing autopilot UI patterns without introducing any new infrastructure.

## Scope — three subtasks, ordered

### 1. New API endpoints in `fast_path_api.py`

Add two endpoints under the existing `/api/trading/fast-path/` prefix:

**`GET /api/trading/fast-path/closed-trades`** — recent fast_exits rows.

Query parameters:
- `limit` (default 50, max 200)
- `include_inherited` (default `false`) — when `false`, queries from `fast_exits_native`; when `true`, queries from `fast_exits` directly.

Response shape:
```json
{
  "trades": [
    {
      "id": 123,
      "entry_execution_id": 56,
      "ticker": "DOGE-USD",
      "alert_type": "imbalance_long",
      "side": "buy",
      "quantity": 228.54,
      "entry_price": 0.10939,
      "exit_price": 0.108990,
      "exit_reason": "stop_hit",
      "realized_pnl_usd": -0.0914,
      "realized_return_pct": -0.366,
      "holding_period_s": 2621,
      "stop_at_entry": 0.108991,
      "target_at_entry": 0.110297,
      "entered_at": "2026-05-01T21:32:23.161",
      "exited_at": "2026-05-01T22:46:03.697",
      "mode": "paper",
      "is_native": true
    },
    ...
  ],
  "as_of": "2026-05-02T00:35:00"
}
```

The `is_native` field is present whether you query the view or the base table — for the native view it's always true; for the all-inclusive query, derive it via the bracket-age trick (`(brain_json->>'computed_at')::timestamp - entered_at < INTERVAL '60 seconds'`) so the UI can color-code which rows are inherited.

You'll need the `alert_type` from `fast_executions` (it's not on `fast_exits` itself) — JOIN on `entry_execution_id`. Keep the JOIN cheap (LIMIT first, then JOIN, with the index on `(entry_execution_id, exited_at)` doing the work).

**`GET /api/trading/fast-path/realized-stats`** — aggregate across closed trades.

Query parameters:
- `include_inherited` (default `false`)
- `since_hours` (default 24) — rolling window, default 24 hours

Response shape:
```json
{
  "round_trips": 3,
  "wins": 0,
  "losses": 3,
  "win_rate_pct": 0.00,
  "total_pnl_usd": -0.2743,
  "avg_return_pct": -0.366,
  "avg_holding_s": 2621,
  "best_trade_pnl_usd": -0.0892,
  "worst_trade_pnl_usd": -0.0936,
  "by_reason": {
    "stop_hit": {"count": 3, "total_pnl_usd": -0.2743},
    "target_hit": {"count": 0, "total_pnl_usd": 0.0},
    "time_stop": {"count": 0, "total_pnl_usd": 0.0}
  },
  "by_ticker": {
    "DOGE-USD": {"count": 3, "total_pnl_usd": -0.2743}
  },
  "since_hours": 24,
  "include_inherited": false,
  "as_of": "2026-05-02T00:35:00"
}
```

`by_reason` always has the three keys (`stop_hit`, `target_hit`, `time_stop`) even when zero — the UI doesn't have to handle missing keys.

Both endpoints follow the same patterns as the existing `paper-trades` / `recent-decisions` / `summary` endpoints — read-only, two queries max each, connect+execute+close pattern that's already in the file.

### 2. Update `_autopilot_fast_path.html` partial

Add a third section below the existing open-positions table and decisions feed: "Closed Round Trips."

Section structure:
- **Header bar** with title, "F5-native only" toggle (defaults ON), as-of timestamp.
- **Stats row** of cards (mirroring the existing summary row pattern): round trips, win rate, total realized P/L, avg return, avg holding time. Color-code P/L green/red, win rate above/below 50% green/red.
- **Trades table** with columns: time exited, ticker, alert type, side, qty, entry, exit, P/L $, P/L %, hold time, exit reason. Color-code P/L cells. Show `is_native: false` rows with a subtle visual cue (e.g., italicized ticker + an `(inherited)` suffix).
- **Toggle behavior** — flipping "include inherited" re-fetches both endpoints with `include_inherited=true` and re-renders.
- **Auto-refresh every 5s** matching the existing sections.

Keep the styling scoped under `#ap-fast-path-section .ap-fp-history-*` so it doesn't collide with anything. Reuse `.ap-fp-card`, `.ap-fp-table`, `.ap-fp-pnl-pos`, `.ap-fp-pnl-neg` classes that already exist in the partial.

Empty state: when no closed trades exist (which won't be the case on this repo, but matters for fresh deploys), show "No closed round trips yet — strategy will populate as positions exit." in the table empty cell.

### 3. Smoke test on the live system

After deploy:
1. Restart `chili` (the web container — fast-data-worker doesn't serve UI). The web container reads `fast_path_api.py`.
2. Browse to `https://localhost:8000/trading/autopilot` (operator-side; you can't actually open a browser, but verify the partial renders by curl/python-fetch and grep for the new section's hooks like `ap-fp-history-tbody`).
3. Hit both new endpoints directly (via the existing dispatch pattern from inside `chili`):
   - `/api/trading/fast-path/closed-trades?limit=10` — should return 3 native trades by default.
   - `/api/trading/fast-path/closed-trades?limit=20&include_inherited=true` — should return more (the 11 inherited bootstraps will populate as exit_manager closes them).
   - `/api/trading/fast-path/realized-stats` — verify the aggregate matches what the verbatim SQL from the F5-cleanup CC report produces (`COUNT=3, total_pnl_usd=-0.18, win_rate=0`).

## Brain integration (reuse, don't rewrite)

- `app/routers/trading_sub/fast_path_api.py` — extend in place. Don't create a new router file.
- `app/templates/trading/_autopilot_fast_path.html` — extend in place. Don't create a new partial.
- `fast_exits_native` view — already created in migration 219. Use it as the default query source.
- `app/routers/trading_sub/_utils.py` if a `json_safe` helper exists there — use it; otherwise the existing endpoints' inline approach (`.isoformat()` on datetimes) is fine.

## Constraints / do not touch

- **Live-placement safety belts.** Same as always.
- **Strategy thresholds.** No tuning of any constant.
- **Bracket policy.** F6 will derive these.
- **The existing UI sections.** Don't refactor open-positions or recent-decisions tables. Add the new section, leave the others alone.
- **DB schema.** No migrations. The `is_native` field is computed at query time, not stored.
- **The Coinbase live path.** Read-only UI surface; doesn't touch placement.

## Out of scope

- F6 (signal half-life mining) — separate next task.
- A "manage trades" view (close, modify, etc.) — read-only.
- Per-trade clickthrough to a detail page — text table only.
- WebSocket / SSE push — keep the 5s polling pattern of the existing UI.
- Any new visualization libraries (Chart.js, Plotly, etc.). If a chart is desired, leave it for a separate task.
- Filtering by ticker / alert type — operator can sort the table mentally for now.
- Pagination beyond `limit` — top 50 is enough for an autopilot view.

## Success criteria

1. `git log --oneline -3` shows a new commit containing both the API endpoint additions and the UI partial changes (single commit since they're a coherent feature).
2. `curl -k -s https://localhost:8000/api/trading/fast-path/closed-trades?limit=5` (or the python-urllib equivalent from inside the chili container) returns valid JSON with `trades` array containing the 3 currently-closed native trades.
3. `curl -k -s https://localhost:8000/api/trading/fast-path/realized-stats` returns aggregate matching the F5-cleanup SQL benchmark: 3 round trips, 0 wins, 3 losses, total_pnl_usd ≈ -0.18, win_rate_pct = 0.
4. Fetching `https://localhost:8000/trading/autopilot` (text body via python-urllib) contains the strings `ap-fp-history-section`, `ap-fp-history-tbody`, and `Closed Round Trips`.
5. `docs/STRATEGY/CC_REPORTS/2026-05-02_autopilot-trades-history.md` written with: what shipped, screenshot-equivalent (a sample row from each endpoint pasted as JSON), any deferrals, Open Questions.

## Open questions for Cowork (surface in your report only if relevant)

- The 24h `since_hours` default — is that the right rolling window for "realized stats"? My logic: scalp sessions tend to span hours, not days, so 24h captures both intraday and overnight context. If you find data showing a different window makes more sense (e.g., 4h or 12h), propose it.
- Should the trades table sort newest-first (default) or have a column-sort UI? My vote: default newest-first only, keep the table simple. Column-sort is feature creep for a v1.
- Color cues for inherited rows — I suggested italics + `(inherited)` suffix. If you have a stronger UX instinct (e.g., gray text, a small tag chip), follow it. Just keep them visibly distinct from native rows so the operator never confuses them in P/L analysis.

## Rollback plan

- API endpoints are read-only and additive — revert removes them, no data migration needed.
- UI partial change is additive — revert removes the section, no JS dependencies left dangling.
- Single commit means single revert.
