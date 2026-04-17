# AutoTrader v1 — Operator guide

AutoTrader v1 is the automation path for pattern-imminent BreakoutAlerts → paper
or Robinhood live equities. It is orchestrated by two APScheduler jobs and
surfaced on `/trading/autopilot` (CHILI pattern desk).

## Safety posture (defaults)

| Flag | Default | Effect |
|---|---|---|
| `CHILI_AUTOTRADER_ENABLED` | **false** | Master switch. When `false`, both the entry tick and the monitor no-op. |
| `CHILI_AUTOTRADER_LIVE_ENABLED` | **false** | Env default for paper vs Robinhood. Desk override wins. |
| `CHILI_AUTOTRADER_RTH_ONLY` | `true` | Monitor only runs during US equity regular hours. |
| `CHILI_AUTOTRADER_DAILY_LOSS_CAP_USD` | `150.0` | Trips the global kill switch when reached. |
| `CHILI_AUTOTRADER_PER_TRADE_NOTIONAL_USD` | `300.0` | Base position size. |
| `CHILI_AUTOTRADER_SYNERGY_SCALE_NOTIONAL_USD` | `150.0` | Scale-in add size (live only, once per trade). |

Live entries and live exits are **only** placed when:

1. `CHILI_AUTOTRADER_ENABLED=true` on the server (tick + monitor registered).
2. Desk is **not paused** (for entries — monitor always runs for open rows).
3. Kill switch is **off** (short-circuits both paths before any broker call).
4. Effective live mode is on (desk override OR env default).
5. The Robinhood adapter is `is_enabled()` (requires `CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED=true`
   and a connected broker session).

## Desk-only live flow

The recommended rollout keeps env flags off and controls live per-session from
the UI:

1. Server env: `CHILI_AUTOTRADER_ENABLED=true` (scheduler tick/monitor run).
2. Server env: `CHILI_AUTOTRADER_LIVE_ENABLED=false` (paper default).
3. On `/trading/autopilot`, click **Run / Resume** → `paused=false`.
4. Tick the **Robinhood live orders** checkbox → desk sets `live_orders=true`.
5. Desk override is reflected immediately by `effective_autotrader_runtime`;
   the next scheduler tick places via the Robinhood adapter.
6. Untick the checkbox to fall back to env default (no live orders when
   `CHILI_AUTOTRADER_LIVE_ENABLED=false`).

State is persisted in `trading_brain_runtime_modes.slice_name=autotrader_v1_desk`.

## Kill switch precedence

`is_kill_switch_active()` is checked at the top of **both**
`run_auto_trader_tick` and `tick_auto_trader_monitor`. When active, both return
`{"ok": True, "skipped": ..., "reason": "kill_switch"}` with zero broker
interaction, regardless of desk pause state or live_orders flag.

The monitor trips the kill switch itself if realized PnL today
(ET calendar) ≤ `-CHILI_AUTOTRADER_DAILY_LOSS_CAP_USD` — separate thresholds
exist for paper (trips only when live is off) and live.

## PDT soft-warn

AutoTrader does **not** block same-day round trips. Instead, every
auto-exit or desk "Close now" writes an `AutoTraderRun` audit row with:

```json
{
  "exit_reason": "stop|target|desk_close_now",
  "pnl": 12.34,
  "opened_today_et": true,
  "would_be_day_trade": true
}
```

`would_be_day_trade` is true only for same-ET-day **long stock** exits
(crypto excluded). The desk also exposes `opened_today_et` per row so the UI
renders a yellow "opened today (PDT)" pill. If the account is PDT-restricted,
Robinhood itself rejects the order — the monitor logs the error and leaves
the position open.

## Per-position controls (AutoTrader v1 only)

Per-row buttons on the desk persist to
`trading_brain_runtime_modes.slice_name=autotrader_v1_position:{kind}:{trade_id}`
with `payload_json = {monitor_paused, synergy_excluded, kind}`.

- **Pause monitor** — holds the row past its stop/target; monitor skips it
  until resumed. Reported as `live_monitor_paused_ids` /
  `paper_monitor_paused_ids` in the monitor summary.
- **Resume monitor** — clears `monitor_paused`.
- **Exclude synergy** — blocks scale-ins (`maybe_scale_in` returns `None`).
- **Allow synergy** — clears the exclude flag.
- **Close now** — market-sell via Robinhood for live v1 rows; for paper v1
  rows, closes at the current quote with slippage. Always writes an audit
  row with PDT snapshot. Requires `confirm=true`.

Override rows are deleted automatically when a position closes (monitor
auto-exit, close-now, or manual exit).

## APIs

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/trading/autotrader/desk` | Runtime flags + pattern-linked open positions (enriched). |
| `PATCH` | `/api/trading/autotrader/desk` | `{paused?, live_orders?}` (paired user). |
| `PATCH` | `/api/trading/autotrader/positions/{trade_id}` | `{kind, monitor_paused?, synergy_excluded?}`. |
| `POST` | `/api/trading/autotrader/positions/{trade_id}/close` | `{kind, confirm: true}`. 409 on failure. |

All endpoints require a paired user.

## Shadow replay

```powershell
conda run -n chili-env python scripts\autotrader_shadow.py --limit 50 --skip-llm
```

Read-only: replays recent pattern-imminent alerts through the rule gate (and
optionally the LLM revalidation) and prints a decision histogram. No broker
calls, no DB writes. Use before flipping live.

## Tests

- `tests/test_autotrader_desk_api.py` — desk GET/PATCH
- `tests/test_autotrader_position_overrides.py` — per-position overrides + close-now
- `tests/test_autotrader_pdt_soft_warn.py` — PDT audit stamping
- `tests/test_autotrader_live_readiness.py` — kill-switch precedence + desk toggle
- `tests/test_autopilot_page_smoke.py` — page + endpoint smoke
- `tests/test_auto_trader_*` — orchestrator, rules, monitor, synergy, LLM
