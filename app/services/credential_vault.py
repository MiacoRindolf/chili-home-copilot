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
