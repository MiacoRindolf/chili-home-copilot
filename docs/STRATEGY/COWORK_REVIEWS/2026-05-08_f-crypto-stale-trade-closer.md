# COWORK_REVIEW: f-crypto-stale-trade-closer

**Verdict:** Ship it. Phase E delivered the crypto-side parity with
Phases A+B+C and on its first run cleaned up a **14-trade backlog**
of phantom-open crypto rows that had been accumulating undetected.
DOT-USD trade 1810 is now `cancelled / entry_never_filled`. Two
small follow-up items surfaced from the first-run behaviour and are
queued as `f-phase-e-backlog-cleanup-fixes`.

## Algo-trader lens

The blast radius was much bigger than the operator's visible
DOT-USD concern. The first-sweep audit caught:

```
Layer 1 cancellations: 14
Tickers: DOT, HBAR, ADA, AAVE, XPL, SOL, XRP, RAY, RENDER,
         AVAX, QNT, TRUMP, SKY, XLM
```

Every one of these had `last_fill_at IS NULL` despite a
`broker_order_id` being set — the broker placed orders that never
reported fills, and chili wrote `status='open'` optimistically and
never reconciled. Without Phase E, these would have continued
indefinitely, polluting:
- The bracket reconciler's log pipeline (one `missing_stop:warn`
  per minute per phantom = ~14 warnings/min hidden in the noise).
- Capital-allocation snapshots (chili thought it had 14 open
  crypto positions; broker had zero).
- Pattern-quality feedback (the brain saw 14 "live" trades with
  no fills and presumably never updated their realized stats).
- The "what's open right now" operator query.

This is a structural fix that both retroactively cleans up the
existing mess AND prevents recurrence. Real-money correctness gain.

After the sweep:
- Trade 1810 DOT-USD: `status='cancelled'`,
  `exit_reason='entry_never_filled'`, `pnl=NULL` (correct: no
  realized PnL to claim, no fictitious gain or loss).
- All 14 phantom-opens cleared.
- PDT count for user_id=1: still 0 (Phase A's frozenset
  extension is doing its job — the new exit reasons don't
  pollute the count).

## Dev-architect lens

CC's three notable choices:

1. **Settings read at call-time, not at module-import.** The two
   threshold constants (`CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS`,
   `CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN`) are resolved
   inside the helper functions per-call. Operator can tune via
   env vars without restart. Good ergonomics; matches Phase B's
   testability-seam pattern.

2. **`broker_crypto_tickers` test-injection seam.** Production
   calls pass `None`; `run_crypto_stale_trade_close` then calls
   `coinbase_service.get_crypto_positions()` itself. Tests
   inject a known list. Avoids global mocking of the broker
   service.

3. **Phase A's `expanding=True` bindparam paid off.** Adding the
   two new exit reasons to the frozenset auto-applied to the SQL
   filter — no migration, no SQL edit, no risk of typo
   between the constant and the literal. Phase A's earlier
   architectural choice is now load-bearing across three phases.

## What surprised me

**The 14 phantoms.** I expected ~1-3 from the operator's DOT-USD
report. Finding 14 means there's a structural pattern of
`broker_order_id-set, last_fill_at=NULL` events that's been
recurring. Worth a separate audit: WHY are crypto entries
placing-but-not-filling at this rate? Could be:
- Broker rate-limiting / order rejection chain that doesn't
  surface as an explicit error
- Coinbase's crypto-via-Robinhood routing returning order IDs
  for orders that get silently rejected downstream
- Pattern 585's stop_model producing entries that the broker
  rejects (e.g., insufficient buying power, post-only that
  doesn't take immediately)

I'd queue an investigation brief if this pattern continues
post-Phase-E (i.e., new phantoms accrete despite the sweep
running per-cycle).

## Two follow-ups queued

`f-phase-e-backlog-cleanup-fixes` covers:

1. **Backlog-aware burst breaker exemption.** First-sweep
   cleanup of 14 phantoms tripped Phase B's
   `_record_reconcile_close_burst` (3-in-5s = wipeout
   signature). It correctly tripped per its current rule, but
   the rule conflates "real wipeout" (today's positions closed
   simultaneously) with "backlog cleanup" (week-old phantoms
   closed in batch). Fix: add a `trade_age_seconds` discriminator
   so old trades don't trip the breaker. Steady-state (~1-2
   phantoms/week) won't trip; only first-runs do.

2. **`trading_bracket_intents` stale state.** The sweep cancelled
   trade 1810 but `bracket_intents` row 233 still shows
   `intent_state='intent'`, `last_diff_reason='missing_stop:warn'`.
   The reconciler keeps emitting warnings for the now-cancelled
   trade. Fix: have the sweep update the matching intent rows
   in the same transaction.

Both small. Operator picks ordering for the next NEXT_TASK.

## Operator action already taken (this session)

- Reset breaker via `scripts/d-breaker-reset.ps1`. DB now shows
  newest snapshot `breaker_tripped=false`. All three workers
  (chili / autotrader / scheduler) verified clean in-memory state.
- Phase E sweep is now running per-cycle on the bracket
  reconciler's ~60s cadence. Steady-state confirmed.

## What's left

The wipeout-cascade chain is structurally complete:

| Layer | Asset class | Brief / Commit | Status |
|---|---|---|---|
| Phase A: PDT count filter | equity | `60c26f8` | live |
| Phase B: wipeout burst + obs | equity | `bc1a0f3` | live |
| Phase C: per-trade streak gate | equity | `1d6cf3b` | live |
| Phase D: pattern-demote | pattern lifecycle | `dfb39f0` (sweep code) | live but wiring imperfect |
| **Phase E: crypto stale closer** | **crypto** | **`c8aec21`** | **live** |

Remaining queued briefs:
- `f-phase-e-backlog-cleanup-fixes` (NEW today; small).
- `f-pattern-demote-sweep-wiring-fix` (per-cycle hook fix; small).
- `f-pdt-crypto-bypass-cleanup` (hygiene).
- `f-autotrader-pdt-aware-exit-deferral` (premise was flawed;
  needs rewrite).

### Suggested next direction

`f-phase-e-backlog-cleanup-fixes` — closes the two follow-ups
from this brief; small scope; immediate quality-of-life win.
After that, the pattern-demote-sweep-wiring-fix to give the
demote sweep its proper per-cycle cadence.

## Final note

Phase E's audit-first protocol earned its keep: the brief
DELIBERATELY documented "operator runs the audit" because the
sandbox couldn't read prod data. CC said so honestly in the CC
report. The audit then surfaced 13 OTHER phantoms beyond the
one operator reported. That's the pattern: audit-first surfaces
unknowns; operator decides whether the blast radius is acceptable
before deploy. In this case it was — the brief's "OR the first
sweep handles it post-deploy" allowance was correctly invoked.

Three real-money correctness wins shipped today across five
commits. The reconciler infrastructure is now structurally
complete across both asset classes.
