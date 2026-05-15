# f-netedge-live-wiring (Phase D of evidence-fidelity-architecture)

> **Type:** Add NetEdge shadow-call to autotrader gate path
> **Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
> **Depends on:** Phase A (canonical outcome columns) — NetEdge
> calibrator should read `corrected_*` not raw legacy.

## Goal

NetEdge ranker is technically wired (called from
`portfolio_allocator.py:628`), but every recent row has
`scan_pattern_id=null, regime=unknown` because **the live autotrader
bypasses the allocator entirely**. NetEdge is being fed by paper-shadow
flows that don't have pattern lineage. It can't learn cleanly
by-pattern or by-regime in this configuration.

## Design

### Two-stage wiring

**Stage 1 (this brief):** Add a parallel `net_edge.score(...)` call
from the live autotrader path (`auto_trader.py:_process_one_alert`)
with the full context (scan_pattern_id, regime, timeframe, asset_class).
This is **shadow only** — NetEdge still doesn't gate any decision; it
just records its score next to the heuristic one.

**Stage 2 (deferred — separate brief, post-soak):** Move autotrader to
go THROUGH `portfolio_allocator.evaluate()` so the allocator is the
single source of decision for size + abstain. NetEdge then becomes a
real input to the gate stack.

Stage 1 is low-risk (write-only shadow log). Stage 2 changes the live
decision path and needs more care.

### Where to add the shadow call

In `auto_trader._process_one_alert` (or equivalent), right after the
alert is accepted by the rule gate + LLM revalidation but before
broker placement:

```python
from .net_edge_ranker import score, NetEdgeSignalContext, mode_is_active

if mode_is_active():
    try:
        score(
            db,
            NetEdgeSignalContext(
                ticker=alert.ticker,
                asset_class=alert.asset_class,
                scan_pattern_id=alert.scan_pattern_id,
                raw_prob=pattern.corrected_win_rate or pattern.win_rate,
                entry_price=quote.mid,
                stop_price=bracket_intent.stop_price,
                target_price=bracket_intent.target_price,
                regime=regime_snapshot.get("regime"),
                timeframe=pattern.timeframe,
                heuristic_score=expected_edge_net,
            ),
        )
    except Exception as e:
        logger.debug("[autotrader] netedge shadow score failed: %s", e)
```

Same hook in `crypto_autotrader.py` if it's a separate code path.

### Regime population

The other half of "regime=unknown": ensure `regime_snapshot` is
populated for every autotrader tick. Check the regime_ledger feed —
if it's stale, the score call gets `regime=None` → bucketed as
"unknown". Brief deliverable D3 below checks this.

## Deliverables

1. **`app/services/trading/auto_trader.py`** — add shadow `score(...)`
   call in `_process_one_alert` after rule-gate + LLM pass
2. **`app/services/trading/crypto_autotrader.py`** (if separate) —
   same hook
3. **`app/services/trading/regime_snapshot.py`** (or wherever the
   snapshot is built) — diagnostic check: if regime is empty/unknown
   in >50% of recent ticks, log a warning so operator notices
4. **`tests/test_netedge_autotrader_wiring.py`** — fixture: synthesize
   an alert with known pattern_id + regime, assert NetEdge row
   written with non-null fields
5. **CC_REPORT**: `docs/STRATEGY/CC_REPORTS/2026-05-14_netedge-live-wiring.md`

## Hard constraints

- NO change to live trade decision path. NetEdge score is shadow-log
  only at merge.
- `brain_net_edge_ranker_mode` stays "shadow" — Stage 2 (authoritative)
  is operator-controlled in a future brief.
- Reads `corrected_*` columns from scan_patterns (Phase A dependency).
- Failure of `net_edge.score(...)` MUST NOT block the autotrader (the
  call is wrapped in try/except already in the allocator pattern).
- No autotrader / venue / broker behavior change.

## Consult gate

Wholesale move autotrader → allocator, or parallel shadow call from
autotrader to NetEdge? Brief default: parallel call first (Stage 1),
full integration after Stage 1 soak. CC should confirm.

## Why Stage 1 is enough for this brief

Once NetEdge has 500+ rows with non-null `scan_pattern_id` and `regime`,
the calibrator can train per-pattern, per-regime. That's the real
"NetEdge learning cleanly" payoff codex was describing — and it
arrives from this brief alone, without needing Stage 2.
