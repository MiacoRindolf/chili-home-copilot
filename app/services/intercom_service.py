"""Intercom service: status management, voice message storage, consent."""
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import User, UserStatus, IntercomMessage, IntercomConsent

VOICE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "voice"
VOICE_DIR.mkdir(parents=True, exist_ok=True)


def get_user_status(user_id: int, db: Session) -> dict:
    """Return the current status for a user, creating a default if missing."""
    row = db.query(UserStatus).filter(UserStatus.user_id == user_id).first()
    if not row:
        row = UserStatus(user_id=user_id, status="available")
        db.add(row)
        db.commit()
        db.refresh(row)

    if row.status == "dnd" and row.dnd_until and datetime.utcnow() > row.dnd_until:
        row.status = "available"
        row.dnd_until = None
        db.commit()

    return {"status": row.status, "dnd_until": str(row.dnd_until) if row.dnd_until else None}


def set_user_status(user_id: int, status: str, dnd_minutes: int | None, db: Session) -> dict:
    """Update a user's availability status."""
    row = db.query(UserStatus).filter(UserStatus.user_id == user_id).first()
    if not row:
        row = UserStatus(user_id=user_id)
        db.add(row)

    row.status = status
    row.dnd_until = (datetime.utcnow() + timedelta(minutes=dnd_minutes)) if (status == "dnd" and dnd_minutes) else None
    row.updated_at = datetime.utcnow()
    db.commit()
    return get_user_status(user_id, db)


def get_all_statuses(db: Session) -> list[dict]:
    """Return status for all registered housemates."""
    users = db.query(User).all()
    results = []
    for u in users:
        st = get_user_status(u.id, db)
        results.append({"user_id": u.id, "name": u.name, "status": st["status"], "dnd_until": st["dnd_until"]})
    return results


def is_dnd(user_id: int, db: Session) -> bool:
    """Quick check whether a user is currently on DND."""
    st = get_user_status(user_id, db)
    return st["status"] == "dnd"


def save_voice_message(
    from_user_id: int | None,
    to_user_id: int | None,
    audio_bytes: bytes,
    duration_ms: int,
    is_broadcast: bool,
    db: Session,
) -> IntercomMessage:
    """Save audio to disk and create a DB record."""
    filename = f"{uuid.uuid4().hex}.webm"
    dest = VOICE_DIR / filename
    dest.write_bytes(audio_bytes)

    msg = IntercomMessage(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        is_broadcast=is_broadcast,
        audio_path=filename,
        duration_ms=duration_ms,
        delivered=(to_user_id is None or not is_dnd(to_user_id, db)) if to_user_id else True,
        read=False,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def get_unread_messages(user_id: int, db: Session) -> list[dict]:
    """Get unread voice messages for a user (direct + broadcast)."""
    from sqlalchemy import or_
    msgs = (
        db.query(IntercomMessage)
        .filter(
            or_(IntercomMessage.to_user_id == user_id, IntercomMessage.is_broadcast == True),
            or_(IntercomMessage.from_user_id != user_id, IntercomMessage.from_user_id == None),
            IntercomMessage.read == False,
        )
        .order_by(IntercomMessage.created_at.desc())
        .limit(50)
        .all()
    )
    return [_msg_to_dict(m, db) for m in msgs]


def get_all_messages(user_id: int, db: Session) -> list[dict]:
    """Get all voice messages relevant to a user."""
    msgs = (
        db.query(IntercomMessage)
        .filter(
            (IntercomMessage.to_user_id == user_id)
            | (IntercomMessage.from_user_id == user_id)
            | (IntercomMessage.is_broadcast == True)
        )
        .order_by(IntercomMessage.created_at.desc())
        .limit(100)
        .all()
    )
    return [_msg_to_dict(m, db) for m in msgs]


def mark_read(message_id: int, user_id: int, db: Session) -> bool:
    msg = db.query(IntercomMessage).filter(IntercomMessage.id == message_id).first()
    if not msg:
        return False
    if msg.to_user_id != user_id and not msg.is_broadcast:
        return False
    msg.read = True
    msg.delivered = True
    db.commit()
    return True


def has_consent(user_id: int, db: Session) -> bool:
    row = db.query(IntercomConsent).filter(IntercomConsent.user_id == user_id).first()
    return bool(row and not row.revoked_at)


def grant_consent(user_id: int, db: Session) -> None:
    row = db.query(IntercomConsent).filter(IntercomConsent.user_id == user_id).first()
    if row:
        row.revoked_at = None
        row.consented_at = datetime.utcnow()
    else:
        row = IntercomConsent(user_id=user_id)
        db.add(row)
    db.commit()


def revoke_consent(user_id: int, db: Session) -> None:
    row = db.query(IntercomConsent).filter(IntercomConsent.user_id == user_id).first()
    if row:
        row.revoked_at = datetime.utcnow()
        db.commit()


def delete_message(message_id: int, user_id: int, db: Session) -> bool:
    """Delete a voice message if the user is the recipient or the sender."""
    msg = db.query(IntercomMessage).filter(IntercomMessage.id == message_id).first()
    if not msg:
        return False
    if msg.to_user_id != user_id and msg.from_user_id != user_id:
        return False
    dest = VOICE_DIR / msg.audio_path
    if dest.exists():
        try:
            dest.unlink()
        except OSError:
            pass
    db.delete(msg)
    db.commit()
    return True


def _msg_to_dict(m: IntercomMessage, db: Session) -> dict:
    from_name = "CHILI"
    if m.from_user_id:
        u = db.query(User).filter(User.id == m.from_user_id).first()
        from_name = u.name if u else "Unknown"
    to_name = "Everyone" if m.is_broadcast else None
    if m.to_user_id and not m.is_broadcast:
        u = db.query(User).filter(User.id == m.to_user_id).first()
        to_name = u.name if u else "Unknown"
    return {
        "id": m.id,
        "from_user_id": m.from_user_id,
        "from_name": from_name,
        "to_user_id": m.to_user_id,
        "to_name": to_name,
        "is_broadcast": m.is_broadcast,
        "audio_path": m.audio_path,
        "duration_ms": m.duration_ms,
        "delivered": m.delivered,
        "read": m.read,
        "created_at": str(m.created_at),
    }
