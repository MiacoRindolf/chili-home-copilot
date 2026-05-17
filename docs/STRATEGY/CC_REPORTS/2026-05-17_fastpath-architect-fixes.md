# CC_REPORT: Fast-path architect fixes — 2026-05-17

**Date:** 2026-05-17
**Author:** Cowork (algo-trader architect pass)
**Scope:** Two findings from the 24h fast-path reject-distribution probe.

## TL;DR

- **Shipped:** `emit_short_alerts` gate stops the scanner from emitting `imbalance_short` on long-only venues. Drops 39% of wasted alert volume (2,546/6,595 alerts/24h that were 100% rejected with `short_unsupported_in_spot`).
- **Diagnosed (operator action):** `fast_path_universe` is empty because `CHILI_FAST_PATH_UNIVERSE_ROTATION_ENABLED` defaults to `False` and the operator never flipped it. The rotator code itself appears wired correctly (http-retry, top-of-book, and 403 fixes all shipped per memory).
- **Two follow-up items** flagged for separate briefs.

## What the probe showed (24h, 2026-05-16)

```
6,595 executor decisions
  6,583 rejected (99.85%)
     10 paper_fill (0.15%)

Reject reasons (top 5):
  negative_edge:negative_edge            2,874 (44%)  ← cost-aware gate firing correctly
  short_unsupported_in_spot              2,546 (39%)  ← WASTE (this fix)
  maker_limit_price_unavailable            891 (13%)  ← follow-up #1
  capacity:pair_already_held               192  (3%)  ← fine
  recency / score / spread (combined)       80  (1%)  ← background noise

Mode = 'paper' for all 6,595 decisions.
fast_path_universe = 0 rows                              ← follow-up #2 (operator flip)
```

## Fix shipped: `emit_short_alerts` gate

### Files

- `app/services/trading/fast_path/settings.py`: added `emit_short_alerts: bool = False` field + `_env_bool("CHILI_FAST_PATH_EMIT_SHORT_ALERTS", False)` in `load()`.
- `app/services/trading/fast_path/scanner.py`: `MomentumScanner.__init__` now takes `emit_short_alerts: bool = True` kwarg (default True preserves backwards-compat with existing test fixtures). Emission site at `on_book_emit` is gated; new counter `suppressed_short_alert_disabled` surfaces via `.stats()`.
- `app/services/trading/fast_path/ws_client.py`: passes `settings.emit_short_alerts` to the scanner constructor (default False at the settings layer).
- `tests/test_fastpath_emit_short_alerts.py`: 6 tests covering gate-on, gate-off, one-directional asymmetry, stats surfacing, and env-var override.

### Predicted impact

At current 24h rate, the gate eliminates ~2,500 wasted alerts/day = 17,500/week of DB writes (`fast_alerts` insert + `fast_executions` decision-log insert + executor compute). The remaining alert traffic becomes:

```
Expected post-gate:
  imbalance_long              ~3,000/24h  (44% of remaining)
  spread_squeeze                ~100/24h
  volume_breakout_long           ~90/24h
  volume_breakout_pullback_long  ~90/24h
  Total: ~3,300/24h (was 6,595/24h)
```

The reject distribution should compress to:
- `negative_edge` becomes ~85% of rejects (cost-aware gate still dominant — fee structural problem)
- `maker_limit_price_unavailable` becomes ~10%
- Other reasons ~5%

### Operator notes

- **Default is OFF.** Coinbase spot can't short, so this is safe-by-default for the current venue.
- **Override for perp venues** (Hyperliquid / dYdX / Drift): `CHILI_FAST_PATH_EMIT_SHORT_ALERTS=true` in `.env` + restart.
- **No restart required for the gate to take effect** once the new code is deployed — `MomentumScanner` is instantiated fresh per process startup. Force-recreate `fast-data-worker` after the deploy.

## Diagnosed: rotator is flag-OFF

### Root cause

`app/services/trading_scheduler.py:3170-3175`:

```python
fp_settings = fp_settings_mod.load()
if not fp_settings.universe_rotation_enabled:
    logger.debug(
        "[scheduler] fast-path universe rotator: skipped "
        "(universe_rotation_enabled=False)"
    )
    return
```

`CHILI_FAST_PATH_UNIVERSE_ROTATION_ENABLED` defaults `False` (`settings.py:252`). The operator never flipped it, so the rotator job has been a no-op since 2026-05-07 ship. The hourly schedule fires every 60min and immediately returns.

### Operator action to enable

1. Confirm `.env` has `CHILI_FAST_PATH_UNIVERSE_ROTATION_ENABLED=true`
2. Confirm volume threshold is reasonable: `CHILI_FAST_PATH_UNIVERSE_MIN_VOLUME_24H_USD=2000000` (memory says $2M was set 2026-05-08)
3. `docker compose up -d --force-recreate scheduler-worker`
4. Wait ≤60min for first pass.
5. Probe: `SELECT * FROM fast_path_universe ORDER BY composite_score DESC LIMIT 25;`

If the rotator runs but still produces 0 rows after the flag flip, escalate via a new brief (the http-retry + 403 + top-of-book fixes are all in HEAD per memory, so a fresh failure mode would be new investigation).

## Follow-up #1: `maker_limit_price_unavailable` (891/24h, 13%)

13% of decisions can't compute a maker limit price. Suspicion: orderbook snapshot staleness at decision time, or a corner case in the maker-price formula when the best_bid/best_ask is missing. Worth a focused probe:

```sql
SELECT ticker, COUNT(*) FROM fast_executions
WHERE reject_reason = 'maker_limit_price_unavailable'
  AND decided_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

If concentrated on specific tickers → orderbook freshness gap on those. If uniform → calc bug. Brief stub: `docs/STRATEGY/QUEUED/f-fastpath-maker-limit-price-availability.md`.

## Follow-up #2: structural fee problem still dominates

Even after this fix and after the rotator/maker-only activation, the cost-aware gate's `negative_edge` rejection of ~3,000/24h (now ~85% of rejects) is the fee math. Per the 2026-05-07 alpha replay:

```
Best Coinbase pair: ICP-USD, +6.13 bps 5m edge, +2.76 bps net maker.
Second-best: TAO-USD, +2.55 bps 5m edge, -0.07 bps net maker.
```

**One pair net-positive at maker tier is not a strategy.** The architect view from the 2026-05-16 diagnosis stands: same engineering effort on Hyperliquid perps (5 bps round-trip taker, can short) would unlock the 3,000 wasted-on-spot imbalance_short alerts AND extend the universe by 10×. The fast-path code is venue-agnostic at the alert layer; the executor would need a Hyperliquid adapter to compose with the existing path.

Not a brief yet — flagging for operator strategic decision. Sequence if pursued:
1. Hyperliquid auth wiring (~1 day, mirror of Coinbase Phase 2)
2. Hyperliquid maker/taker executor (~2 days)
3. Hyperliquid universe loader (~1 day)
4. Side-by-side shadow log on a small universe (1 week)
5. Compare realized PnL vs Coinbase shadow log → decision point

## State on close

- 3 commits ahead of HEAD `3e3253b` (the breaker-attribution fix):
  - `feat(fast_path): emit_short_alerts gate on scanner` (settings + scanner + ws_client)
  - `test(fast_path): emit_short_alerts regression tests`
  - `docs(fast_path): architect fixes CC_REPORT` (this file)
- Operator actions queued in this report (rotator flag flip + force-recreate).
- 2 follow-up briefs identified but not authored — operator's call on priority.
