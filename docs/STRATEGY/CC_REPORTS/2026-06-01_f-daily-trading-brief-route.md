# CC_REPORT: f-daily-trading-brief-route

**Type:** operator-directed, out-of-band ("continue, keep the pace", 2026-06-01;
commit→push→PR→merge per change). Wires U1's `build_brief` onto live trading
read-models. `NEXT_TASK.md` (phase-5i soak) untouched.

## What shipped

- **`app/services/trading_summary.py`** (new) —
  `build_trading_summary(db, user_id, window_hours=24) -> dict` shaped for
  `build_brief`. **Read-only, ORM-only, defensive:**
  - Queries the `Trade` ORM (maps through the compat view, so it survives the
    position-identity rename) for closed trades in the window + open trades;
    resolves `scan_pattern_id` → `ScanPattern.name`.
  - Computes net realized P/L, win rate, per-pattern realized P/L (top 5),
    closes list, open-position tickers/sides.
  - No raw SQL / hardcoded table names; no broker or live-quote calls (open-
    position unrealized P/L is intentionally omitted); every query wrapped so a
    failure degrades that section to empty rather than 500-ing the report.

- **`GET /api/brain/trading/brief`** (new, `app/routers/brain.py`) — renders the
  brief as self-contained HTML (`category="brief"` theme). Guests get an empty
  brief; `?download=1` → attachment; `window_hours` validated `ge=1, le=720`
  (out-of-range → 422, not 500).

- **W1 research digest themed** — `GET /api/brain/reasoning/research/report` now
  passes `category="research"` (U2 theming on a live consumer).

## Verification

- `tests/test_trading_summary.py` (9 cases, fast, query-helpers patched):
  net-P/L + win-rate aggregation, pattern-name resolution + top-pattern sort +
  id-fallback, open positions without unrealized, null-pnl excluded from
  aggregates but still listed, top-5 cap, short-window date omission, no-user →
  {}. All 9 pass.
- `tests/test_trading_brief_route.py` (4 cases, full-app boot): guest → empty
  brief; paired user with a seeded closed AAPL trade → hero title preserved +
  Performance section + AAPL + "+$10.00"; download attachment header; bad
  `window_hours` → 422. <RESULT FILLED ON GREEN>

## Surprises / deviations

- None. `build_trading_summary` was structured with thin query helpers so its
  aggregation logic unit-tests fast (DB-free), keeping the slow full-boot path to
  a single route test.

## Deferred

- Open-position unrealized P/L (needs live quotes — out of scope for a read-only,
  never-hangs report endpoint).
- A scheduled daily-brief artifact / Telegram push — future hook.
- Per-pattern payoff ratio in the brief (available on `ScanPattern.payoff_ratio`
  but per-user/per-window payoff is noisier; left out for now).

## Open questions for Cowork

1. Surface `/api/brain/trading/brief` in the Brain UI, and/or schedule a daily
   artifact?
2. Want the brief to include a per-pattern payoff column from
   `ScanPattern.payoff_ratio`?
