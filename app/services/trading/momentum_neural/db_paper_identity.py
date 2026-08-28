"""Canonical non-secret identity for the instance-local DB-paper account.

Ang DB-paper lane ay isang purong-simulasyon na execution surface: walang
broker, ang "account" ay ang instance mismo. Kaya ang identity ay DETERMINISTIC
mula sa materyales ng DB — ang parehong database ay laging parehong account,
at ang chili / chili_test / chili_staging ay awtomatikong magkakahiwalay na
scope. Sinasalamin nito ang tatlong-field na disiplina ng
``alpaca_paper_identity`` habang mahigpit na HIWALAY ang authority scope:
ang ``db-paper:`` prefix ay ipinapatupad ng bawat consumer (paper_runner,
adaptive_risk_request_builder) at hindi kailanman nagsasalubong sa
``alpaca:paper``.

Ang SINGLETON ROW sa ``db_paper_account_identity`` ang authority: ang
``current_database()`` ay ginagamit LAMANG sa unang mint; pagkatapos ang row
ang totoo (ligtas ang DB rename). Ang sirang row ay typed failure — hindi
kailanman tahimik na "hinihilom" (tingnan ang restore-storm risk sa blueprint).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import text

from .adaptive_risk_account_lock import require_canonical_account_scope
from .replay_capture_contract import sha256_json


DB_PAPER_ACCOUNT_SCOPE_PREFIX = "db-paper:"
DB_PAPER_ACCOUNT_IDENTITY_SCHEMA_VERSION = "chili.db-paper-account-identity.v1"

_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_-]{1,63}$")


class DbPaperAccountIdentityError(ValueError):
    """The supplied value cannot identify the instance DB-paper account."""


def canonical_db_paper_account_id(value: object) -> str:
    """Return one canonical lower-case account id or fail closed."""

    raw = str(value or "").strip().lower()
    if not _ACCOUNT_ID_RE.fullmatch(raw):
        raise DbPaperAccountIdentityError(
            "DB-paper account id must match ^[a-z0-9_-]{1,63}$"
        )
    return raw


def db_paper_account_identity_payload(account_id: object) -> Mapping[str, str]:
    """Return the exact immutable payload every db-paper subsystem hashes."""

    return MappingProxyType(
        {
            "broker": "db_paper",
            "environment": "paper",
            "account_id": canonical_db_paper_account_id(account_id),
        }
    )


def db_paper_account_identity_sha256(account_id: object) -> str:
    """Deterministic 64-hex identity digest over the canonical payload."""

    return sha256_json(dict(db_paper_account_identity_payload(account_id)))


def db_paper_account_scope(account_id: object) -> str:
    """Ang lock-domain scope: ``db-paper:<account_id>`` (canonical text)."""

    scope = DB_PAPER_ACCOUNT_SCOPE_PREFIX + canonical_db_paper_account_id(
        account_id
    )
    return require_canonical_account_scope(scope)


def _mint_account_id(db: Any) -> str:
    row = db.execute(text("SELECT current_database()")).fetchone()
    if row is None or not row[0]:
        raise DbPaperAccountIdentityError(
            "current_database() unavailable for db-paper identity mint"
        )
    return canonical_db_paper_account_id(row[0])


def resolve_db_paper_account_binding(db: Any) -> dict[str, str]:
    """Get-or-create ang IISANG instance account; ibalik ang session binding.

    Ang balik: ``{"account_scope", "account_identity_sha256", "schema_version"}``
    — ang eksaktong hugis na binabasa ng ``paper_runner._db_paper_account_binding``
    (tinatanggap nito ang dagdag na ``schema_version`` bilang audit key).
    Concurrency-safe sa pamamagitan ng ``ON CONFLICT DO NOTHING`` + muling
    SELECT: ang nanalo sa insert ang authority para sa lahat.
    """

    row = db.execute(
        text(
            "SELECT account_id, account_identity_sha256 "
            "FROM db_paper_account_identity WHERE singleton_id = 1"
        )
    ).fetchone()
    if row is None:
        account_id = _mint_account_id(db)
        db.execute(
            text(
                "INSERT INTO db_paper_account_identity "
                "(singleton_id, account_id, account_identity_sha256, "
                " schema_version, created_at) "
                "VALUES (1, :aid, :sha, :ver, :at) "
                "ON CONFLICT (singleton_id) DO NOTHING"
            ),
            {
                "aid": account_id,
                "sha": db_paper_account_identity_sha256(account_id),
                "ver": DB_PAPER_ACCOUNT_IDENTITY_SCHEMA_VERSION,
                "at": datetime.now(timezone.utc),
            },
        )
        row = db.execute(
            text(
                "SELECT account_id, account_identity_sha256 "
                "FROM db_paper_account_identity WHERE singleton_id = 1"
            )
        ).fetchone()
    if row is None:
        raise DbPaperAccountIdentityError(
            "db_paper_account_identity singleton unavailable after mint"
        )
    account_id = canonical_db_paper_account_id(row[0])
    stored_sha = str(row[1] or "").strip().lower()
    expected_sha = db_paper_account_identity_sha256(account_id)
    if stored_sha != expected_sha:
        # ANG ROW ANG AUTHORITY, pero ang sirang sha ay hindi hinihilom nang
        # tahimik — typed failure para makita ng operator (restore runbook).
        raise DbPaperAccountIdentityError(
            "db_paper_account_identity sha mismatch — operator runbook required"
        )
    return {
        "account_scope": db_paper_account_scope(account_id),
        "account_identity_sha256": expected_sha,
        "schema_version": DB_PAPER_ACCOUNT_IDENTITY_SCHEMA_VERSION,
    }
