"""Canonical broker-account ownership repair and duplicate-account guardrails."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models.core import BrokerCredential, Device, User
from ...models.trading import Trade
from ..credential_vault import (
    broker_identity_from_credentials,
    find_users_with_broker_identity,
    get_broker_credentials,
    iter_broker_credentials_with_identity,
)
from .broker_position_sync import _merge_duplicate_into_canonical, _trade_rank_key
from .management_scope import (
    MANAGEMENT_SCOPE_BROKER_SYNC,
    MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY,
    infer_trade_management_scope_from_fields,
)

logger = logging.getLogger(__name__)


def _guest_like_user(user: User | None) -> bool:
    if user is None:
        return False
    if user.email:
        return False
    name = str(user.name or "").strip().lower()
    return name.startswith("trader-")


def _choose_canonical_user_id(
    db: Session,
    user_ids: list[int],
    *,
    preferred_user_id: int | None = None,
) -> int | None:
    ids = [int(uid) for uid in user_ids if uid is not None]
    if not ids:
        return None
    if preferred_user_id is not None and int(preferred_user_id) in ids:
        return int(preferred_user_id)

    users = {
        int(u.id): u
        for u in db.query(User).filter(User.id.in_(ids)).all()
    }
    email_ids = sorted(
        uid for uid in ids
        if users.get(uid) is not None and bool(users[uid].email)
    )
    if email_ids:
        return int(email_ids[0])
    named_ids = sorted(
        uid for uid in ids
        if users.get(uid) is not None and not _guest_like_user(users[uid])
    )
    if named_ids:
        return int(named_ids[0])
    return int(sorted(ids)[0])


def resolve_canonical_broker_user(
    db: Session,
    *,
    broker: str,
    creds: dict[str, Any] | None = None,
    broker_identity: str | None = None,
    preferred_user_id: int | None = None,
) -> dict[str, Any]:
    """Resolve the canonical local user for a broker identity."""
    identity = (
        broker_identity_from_credentials(broker, creds)
        if broker_identity is None
        else str(broker_identity or "").strip().lower()
    )
    if not identity and preferred_user_id is not None:
        preferred_creds = get_broker_credentials(db, int(preferred_user_id), broker)
        identity = broker_identity_from_credentials(broker, preferred_creds)
    if not identity:
        identities = sorted(_count_broker_identities(db, broker))
        if len(identities) == 1:
            identity = identities[0]
    if not identity:
        return {
            "broker": broker,
            "identity": None,
            "canonical_user_id": preferred_user_id,
            "user_ids": [int(preferred_user_id)] if preferred_user_id is not None else [],
            "duplicate_user_ids": [],
        }

    user_ids = find_users_with_broker_identity(db, broker, identity)
    if preferred_user_id is not None and int(preferred_user_id) not in user_ids:
        user_ids.append(int(preferred_user_id))
    canonical_user_id = _choose_canonical_user_id(
        db,
        user_ids,
        preferred_user_id=preferred_user_id,
    )
    duplicate_user_ids = [
        int(uid)
        for uid in user_ids
        if canonical_user_id is not None and int(uid) != int(canonical_user_id)
    ]
    return {
        "broker": broker,
        "identity": identity,
        "canonical_user_id": canonical_user_id,
        "user_ids": sorted(set(int(uid) for uid in user_ids)),
        "duplicate_user_ids": sorted(set(duplicate_user_ids)),
    }


def repoint_device_token_to_user(
    db: Session,
    *,
    device_token: str | None,
    canonical_user_id: int | None,
) -> bool:
    token = str(device_token or "").strip()
    if not token or canonical_user_id is None:
        return False
    row = db.query(Device).filter(Device.token == token).first()
    if row is None or int(row.user_id) == int(canonical_user_id):
        return False
    row.user_id = int(canonical_user_id)
    db.commit()
    return True


def _append_note(existing: str | None, line: str) -> str:
    cur = (existing or "").rstrip()
    if line in cur:
        return cur
    if not cur:
        return line
    return f"{cur}\n{line}"


def _reconcile_open_trades(
    db: Session,
    *,
    broker: str,
    canonical_user_id: int,
    user_ids: list[int],
    preview_only: bool,
) -> dict[str, int]:
    now = datetime.utcnow()
    rows = (
        db.query(Trade)
        .filter(
            Trade.broker_source == broker,
            Trade.status == "open",
            Trade.user_id.in_(user_ids),
        )
        .order_by(Trade.entry_date.asc(), Trade.id.asc())
        .all()
    )
    by_ticker: dict[str, list[Trade]] = defaultdict(list)
    for trade in rows:
        ticker = (trade.ticker or "").strip().upper()
        if ticker:
            by_ticker[ticker].append(trade)

    moved = 0
    deduped = 0
    groups = 0
    for _ticker, trades in by_ticker.items():
        ordered = sorted(
            trades,
            key=lambda t: ((1 if int(t.user_id or 0) == int(canonical_user_id) else 0),) + _trade_rank_key(t),
            reverse=True,
        )
        canonical = ordered[0]
        duplicates = ordered[1:]
        if len(ordered) > 1:
            groups += 1
        if preview_only:
            if int(canonical.user_id or 0) != int(canonical_user_id):
                moved += 1
            deduped += len(duplicates)
            continue

        if int(canonical.user_id or 0) != int(canonical_user_id):
            canonical.user_id = int(canonical_user_id)
            moved += 1
        canonical.management_scope = infer_trade_management_scope_from_fields(
            management_scope=canonical.management_scope,
            auto_trader_version=canonical.auto_trader_version,
            broker_source=canonical.broker_source,
            tags=canonical.tags,
        )
        for duplicate in duplicates:
            _merge_duplicate_into_canonical(canonical, duplicate)
            duplicate.user_id = int(canonical_user_id)
            duplicate.management_scope = MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY
            duplicate.status = "cancelled"
            duplicate.exit_date = now
            duplicate.exit_reason = "sync_duplicate_cross_user"
            duplicate.notes = _append_note(
                duplicate.notes,
                (
                    "Cancelled as duplicate mirrored broker sync row during "
                    f"canonical {broker} account repair at {now.isoformat()}."
                ),
            )
            deduped += 1
        if duplicates:
            canonical.notes = _append_note(
                canonical.notes,
                (
                    "Merged mirrored broker sync rows into canonical user "
                    f"{canonical_user_id} during account repair at {now.isoformat()}."
                ),
            )
    return {"open_trade_groups": groups, "open_trades_moved": moved, "open_trades_deduped": deduped}


def _count_broker_identities(db: Session, broker: str) -> set[str]:
    return {
        identity
        for _row, _data, identity in iter_broker_credentials_with_identity(db, broker)
        if identity
    }


def _reconcile_closed_history(
    db: Session,
    *,
    broker: str,
    canonical_user_id: int,
    duplicate_user_ids: list[int],
    preview_only: bool,
    allow_null_user_backfill: bool,
) -> dict[str, int]:
    moved_duplicate_rows = 0
    backfilled_null_rows = 0
    quarantined_null_rows = 0

    dupe_rows = (
        db.query(Trade)
        .filter(
            Trade.broker_source == broker,
            Trade.user_id.in_(duplicate_user_ids) if duplicate_user_ids else False,
        )
        .all()
        if duplicate_user_ids
        else []
    )
    for trade in dupe_rows:
        if preview_only:
            moved_duplicate_rows += 1
            continue
        trade.user_id = int(canonical_user_id)
        if not trade.management_scope:
            trade.management_scope = MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY
        moved_duplicate_rows += 1

    null_rows = (
        db.query(Trade)
        .filter(
            Trade.broker_source == broker,
            Trade.user_id.is_(None),
            Trade.status.in_(["closed", "cancelled", "rejected"]),
        )
        .all()
    )
    for trade in null_rows:
        if not allow_null_user_backfill:
            quarantined_null_rows += 1
            continue
        if preview_only:
            backfilled_null_rows += 1
            continue
        trade.user_id = int(canonical_user_id)
        if not trade.management_scope:
            trade.management_scope = MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY
        backfilled_null_rows += 1

    return {
        "closed_rows_moved": moved_duplicate_rows,
        "closed_null_rows_backfilled": backfilled_null_rows,
        "closed_null_rows_quarantined": quarantined_null_rows,
    }


def _repair_credentials_and_devices(
    db: Session,
    *,
    broker: str,
    identity: str | None,
    canonical_user_id: int,
    duplicate_user_ids: list[int],
    preview_only: bool,
) -> dict[str, int]:
    rows = [
        row
        for row, _data, row_identity in iter_broker_credentials_with_identity(db, broker)
        if identity and row_identity == identity
    ]
    creds_moved = 0
    creds_deleted = 0
    devices_repointed = 0

    if rows and not preview_only:
        canonical_rows = [row for row in rows if int(row.user_id) == int(canonical_user_id)]
        canonical_row = canonical_rows[0] if canonical_rows else None
        if canonical_row is None:
            keeper = rows[0]
            keeper.user_id = int(canonical_user_id)
            canonical_row = keeper
            creds_moved += 1
        for row in rows:
            if row.id == canonical_row.id:
                continue
            db.delete(row)
            creds_deleted += 1

    if duplicate_user_ids:
        guest_devices = (
            db.query(Device)
            .join(User, User.id == Device.user_id)
            .filter(Device.user_id.in_(duplicate_user_ids))
            .all()
        )
        for device in guest_devices:
            if not _guest_like_user(device.user):
                continue
            if preview_only:
                devices_repointed += 1
                continue
            device.user_id = int(canonical_user_id)
            devices_repointed += 1

    return {
        "credentials_moved": creds_moved,
        "credentials_deleted": creds_deleted,
        "devices_repointed": devices_repointed,
    }


def repair_broker_account_truth(
    db: Session,
    *,
    broker: str = "robinhood",
    creds: dict[str, Any] | None = None,
    broker_identity: str | None = None,
    canonical_user_id: int | None = None,
    preview_only: bool = False,
    evaluate_open_positions: bool = False,
) -> dict[str, Any]:
    """Collapse duplicated broker-linked users into one canonical account owner."""
    preferred_user_id = canonical_user_id
    if preferred_user_id is None:
        preferred_user_id = getattr(settings, "chili_autotrader_user_id", None)
    resolved = resolve_canonical_broker_user(
        db,
        broker=broker,
        creds=creds,
        broker_identity=broker_identity,
        preferred_user_id=preferred_user_id,
    )
    canonical = resolved.get("canonical_user_id")
    user_ids = list(resolved.get("user_ids") or [])
    duplicate_user_ids = list(resolved.get("duplicate_user_ids") or [])
    identity = resolved.get("identity")

    if canonical is None:
        return {
            "ok": False,
            "preview_only": preview_only,
            "reason": "no_canonical_user",
            **resolved,
        }

    if canonical not in user_ids:
        user_ids.append(int(canonical))
    if not duplicate_user_ids and len(user_ids) <= 1:
        return {
            "ok": True,
            "preview_only": preview_only,
            **resolved,
            "open_trade_groups": 0,
            "open_trades_moved": 0,
            "open_trades_deduped": 0,
            "closed_rows_moved": 0,
            "closed_null_rows_backfilled": 0,
            "closed_null_rows_quarantined": 0,
            "credentials_moved": 0,
            "credentials_deleted": 0,
            "devices_repointed": 0,
            "evaluated_open_positions": False,
        }

    identity_count = len(_count_broker_identities(db, broker))
    allow_null_user_backfill = bool(identity) and identity_count <= 1

    out: dict[str, Any] = {
        "ok": True,
        "preview_only": preview_only,
        **resolved,
        "allow_null_user_backfill": allow_null_user_backfill,
    }
    out.update(
        _reconcile_open_trades(
            db,
            broker=broker,
            canonical_user_id=int(canonical),
            user_ids=sorted(set(int(uid) for uid in user_ids)),
            preview_only=preview_only,
        )
    )
    out.update(
        _reconcile_closed_history(
            db,
            broker=broker,
            canonical_user_id=int(canonical),
            duplicate_user_ids=duplicate_user_ids,
            preview_only=preview_only,
            allow_null_user_backfill=allow_null_user_backfill,
        )
    )
    out.update(
        _repair_credentials_and_devices(
            db,
            broker=broker,
            identity=identity,
            canonical_user_id=int(canonical),
            duplicate_user_ids=duplicate_user_ids,
            preview_only=preview_only,
        )
    )

    if preview_only:
        out["evaluated_open_positions"] = False
        return out

    db.commit()

    evaluated = False
    if evaluate_open_positions:
        try:
            from .position_plan_generator import generate_position_plans

            generate_position_plans(db, int(canonical), force_refresh=True)
            evaluated = True
        except Exception:
            logger.warning(
                "[broker_account_repair] AI Evaluate All replay failed for canonical user=%s",
                canonical,
                exc_info=True,
            )
    out["evaluated_open_positions"] = evaluated
    return out
