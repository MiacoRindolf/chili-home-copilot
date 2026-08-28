"""DB-paper instance identity — determinism, canonicality, row authority.

Bahagi ng production admission ceremony (2026-08-28): ang IISANG deterministic
na db-paper account kada instance, minted mula sa current_database(), na ang
singleton row ang authority pagkatapos.

Runnable: pytest tests/test_db_paper_identity.py -v
"""
from __future__ import annotations

from sqlalchemy import text

from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    require_canonical_account_scope,
)
from app.services.trading.momentum_neural.db_paper_identity import (
    DB_PAPER_ACCOUNT_IDENTITY_SCHEMA_VERSION,
    DbPaperAccountIdentityError,
    canonical_db_paper_account_id,
    db_paper_account_identity_sha256,
    resolve_db_paper_account_binding,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    sha256_json,
)


def test_binding_is_deterministic_and_canonical(db):
    a = resolve_db_paper_account_binding(db)
    b = resolve_db_paper_account_binding(db)
    assert a == b
    assert a["account_scope"] == "db-paper:chili_test"
    assert a["account_identity_sha256"] == sha256_json(
        {"broker": "db_paper", "environment": "paper", "account_id": "chili_test"}
    )
    assert a["schema_version"] == DB_PAPER_ACCOUNT_IDENTITY_SCHEMA_VERSION
    assert require_canonical_account_scope(a["account_scope"]) == a["account_scope"]


def test_truncation_remint_is_byte_identical(db):
    first = resolve_db_paper_account_binding(db)
    db.execute(text("DELETE FROM db_paper_account_identity"))
    again = resolve_db_paper_account_binding(db)
    assert again == first


def test_row_is_the_authority_over_the_mint_input(db):
    resolve_db_paper_account_binding(db)
    db.execute(text(
        "UPDATE db_paper_account_identity SET account_id = 'renamed_db', "
        "account_identity_sha256 = :sha WHERE singleton_id = 1"
    ), {"sha": db_paper_account_identity_sha256("renamed_db")})
    got = resolve_db_paper_account_binding(db)
    assert got["account_scope"] == "db-paper:renamed_db"


def test_corrupt_row_sha_is_a_typed_failure_never_a_silent_heal(db):
    resolve_db_paper_account_binding(db)
    db.execute(text(
        "UPDATE db_paper_account_identity SET account_identity_sha256 = "
        "repeat('0', 64) WHERE singleton_id = 1"
    ))
    try:
        resolve_db_paper_account_binding(db)
        raise AssertionError("dapat nag-raise")
    except DbPaperAccountIdentityError as exc:
        assert "runbook" in str(exc)


def test_account_id_hygiene():
    assert canonical_db_paper_account_id("  Chili_Test ") == "chili_test"
    for junk in ("", "UPPER SPACE", "a" * 64, "semi;colon", None):
        try:
            canonical_db_paper_account_id(junk)
            raise AssertionError(f"dapat tinanggihan: {junk!r}")
        except DbPaperAccountIdentityError:
            pass
