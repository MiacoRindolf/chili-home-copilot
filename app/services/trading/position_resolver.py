"""Shared position-id resolver for the position-identity refactor.

Phase 2 (mig 248, 2026-05-18) introduced
``trading_execution_events.position_id`` and the
``_resolve_position_id_for_event`` helper inside
``execution_audit.py``. Phase 3 (mig 249, 2026-05-18) extends the
same pattern to ``trading_bracket_intents.position_id``, so the
helper is extracted here to be importable from both writer paths
without circular dependencies.

The resolver matches on the (user_id, broker_source, ticker, direction)
natural key — the same key Phase 1's writers used in
``broker_service._phase1_record_position_observation``. Prefers
``state='open'`` over ``state='closed'`` when both match (handles the
close/reopen pattern from the Phase 1 soak).

NEVER raises. All exceptions swallowed. Caller MUST tolerate a NULL
result and continue writing the row regardless. This preserves the
shadow-mode invariant of Phases 1/2/3: the position-identity layer is
write-only until Phase 4's authority flip.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def resolve_position_id(
    db: Session,
    *,
    trade: Any | None = None,
    user_id: int | None = None,
    ticker: str | None = None,
    broker_source: str | None = None,
    direction: str | None = None,
    account_type: str | None = None,
    open_only: bool = False,
) -> int | None:
    """Resolve a ``trading_positions.id`` from event/intent context.

    Resolution priority:
    1. If ``trade`` is provided, prefer attributes off the trade row
       (user_id, broker_source, ticker, direction).
    2. Otherwise use the explicit kwargs.

    Falls back to ``direction='long'`` when not specified (matches
    Phase 1's default in
    ``broker_service._resolve_direction_for_position``). Broker source
    is lower-cased to match the seed convention.

    Returns ``None`` when:
    - broker_source or ticker is missing/empty
    - no matching ``trading_positions`` row exists
    - the DB query raises any exception (swallowed silently)

    When ``open_only`` is true, closed historical identities are ignored.
    Use that for active order/intent paths; falling back to a closed row is
    useful for old event lineage but unsafe for fresh broker actions.

    Returns the ``id`` of the matched position otherwise.
    """
    try:
        if trade is not None:
            uid = getattr(trade, "user_id", None) or user_id
            broker = (
                getattr(trade, "broker_source", None) or broker_source or ""
            )
            tkr = getattr(trade, "ticker", None) or ticker
            dir_ = getattr(trade, "direction", None) or direction or "long"
            acct = getattr(trade, "account_type", None) or account_type
            try:
                from .autopilot_scope import is_option_trade
                is_option = bool(is_option_trade(trade))
            except Exception:
                is_option = str(getattr(trade, "asset_kind", "") or "").lower() in {
                    "option",
                    "options",
                }
        else:
            uid = user_id
            broker = broker_source or ""
            tkr = ticker
            dir_ = direction or "long"
            acct = account_type
            is_option = False

        broker = (broker or "").strip().lower()
        dir_ = (dir_ or "long").strip().lower()
        tkr = (tkr or "").strip()
        acct = (acct or ("spot" if broker == "coinbase" else "cash")).strip().lower()

        if not (broker and tkr):
            return None

        # trading_positions is keyed by the underlying ticker today, not by
        # option contract identity. A NULL position_id is safer than linking a
        # SPY 729C management envelope to the SPY equity inventory row.
        if is_option:
            return None

        state_clause = "AND state = 'open'" if open_only else ""
        row = db.execute(text(
            """
            SELECT id FROM trading_positions
            WHERE COALESCE(user_id, -1) = COALESCE(:uid, -1)
              AND broker_source = :broker
              AND account_type = :account_type
              AND ticker = :tkr
              AND direction = :direction
              {state_clause}
            ORDER BY
              CASE state WHEN 'open' THEN 0 ELSE 1 END,
              id DESC
            LIMIT 1
            """.format(state_clause=state_clause)
        ), {
            "uid": uid,
            "broker": broker,
            "account_type": acct,
            "tkr": tkr,
            "direction": dir_,
        }).first()
        return int(row[0]) if row else None
    except Exception:
        return None


def position_has_recorded_sell(
    db: Session,
    position_id: int | None,
) -> bool:
    """f-position-identity-phase-4 (2026-05-18): precise inverse-reconcile
    check — does the given position have at least one recorded SELL fill in
    ``trading_execution_events``?

    Phase 1's inverse-reconcile workaround used a per-trade_id event_count
    check (zero events on the dead trade_id → re-open). That was conservative
    because:
    - Fills get attached to whichever trade_id is active when they happen.
    - When a Trade row gets wrongly closed and recreated, fills associated
      with the prior trade_id are orphaned from the live position.
    - The count couldn't distinguish "no buy/sell fills" from "buy fills
      under a different trade_id".

    Phase 2 (mig 248) populated ``trading_execution_events.position_id`` so
    we can ask the precise question instead: across ALL Trade-row generations
    associated with this position, has the broker ever recorded a SELL fill?

    Returns False on:
    - ``position_id is None`` (caller falls back to old path)
    - Any DB exception (caller falls back to old path)
    - No matching rows

    Returns True only when at least one non-synthetic event row matches::

        position_id = :pid
        AND status = 'filled'
        AND lower(payload_json->>'side') = 'sell'

    Synthetic migration/reconcile rows are excluded. Those rows describe
    local bookkeeping repairs, not broker-confirmed exits, and must not block
    inverse reconcile when the broker still reports the position.

    The ``status='filled'`` guard excludes status='queued'/'pending'/etc
    where 'side' might also be 'sell' but no actual SELL fill happened
    (intent only). The ``payload_json->>'side'`` discriminator was
    verified live 2026-05-18 — Robinhood / Coinbase / autotrader paths
    all set ``side`` in the normalized payload.
    """
    if position_id is None:
        return False
    try:
        from sqlalchemy import text as _t
        row = db.execute(_t(
            """
            SELECT 1 FROM trading_execution_events
            WHERE position_id = :pid
              AND status = 'filled'
              AND LOWER(payload_json->>'side') = 'sell'
              AND COALESCE(LOWER(payload_json->>'source'), '') NOT IN (
                  'mig254_backfill'
              )
              AND COALESCE(LOWER(payload_json->>'exit_reason'), '') NOT IN (
                  'broker_reconcile_position_gone',
                  'broker_reconcile_no_exit_price',
                  'coinbase_position_sync_gone',
                  'forced_unwind_reconcile',
                  'zombie_reconcile_orphan'
              )
              AND COALESCE(LOWER(payload_json->>'synthetic'), 'false') NOT IN (
                  'true', '1', 'yes'
              )
            LIMIT 1
            """
        ), {"pid": int(position_id)}).first()
        return row is not None
    except Exception:
        return False
