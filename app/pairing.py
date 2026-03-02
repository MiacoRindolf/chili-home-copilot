import secrets
import random
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from .models import User, Device, PairCode

DEVICE_COOKIE_NAME = "chili_device_token"

def generate_pair_code(db: Session, user_id: int, minutes_valid: int = 10, numeric: bool = False) -> str:
    code = f"{random.randint(100000, 999999)}" if numeric else secrets.token_hex(4)
    pc = PairCode(
        code=code,
        user_id=user_id,
        expires_at=datetime.utcnow() + timedelta(minutes=minutes_valid),
        used=False,
    )
    db.add(pc)
    db.commit()
    return code

def redeem_pair_code(db: Session, code: str) -> PairCode | None:
    pc = db.query(PairCode).filter(PairCode.code == code).first()
    if not pc or pc.used:
        return None
    if datetime.utcnow() > pc.expires_at:
        return None
    pc.used = True
    db.commit()
    return pc

def register_device(db: Session, user_id: int, label: str, client_ip: str) -> str:
    token = secrets.token_hex(24)  # long random token
    dev = Device(token=token, user_id=user_id, label=label, client_ip_last=client_ip)
    db.add(dev)
    db.commit()
    return token

def get_identity(db: Session, device_token: str | None):
    """
    Returns (user_name, is_guest)
    """
    if not device_token:
        return ("Guest", True)

    dev = db.query(Device).filter(Device.token == device_token).first()
    if not dev:
        return ("Guest", True)

    user = db.query(User).filter(User.id == dev.user_id).first()
    return (user.name if user else "Guest", user is None)

def get_identity_record(db: Session, token: str | None):
    """
    Returns dict: {"user_id": int|None, "user_name": str, "is_guest": bool}
    """
    if not token:
        return {"user_id": None, "user_name": "Guest", "is_guest": True}

    dev = db.query(Device).filter(Device.token == token).first()
    if not dev:
        return {"user_id": None, "user_name": "Guest", "is_guest": True}

    user = db.query(User).filter(User.id == dev.user_id).first()
    if not user:
        return {"user_id": None, "user_name": "Guest", "is_guest": True}

    return {"user_id": user.id, "user_name": user.name, "is_guest": False}