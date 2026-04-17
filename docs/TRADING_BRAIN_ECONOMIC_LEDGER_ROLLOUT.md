# Trading Brain — Economic-truth ledger (Phase A) rollout

## What this is

A single canonical append-only ledger of **economic events** (fills, fees,
adjustments) with explicit ``cash_delta`` and ``realized_pnl_delta``. It
exists parallel to legacy ``Trade`` / ``PaperTrade`` rows. Legacy ``pnl``
columns remain authoritative until a later cutover phase.

Purpose: give every downstream measurement (NetEdgeRanker calibration,
Phase D economic promotion metric, Phase K divergence panel) a single
trustworthy realized-PnL stream instead of four partial ones.

## Rollout ladder (same as Phase B / Phase E)

| Mode          | Ledger writes | Reconcile vs legacy | Authoritative for PnL |
| ------------- | :-----------: | :-----------------: | :-------------------: |
| off           |       no      |          no         |           no          |
| shadow        |      yes      |         yes         |           no          |
| compare       |      yes      |         yes         |           no          |
| authoritative |      yes      |         yes         |   **yes** (cutover)   |

Current phase (Phase A): ``shadow``. ``authoritative`` is a later phase with
its own plan, its own soak window, and its own release-blocker change.

## Forward procedure

1. Set ``BRAIN_ECONOMIC_LEDGER_MODE=shadow`` in ``.env``.
2. ``docker compose up -d --force-recreate chili brain-worker``.
3. Verify migration 129 applied:
   ```sql
   SELECT version_id FROM schema_version WHERE version_id LIKE '129%';
   ```
4. Confirm tables exist:
   ```sql
   SELECT to_regclass('trading_economic_ledger'),
          to_regclass('trading_ledger_parity_log');
   ```
5. Force a synthetic paper trade open+close; confirm two ledger rows +
   one parity row appear, ``agree_bool=true``, ``|delta_abs| <= 0.01``.
6. GET ``/api/trading/brain/ledger/diagnostics?lookback_hours=1`` returns
   ``{ok:true, ledger:{mode:"shadow", events_total>=2, parity_total>=1}}``.
7. ``scripts/check_ledger_release_blocker.ps1`` on live logs exits 0.

## Rollback

1. Set ``BRAIN_ECONOMIC_LEDGER_MODE=off`` in ``.env``.
2. ``docker compose up -d --force-recreate chili brain-worker``.
3. Verify no new ``[ledger_ops]`` lines for 5 minutes.
4. Existing ledger rows are preserved; no destructive rollback.

## Ops log shape (frozen)

Prefix: ``[ledger_ops]``

```
[ledger_ops] mode=<off|shadow|compare|authoritative>
             source=<paper|live|broker_sync>
             event_type=<entry_fill|exit_fill|partial_fill|fee|adjustment|reconcile>
             trade_ref=<paper:NNN|live:NNN|broker_sync:NNN>
             ticker=<up to 24 chars>
             qty=<6dp or none>
             price=<6dp or none>
             cash_delta=<4dp or none>
             realized_pnl_delta=<4dp or none>
             agree=<true|false|none>
```

One line per write. Bounded; no PII, no provenance blobs.

## Release-blocker rule

A log line is a **BLOCKER** iff it contains both:

- ``[ledger_ops]``
- ``mode=authoritative``

and the target environment is not meant to run the ledger in authoritative
mode (i.e. until the cutover phase opens).

```powershell
docker compose logs chili --since 30m 2>&1 | .\scripts\check_ledger_release_blocker.ps1
```

Exit 0 → pass. Exit 1 → authoritative leak; stop the deploy.

## Diagnostics endpoint

``GET /api/trading/brain/ledger/diagnostics?lookback_hours=<1..168>&source=<paper|live|broker_sync>``

Response shape (frozen):

```json
{
  "ok": true,
  "ledger": {
    "ok": true,
    "mode": "shadow",
    "lookback_hours": 24,
    "tolerance_usd": 0.01,
    "events_total": 42,
    "events_by_type": {"entry_fill": 21, "exit_fill": 21},
    "events_by_source": {"paper": 42},
    "parity_total": 21,
    "parity_agree": 21,
    "parity_disagree": 0,
    "parity_rate": 1.0,
    "mean_abs_delta_usd": 0.0,
    "max_abs_delta_usd": 0.0,
    "top_disagreements": []
  }
}
```

## Known limitations this phase

- Live entry fills are **lazy-emitted at close time** from ``Trade.avg_fill_price``
  / ``filled_quantity``. Real-time entry emission from ``broker_service``
  is out of scope for Phase A (lands in Phase G when live brackets are wired).
- Reconciliation is **ledger-vs-legacy-pnl** only. Venue reconciliation
  (ledger-vs-broker state) lands in Phase F/G.
- Fees are folded into the exit leg for paper; per-fill fee attribution
  for live brokers is out of scope until Phase F captures
  fee-aware execution events.
- No retroactive backfill of historical closed trades. Only trades closing
  while the mode is shadow+ are observed.

## Tables

### ``trading_economic_ledger``

Append-only. Partial unique indexes enforce at most one ``entry_fill`` and
one ``exit_fill`` per ``(paper_trade_id)`` / ``(trade_id)`` — idempotency
is a first-class property.

### ``trading_ledger_parity_log``

One row per closed-trade reconciliation. ``agree_bool = |delta_pnl| <=
tolerance_usd``. ``tolerance_usd`` defaults to 0.01 USD.
