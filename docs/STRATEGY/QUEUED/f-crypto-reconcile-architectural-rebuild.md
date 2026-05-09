# f-crypto-reconcile-architectural-rebuild

STATUS: QUEUED
SLUG: crypto-reconcile-architectural-rebuild
PROPOSED: 2026-05-08
SEVERITY: critical (real money; chili silently lost track of 14 open crypto positions for up to 7 days because the crypto-side reconcile chain has SIX independent failures)

## TL;DR

Today's DOT-USD "missed exit" investigation surfaced the entire
crypto-side reconcile chain is structurally broken. Six independent
failure modes were ALL contributing:

1. **Multi-process auth cache divergence** — chili UI reconnect to RH
   updates DB session but broker-sync-worker's in-memory
   `_logged_in` flag stays True with TTL not yet expired, so it
   reports "connected" while underlying rh.session is stale.
2. **Silent-empty position fetch** — `get_crypto_positions()` has
   THREE return-empty paths (`if not is_connected: return []`,
   `if not crypto_pos: return []`, `except: return []`) all
   indistinguishable from "broker has zero positions."
3. **R32 GUARD over-defensive** — refuses to mass-close when
   rh_tickers is empty. This is correct for wipeout protection
   but means real-time-empty (auth issue) kept SOL/DOT
   status='open' for hours after the autotrader actually sold them.
4. **Crypto exit_monitor only sets pending_exit, never status='closed'** —
   relies on broker_sync stale-close path, which loses the actual
   exit_reason ('take_profit_hit' → 'broker_reconcile_position_gone')
   AND the actual fill price (uses _resolve_close_exit_price fallback).
5. **`sync_pending_exit_order` is dead code** — defined in
   `robinhood_exit_execution.py:1191` but called from nowhere.
   The pending-exit-poller path that should confirm fills and
   write status='closed' deterministically doesn't exist.
6. **Bracket writer `place_missing_stop` crashes with
   "list index out of range"** on every attempt for ADA/SOL,
   so attempts retry forever in a loop. Plus the initial bracket
   placement path didn't fire for 10 of 14 crypto trades — only
   4 have `trading_bracket_intents` rows at all.

Phase E (commit `c8aec21`, my brief from earlier today) was the
WRONG fix. It misread "broker says zero positions" as "trade is a
phantom" — but on a stale-auth empty list, EVERY trade looks like a
phantom. I disabled Phase E in `.env` after it false-cancelled 14
real positions. Operator manually reconnected RH UI to restore
chili's auth. Real loss: ~10 minutes of false-cancellation while
the broker-sync still thought positions were empty. The autotrader's
exit_monitor DID submit the SOL/DOT sells correctly, they DID fill
at the broker, but the DB never reflected the close until I ran a
manual SQL UPDATE with operator approval.

This brief is the architectural rebuild. Not a band-aid.

## Six anomalies, six fixes

### Anomaly 1: Multi-process auth cache divergence

**Root cause:** `broker_service._logged_in` and `_last_login` are
module-level globals that don't sync across processes. When
operator reconnects via chili UI, only chili's process knows.
Broker-sync-worker keeps its stale `_logged_in=True` until the TTL
expires (`_LOGIN_TTL` = ?), so `is_connected()` returns True but
underlying `rh.session` returns empty/error.

**Fix:** Replace the in-memory `_logged_in` cache with a
DB-backed liveness check. Each `is_connected()` reads
`broker_session_state.last_validated_at`; if older than 30s, do a
cheap probe (e.g., `rh.load_account_profile()`) and update. Each
process re-validates independently, and a UI reconnect
invalidates the row so all processes pick up fresh state on next
check. Per-process cost: ~1 extra API call per 30s, negligible.

### Anomaly 2: Silent-empty position fetch

**Root cause:** `get_crypto_positions()` returns `[]` on auth
fail, on cache hit returning `None`, on broker error, and on
genuinely-empty position list. Caller can't distinguish.

**Fix:** Replace single-return-empty with a typed result:
```python
@dataclass
class BrokerPositionsResult:
    positions: list[dict] | None  # None iff query failed
    fresh: bool                   # False iff served from cache
    auth_alive: bool              # Result of explicit auth probe
    error: str | None             # Description if positions is None

def get_crypto_positions_v2() -> BrokerPositionsResult: ...
```

Callers that need ground truth (R32 GUARD, Phase E sweep,
broker_sync close path) MUST gate on `result.auth_alive AND
result.positions is not None AND result.fresh`. If any is false,
SKIP the reconcile decision. Old `get_crypto_positions()` stays
as a thin wrapper for non-reconcile callers (e.g., portfolio
display).

### Anomaly 3: R32 GUARD too coarse

**Root cause:** R32 (commit `539e1c2`, 2026-04-30) refuses
mass-close when `rh_tickers` is empty. Correct for wipeout
protection but conflates "auth flap" with "valid empty
account." Today's incident: auth was broken but `rh_tickers`
was empty FROM THE BROKER not from auth-flap, AND positions
WERE legitimately closed at the broker. R32 wouldn't act on
them.

**Fix:** R32 should require `auth_alive=True` (Anomaly 2's
typed result) AND empty positions list AND positive open-trade
count to skip-close. If `auth_alive=False`, also skip-close
(don't try to act on stale data). The new gate is "skip if
data is stale OR (data is fresh AND empty)." Today's Phase E
also needs this gate.

### Anomaly 4: Crypto exit_monitor doesn't deterministically close

**Root cause:** `crypto/exit_monitor.py:312-348` submits sell,
sets `pending_exit_*`, logs "[crypto_exit] CLOSED" — but never
sets `status='closed'`, `exit_price`, `pnl`, `exit_reason`. Relies
on a downstream poller that doesn't exist for crypto.

**Fix:** After successful sell submission, immediately call
broker order detail (e.g., `rh.get_crypto_order_info(order_id)`)
to get the actual fill state. If `state='filled'`, call
`_finalize_filled_exit(...)` (the equity path's
finalize) to write status='closed' deterministically with the
real exit_price and exit_reason='take_profit_hit'. If state is
not yet filled (queued/submitted), set pending_exit_* AND
schedule a poll task in N seconds. Today's "[crypto_exit] CLOSED"
log line should be renamed to "[crypto_exit] SELL_SUBMITTED"
when the close write didn't actually happen yet.

### Anomaly 5: `sync_pending_exit_order` is dead code

**Root cause:** Function defined in
`robinhood_exit_execution.py:1191` to poll pending-exit orders
and finalize on fill. Zero callers in the codebase
(`grep -r "sync_pending_exit_order(" app/` confirms).

**Fix:** Wire `sync_pending_exit_order` into a per-cycle
scheduler job (`pending_exit_reconciler` running every 30s):
```python
def run_pending_exit_reconciler(db):
    rows = db.query(Trade).filter(
        Trade.status == 'open',
        Trade.pending_exit_order_id.isnot(None),
        Trade.pending_exit_status == 'submitted',
    ).all()
    for t in rows:
        order = (
            broker_service.get_crypto_order_info(t.pending_exit_order_id)
            if t.ticker.endswith('-USD')
            else broker_service.get_order_by_id(t.pending_exit_order_id)
        )
        if order:
            sync_pending_exit_order(db, t, order=order)
```

### Anomaly 6: bracket_writer crashes on missing-stop placement

**Root cause:** `g2_place_missing_stop_rejected` events for
ADA/SOL show `error: "list index out of range"` — every cycle.
The error is in chili code, not broker rejection. Probable site:
the place_missing_stop path indexes into `cost_bases[0]` or
similar without checking length. With 60 events/hour on 2
trades, this is a hot loop wasting cycles.

**Fix:** Locate the IndexError site in
`bracket_writer_g2.place_missing_stop` (line range 876+) via
log traceback + code inspection. Add bounds check or use
`(cost_bases or [{}])[0]` pattern. Add a 5-min cooldown after
ANY exception (not just terminal-rejects) so a code bug doesn't
hammer the API.

### Plus: 10 of 14 trades have no bracket_intent at all

Audit finding: HBAR, AAVE, XPL, XRP, RAY, RENDER, AVAX, QNT,
SKY, XLM never had `trading_bracket_intents` rows created. ZERO
`trading_execution_events` rows around their entry times. This
is a SEPARATE bug from Anomaly 6 — the *initial* bracket
placement path didn't fire for these crypto entries. Possibly
because the autotrader entry path doesn't always invoke
bracket_writer for crypto, or because the writer was guarded
on something that filtered crypto out.

**Fix:** Audit the entry-time bracket-writer invocation for
crypto. Should be: every successful crypto entry creates a
bracket_intent row. If not, find why and fix.

## Deferred (not in this brief)

- Phase E removal (`f-crypto-stale-trade-closer`) — the
  brief's heuristics are unsafe; revert the commit entirely
  rather than keeping it disabled in `.env`.
- Phase B's wipeout-burst breaker exemption for backlog
  cleanups (`f-phase-e-backlog-cleanup-fixes`) — moot if Phase E
  is reverted.
- Pattern-demote sweep wiring fix
  (`f-pattern-demote-sweep-wiring-fix`) — separate brief, can
  ship after this.

## Acceptance criteria (for the architectural rebuild brief)

This is a multi-week brief. Phased implementation:

**Phase 1 (week 1) — auth cache + typed result + R32 gate:**
1. Anomaly 1 fix: DB-backed broker_session_state liveness probe.
2. Anomaly 2 fix: `BrokerPositionsResult` typed return; legacy
   wrapper preserved.
3. Anomaly 3 fix: R32 GUARD requires `auth_alive=True`.
4. Tests for all three.
5. Live verification: simulate stale-auth scenario; confirm
   reconcile skips and surfaces a clear log line.

**Phase 2 (week 2) — pending-exit reconciler + crypto exit close:**
6. Anomaly 4 fix: crypto/exit_monitor calls finalize when fill
   confirmed at submission time.
7. Anomaly 5 fix: pending-exit reconciler wired to scheduler.
8. Tests + live verification (paper-trade a crypto round-trip).

**Phase 3 (week 3) — bracket writer hardening + entry-side audit:**
9. Anomaly 6 fix: IndexError site located + patched + cooldown.
10. Entry-time bracket-writer audit for crypto; document why 10/14
    didn't fire and fix.
11. Tests + live verification.

**Phase 4 (week 4) — Phase E revert + integration tests:**
12. `git revert c8aec21` (Phase E feat commit). Backfill removes
    the unused column via mig 235.
13. End-to-end integration test: simulated round-trip on
    chili_test for crypto with a healthy auth → buy fills →
    bracket placed → take-profit → exit fills → status='closed'
    with right reason and price.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Hard Rule 3**: data-first. Use the typed result pattern; no
  off-schema state.
- **Don't touch the equity-side reconciler** (Phases A+B+C).
- **Don't touch Phase D** (pattern-demote — separate concern).
- **Phase E is to be REVERTED**, not extended. The brief was wrong.
- **Edit-tool truncation discipline (HARD).**
- **Tests use `_test`-suffixed DB.**
- **No magic numbers**: all thresholds (auth probe interval, etc.)
  lift from settings.

## Out of scope

- Equity-side reconcile (already covered by A+B+C).
- Pattern-demote wiring (separate brief).
- Order-placement code paths beyond the bracket-writer crash
  (Anomaly 6).
- Multi-broker support (Coinbase-spot only; same logic but
  separate adapter).

## Operator-side after each phase ships

Each phase has its own deploy + verify steps. The brief lists
them per-phase.

## Rollback plan per phase

- Phase 1: reverting restores in-memory cache. Trade-off: no
  liveness probe, but no behavior regression vs status quo.
- Phase 2: reverting restores legacy "log CLOSED but not actually"
  behavior. Risk: positions linger as status='open' until
  broker_sync stale-close.
- Phase 3: reverting restores the IndexError loop. Burst-protection
  via cooldown stays.
- Phase 4: reverting un-does the Phase E revert. Phase E stays
  disabled via `.env` regardless.

## Open questions

1. **Is RH crypto's order-info endpoint reliable for fill-state
   polling?** robin_stocks has `get_crypto_order_info` but it
   requires login. Verify it returns `state='filled'` reliably
   for sell orders submitted via `place_crypto_sell_order`.
2. **`_LOGIN_TTL` value?** Need to read it; if it's >30s the
   liveness probe interval should be tighter.
3. **Are there other reconcile paths I haven't found?** Earlier
   I missed `sync_pending_exit_order`'s lack-of-callers.
   Suggest a structured audit: every `pending_exit_*` field
   write needs a corresponding read+act path; verify each.
4. **The 10 trades without bracket_intents — were they entered
   via a code path that intentionally skips brackets?** Check the
   autotrader entry code; if there's a "fast crypto entry"
   shortcut that skips bracket creation, the operator should know.

## Appendix: today's incident timeline (UTC)

| Time | Event |
|---|---|
| 2026-05-01 | First crypto entries placed; bracket_intents created for ADA/DOT/SOL/TRUMP only (4/14). 10 entries skipped bracket creation. |
| 2026-05-01–08 | All 14 crypto trades sit `status='open'` with `last_fill_at IS NULL`. Auth was working enough that `get_crypto_positions()` returned the positions. broker_sync stale-close didn't fire (positions WERE held). |
| 2026-05-08 | RH crypto auth state degraded silently (or token expired). `get_crypto_positions()` started returning `[]`. |
| 2026-05-08 17:13 PDT | Operator surfaced "DOT-USD missed exit." |
| 2026-05-08 18:30 PDT | I shipped Phase E (false fix); first-run sweep cancelled all 14. |
| 2026-05-08 18:50 PDT | Operator caught the wrong-cancellations; I reverted all 14 to `status='open'`. |
| 2026-05-08 19:00 PDT | Operator reconnected RH via chili UI. Auth restored in chili process. |
| 2026-05-08 19:01 PDT | autotrader's crypto exit_monitor sold DOT at $1.388 + SOL at $93.69 successfully. Logged "[crypto_exit] CLOSED" but DB row not updated. |
| 2026-05-08 19:22 PDT | I ran operator-approved SQL UPDATE to flip DOT and SOL to status='closed' with correct exit_price + pnl. |
| 2026-05-08 19:30 PDT | Began root-cause investigation. Found the six anomalies above. |

Net realized: +$99.55 on the two closes that did succeed. Net
unrealized: ~$370 across the 12 still-open crypto positions, none
of which have working broker stop/target orders.
