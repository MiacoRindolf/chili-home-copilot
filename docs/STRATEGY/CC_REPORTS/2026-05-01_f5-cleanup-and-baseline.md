# CC_REPORT: f5-cleanup-and-baseline

## What shipped

- **`cb137ea`** — `feat(fast-path): F5 - exit manager closes paper-trade loop`
  - 5 files (4 modified + 1 new), +717 / -12
  - New: `app/services/trading/fast_path/exit_manager.py` (518 lines)
  - Modified: `supervisor.py`, `fast_path_api.py`, `stop_engine.py`, `migrations.py`
  - Migration: `218_fast_exits` (partitioned table, 3 indexes, 1 unique)
  - Brain integration: new public `stop_engine.compute_initial_bracket(entry, atr, regime, lifecycle, ...)` wrapper exposing the existing swing-side ATR-based, regime-aware, lifecycle-adjusted bracket policy to callers without a Trade ORM row.
- **`6bab79c`** — `feat(fast-path): F5-cleanup - fast_exits_native view (migration 219)`
  - 1 file, +40 / -0
  - Migration: `219_fast_exits_native_view` (CREATE OR REPLACE VIEW)
  - View applied to live DB out-of-band so reviews can use it now without waiting for a chili-container restart.

Both pushed to `origin/main`.

**Out of F5 scope and intentionally left uncommitted** (don't bundle unrelated changes per PROTOCOL hard rule 6):
- `app/models/trading.py` — phantom-close guard for the legacy Trade model (option-vs-underlying bug class). Independent concern.
- `.env.example` — `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE` env vars for the promotion-evidence audit. Independent concern.
- `CLAUDE.md` — strategy-protocol pointer block + `docs/STRATEGY/` itself. These are the protocol's own infrastructure; flagging for a separate commit by Cowork or the operator since strategy plumbing is not part of F5-the-feature.

## Verification

### F5 commit ✅

```
$ git log --oneline -5
6bab79c feat(fast-path): F5-cleanup - fast_exits_native view (migration 219)
cb137ea feat(fast-path): F5 - exit manager closes paper-trade loop
8f18be5 feat(fast-path): wire real Coinbase live placement (gated, paper-default)
f420ea6 feat(autopilot): fast-path paper-trade view with real-time P/L
1431cb9 feat(fast-path): F4 - paper-mode executor with mode interlock + gates

$ git push origin main
   8f18be5..cb137ea  main -> main
   cb137ea..6bab79c  main -> main
```

### Live data verification ✅

Bracket-age separation is cleanly bimodal — no ambiguity at the 60s threshold:

```
 entry_id |  ticker  |  bracket_age_s |  classification
----------+----------+----------------+-----------------
      398 | DOGE-USD |          0.319 | native
      402 | BTC-USD  |          0.339 | native
      404 | ETH-USD  |          0.325 | native
      160 | DOGE-USD |       2972.010 | inherited
       56 | DOGE-USD |       4109.290 | inherited
      205 | SOL-USD  |       2321.208 | inherited
```

`fast_exits_native` view returns only the 3 native rows.

### Unhealthy investigation ❌ — recurring, flagged

Container is currently `(unhealthy)` after 59 min uptime. The pattern is clear from `docker compose logs fast-data-worker --since 10m`:

- WS connection healthy: `errors_60s=0 reconnects=0 queue_depth=0/10000`
- L2 books streaming: `book updates_recv=205735 emitted=49597`, alerts firing every few seconds
- **Candle channel sparse**: `last_bar_at` snapshots at 23:34:12 show ALL 5 pairs at `2026-05-01T23:31:00` (~3 min old). At 23:36:12 BTC and AVAX still stuck at 23:31:00 while ETH/SOL/DOGE updated to 23:36:00.
- **Healthz oscillates 200↔503** as the freshest pair's bar age crosses the 90s threshold defined in `app/services/trading/fast_path/healthz.py:110`.

So: the cause is what Cowork's Open Question #2 anticipated — the 90s healthcheck threshold is too tight relative to candle-channel cadence on quiet pairs. WS is alive; the candles channel just goes silent on low-volatility pairs for minutes at a time.

Per the brief (*"if you find evidence it could recur, STOP and flag in Open Questions. Do not pull on the thread mid-cleanup"*), I stopped here. Surfacing in Open Questions below.

## Surprises / deviations

1. **Brain_json already gives us the marker for free.** The exit_manager populates `brain_json.computed_at` at bootstrap time using `datetime.now()`. Combined with `entered_at` on the same row, the gap is a clean classifier — no schema column needed, no exit_manager code change needed, no backfill UPDATE needed. Picked Option B (per the brief's `B is probably faster and just as good — your call`) implemented as a pure SQL view. The 60s threshold sits in a >2300x gap (0.3s vs 2300+s) so there's no calibration risk.

2. **Container health is NOT transient as the brief assumed it might be.** The 22:00–23:04 episode wasn't a one-off — the container is `(unhealthy)` again right now from the same root cause. Flagging.

3. **`models/trading.py` change in the working tree was unrelated.** The brief listed it as F5; in practice the diff was a phantom-close guard for the legacy Trade ORM (option-vs-underlying bug class), with no `FastExit` ORM model added. F5 writes to `fast_exits` via raw SQL, not ORM. Excluded from the F5 commit.

4. **`.env.example` and `CLAUDE.md` changes were unrelated to F5.** Excluded from the F5 commit. `CLAUDE.md` contains the strategy-protocol pointer that Cowork wrote — that should land as part of a "strategy infrastructure" commit alongside `docs/STRATEGY/` itself; deferring that decision.

## Deferred

- **Strategy infrastructure commit** (`CLAUDE.md` pointer + `docs/STRATEGY/PROTOCOL.md` + `CURRENT_PLAN.md` + `NEXT_TASK.md` + this report). This is meta-process plumbing, not F5-the-feature, and the brief was explicit about a single F5 commit. Recommending Cowork or the operator stage these together; happy to do it on the next run if desired.
- **Marking `NEXT_TASK.md` STATUS: DONE** — done in this same run, but committed alongside the strategy infrastructure since it's adjacent.
- **Tightening `ALERT_RECENCY_MAX_AGE_S`** — explicitly out-of-scope per constraints, deferred to post-F6.
- **No-recent-bars healthcheck** — out of scope per "don't pull on the thread", deferred (see Open Questions).

## Open questions for Cowork

1. **(Surfacing the brief's own Open Question #2.)** Confirmed: the unhealthy state is driven by the 90s `last_bar_at` freshness threshold, not WS connectivity. Two options I see, neither attempted in this task:
   - **Raise the threshold** (e.g., to 180s or 300s). Cheap, but tightens the wrong knob — we lose the ability to detect a *real* candle-stream outage on volatile pairs.
   - **Split the probe into two checks** — `ws_connected` (heartbeats + reconnects) and `candle_freshness` (long threshold, e.g. 5 min, on at least one pair). This is the structurally correct answer; it's small but it's its own task.

   My vote: option 2, scheduled as a small follow-up. Don't ship F6 work over a known-flapping `(unhealthy)` container — it muddies any future incident triage.

2. **(Surfacing the brief's own Open Question #1.)** With 3 F5-native exits in the can (all stop_hit, all DOGE/BTC/ETH paying ~16-32 bps each) and 6 still-open positions floating green, do we extend the soak before F6 starts mining? My read: F6 mines from `fast_alerts` history (signal half-life is observable *without* realized exits — it's measured against subsequent price trajectory in the L2 book), so F6 can start now and realized exits will accumulate in parallel. Confirming.

3. **F5-native sample is too small to mean anything.** 3 round trips, 3 stop_hits, 0% win rate, -$0.18. This is consistent with the calibration thesis (DOGE 16-bp ATR/price ratio is structurally tight against current bracket policy) but is not statistically separable from "lost the coin flip." Edge-proof bar in `CURRENT_PLAN.md` is >50 round trips — we are at 3. F6's signal-decay mining is the right next move; just flagging that we should not draw any "F5 strategy is broken" conclusion from the current dataset.

## Verbatim SQL for review use

**Headline P/L on F5-native trades only** — copy-paste:

```sql
SELECT
  COUNT(*)                                            AS native_round_trips,
  ROUND(SUM(realized_pnl_usd)::numeric, 4)            AS total_pnl_usd,
  ROUND(AVG(realized_return_pct)::numeric, 4)         AS avg_return_pct,
  COUNT(*) FILTER (WHERE realized_pnl_usd > 0)        AS wins,
  COUNT(*) FILTER (WHERE realized_pnl_usd <= 0)       AS losses,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE realized_pnl_usd > 0)
    / NULLIF(COUNT(*), 0),
    2
  )                                                   AS win_rate_pct
FROM fast_exits_native;
```

Current result (2026-05-01 ~23:40 UTC):

```
 native_round_trips | total_pnl_usd | avg_return_pct | wins | losses | win_rate_pct
--------------------+---------------+----------------+------+--------+--------------
                  3 |       -0.1818 |        -0.2423 |    0 |      3 |         0.00
```

**Per-trade detail on F5-native trades only**:

```sql
SELECT entry_execution_id, ticker, exit_reason, entry_price, exit_price,
       realized_pnl_usd, realized_return_pct, holding_period_s, exited_at
FROM fast_exits_native
ORDER BY exited_at;
```
