"""Encrypted credential storage for per-user broker secrets.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from
settings.session_secret via PBKDF2.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.orm import Session

from ..config import settings
from ..models.core import BrokerCredential

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None
_SALT = b"chili-broker-vault-v1"


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    secret = (settings.session_secret or "chili-default-key").encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=480_000)
    key = base64.urlsafe_b64encode(kdf.derive(secret))
    _fernet = Fernet(key)
    return _fernet


def encrypt_credentials(data: dict[str, Any]) -> str:
    f = _get_fernet()
    plaintext = json.dumps(data).encode()
    return f.encrypt(plaintext).decode()


def decrypt_credentials(token: str) -> dict[str, Any] | None:
    f = _get_fernet()
    try:
        plaintext = f.decrypt(token.encode())
        return json.loads(plaintext)
    except (InvalidToken, json.JSONDecodeError) as e:
        logger.error(f"[vault] Failed to decrypt credentials: {e}")
        return None


def save_broker_credentials(
    db: Session, user_id: int, broker: str, creds: dict[str, Any]
) -> BrokerCredential:
    """Upsert encrypted credentials for a user+broker pair."""
    encrypted = encrypt_credentials(creds)
    existing = (
        db.query(BrokerCredential)
        .filter(BrokerCredential.user_id == user_id, BrokerCredential.broker == broker)
        .first()
    )
    if existing:
        existing.encrypted_data = encrypted
        db.commit()
        db.refresh(existing)
        return existing

    rec = BrokerCredential(user_id=user_id, broker=broker, encrypted_data=encrypted)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def get_broker_credentials(
    db: Session, user_id: int, broker: str
) -> dict[str, Any] | None:
    """Load and decrypt credentials for a user+broker pair. Returns None if not found."""
    rec = (
        db.query(BrokerCredential)
        .filter(BrokerCredential.user_id == user_id, BrokerCredential.broker == broker)
        .first()
    )
    if not rec:
        return None
    return decrypt_credentials(rec.encrypted_data)


def has_broker_credentials(db: Session, user_id: int, broker: str) -> bool:
    """Check if credentials exist without decrypting."""
    return (
        db.query(BrokerCredential)
        .filter(BrokerCredential.user_id == user_id, BrokerCredential.broker == broker)
        .first()
    ) is not None


def delete_broker_credentials(db: Session, user_id: int, broker: str) -> bool:
    """Remove credentials for a user+broker pair."""
    rec = (
        db.query(BrokerCredential)
        .filter(BrokerCredential.user_id == user_id, BrokerCredential.broker == broker)
        .first()
    )
    if rec:
        db.delete(rec)
        db.commit()
        return True
    return False


def broker_identity_from_credentials(
    broker: str,
    creds: dict[str, Any] | None,
) -> str | None:
    """Best-effort stable identity for duplicate-account prevention."""
    if not isinstance(creds, dict):
        return None
    broker_key = (broker or "").strip().lower()
    if broker_key == "robinhood":
        username = str(creds.get("username") or "").strip().lower()
        return username or None
    if broker_key == "coinbase":
        api_key = str(creds.get("api_key") or "").strip()
        return api_key or None
    return None


def iter_broker_credentials_with_identity(
    db: Session,
    broker: str,
) -> list[tuple[BrokerCredential, dict[str, Any] | None, str | None]]:
    """Decrypt all credentials for one broker with a resolved identity key."""
    rows = (
        db.query(BrokerCredential)
        .filter(BrokerCredential.broker == broker)
        .order_by(BrokerCredential.updated_at.desc(), BrokerCredential.id.desc())
        .all()
    )
    out: list[tuple[BrokerCredential, dict[str, Any] | None, str | None]] = []
    for row in rows:
        data = decrypt_credentials(row.encrypted_data)
        out.append((row, data, broker_identity_from_credentials(broker, data)))
    return out


def find_users_with_broker_identity(
    db: Session,
    broker: str,
    identity: str | None,
) -> list[int]:
    key = str(identity or "").strip().lower()
    if not key:
        return []
    seen: set[int] = set()
    user_ids: list[int] = []
    for row, _data, row_identity in iter_broker_credentials_with_identity(db, broker):
        if row_identity != key or int(row.user_id) in seen:
            continue
        seen.add(int(row.user_id))
        user_ids.append(int(row.user_id))
    return user_ids


# ── Key Rotation ──────────────────────────────────────────────────────


def rotate_encryption_key(
    db: Session,
    new_secret: str | None = None,
) -> dict[str, Any]:
    """Re-encrypt all stored credentials with a new key.

    If *new_secret* is None, uses the current settings.session_secret
    (useful for re-deriving after config change).

    Steps:
    1. Decrypt all credentials with the current key
    2. Derive a new Fernet instance from *new_secret*
    3. Re-encrypt all credentials
    4. Update the global _fernet instance
    """
    global _fernet

    all_creds = db.query(BrokerCredential).all()
    if not all_creds:
        return {"ok": True, "rotated": 0, "message": "No credentials to rotate"}

    # Decrypt with current key
    decrypted: list[tuple[BrokerCredential, dict]] = []
    failed = 0
    for rec in all_creds:
        data = decrypt_credentials(rec.encrypted_data)
        if data is not None:
            decrypted.append((rec, data))
        else:
            failed += 1
            logger.warning(
                "[vault] Failed to decrypt cred for user=%s broker=%s during rotation",
                rec.user_id, rec.broker,
            )

    # Derive new key
    secret_bytes = (new_secret or settings.session_secret or "chili-default-key").encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=480_000)
    new_key = base64.urlsafe_b64encode(kdf.derive(secret_bytes))
    new_fernet = Fernet(new_key)

    # Re-encrypt
    for rec, data in decrypted:
        plaintext = json.dumps(data).encode()
        rec.encrypted_data = new_fernet.encrypt(plaintext).decode()

    db.commit()
    _fernet = new_fernet

    logger.info(
        "[vault] Key rotation complete: %d credentials rotated, %d failed",
        len(decrypted), failed,
    )
    return {
        "ok": True,
        "rotated": len(decrypted),
        "failed": failed,
    }


def get_vault_health(db: Session) -> dict[str, Any]:
    """Check vault health: can we decrypt all stored credentials?"""
    all_creds = db.query(BrokerCredential).all()
    healthy = 0
    corrupted = 0
    for rec in all_creds:
        data = decrypt_credentials(rec.encrypted_data)
        if data is not None:
            healthy += 1
        else:
            corrupted += 1

    return {
        "total_credentials": len(all_creds),
        "healthy": healthy,
        "corrupted": corrupted,
        "salt_version": "v1",
        "kdf_iterations": 480_000,
    }
