"""Mobile-specific API routes: pairing and future mobile helpers."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import User
from ..schemas import PairRequestBody, PairVerifyBody
from ..pairing import generate_pair_code, redeem_pair_code, register_device
from .. import email_service


router = APIRouter(prefix="/api/mobile", tags=["mobile"])


@router.post("/pair/request", response_class=JSONResponse)
def mobile_pair_request(body: PairRequestBody, db: Session = Depends(get_db)):
    """Request a pairing code for the mobile app via email.

    This mirrors /api/pair/request but is namespaced under /api/mobile and
    intended for native clients.
    """
    email = body.email.strip().lower()
    if not email:
        return JSONResponse({"ok": False, "error": "Email is required."}, status_code=400)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return JSONResponse(
            {
                "ok": False,
                "error": "This email isn't registered. Ask the admin to add you as a housemate.",
            },
            status_code=404,
        )

    code = generate_pair_code(db, user_id=user.id, minutes_valid=10, numeric=True)

    if email_service.is_configured():
        sent = email_service.send_pairing_code(email, code, user.name)
        if not sent:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Could not send email. Ask the admin to check email settings.",
                },
                status_code=500,
            )
        return JSONResponse({"ok": True, "message": f"Code sent to {email}."})

    # Email not configured -- return code directly (dev/local mode)
    return JSONResponse(
        {
            "ok": True,
            "message": f"Email not configured. Your code is: {code}",
            "dev_code": code,
        }
    )


@router.post("/pair/verify", response_class=JSONResponse)
def mobile_pair_verify(
    body: PairVerifyBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Verify a pairing code and issue a device token for mobile clients.

    Unlike /api/pair/verify, this returns the device token explicitly so the
    Flutter app can store it securely and send it as a Bearer token.
    """
    code = body.code.strip()
    label = body.label.strip() or "Unknown Device"

    pc = redeem_pair_code(db, code)
    if not pc:
        return JSONResponse(
            {
                "ok": False,
                "error": "Invalid or expired code. Request a new one.",
            },
            status_code=400,
        )

    client_ip = request.client.host
    token = register_device(db, user_id=pc.user_id, label=label, client_ip=client_ip)

    user = db.query(User).filter(User.id == pc.user_id).first()
    return JSONResponse(
        {
            "ok": True,
            "user_name": user.name if user else "Housemate",
            "token": token,
        }
    )

