"""SMS notification service for CHILI trading alerts.

Dual-provider approach:
  1. Email-to-SMS gateway (free, uses existing Gmail SMTP config)
  2. Twilio (optional, if TWILIO_ACCOUNT_SID is set)
"""
from __future__ import annotations

import logging
import re
import smtplib
from email.mime.text import MIMEText

from ..config import settings

logger = logging.getLogger(__name__)

_CARRIER_GATEWAYS: dict[str, str] = {
    "verizon": "{number}@vtext.com",
    "att": "{number}@txt.att.net",
    "tmobile": "{number}@tmomail.net",
    "sprint": "{number}@messaging.sprintpcs.com",
    "uscellular": "{number}@email.uscc.net",
    "boost": "{number}@sms.myboostmobile.com",
    "cricket": "{number}@sms.cricketwireless.net",
    "metro": "{number}@mymetropcs.com",
    "mint": "{number}@tmomail.net",
    "visible": "{number}@vtext.com",
    "google_fi": "{number}@msg.fi.google.com",
    "xfinity": "{number}@vtext.com",
    "consumer_cellular": "{number}@mailmymobile.net",
}


def _clean_phone(raw: str) -> str:
    """Strip non-digit characters from a phone number."""
    return re.sub(r"\D", "", raw)


def is_configured() -> bool:
    return bool(settings.sms_phone) and (
        _has_twilio() or _has_email_gateway()
    )


def _has_twilio() -> bool:
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_phone_number
    )


def _has_email_gateway() -> bool:
    carrier = (settings.sms_carrier or "").lower().strip()
    return bool(
        settings.email_user
        and settings.email_password
        and carrier in _CARRIER_GATEWAYS
    )


def _send_via_twilio(message: str) -> bool:
    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        phone = _clean_phone(settings.sms_phone)
        if not phone.startswith("1"):
            phone = "1" + phone
        client.messages.create(
            body=message,
            from_=settings.twilio_phone_number,
            to=f"+{phone}",
        )
        logger.info(f"[sms] Sent via Twilio to +{phone}")
        return True
    except Exception as e:
        logger.error(f"[sms] Twilio send failed: {e}")
        return False


def _send_via_email_gateway(message: str) -> bool:
    carrier = (settings.sms_carrier or "").lower().strip()
    template = _CARRIER_GATEWAYS.get(carrier)
    if not template:
        logger.warning(f"[sms] Unknown carrier '{carrier}'")
        return False

    phone = _clean_phone(settings.sms_phone)
    to_addr = template.format(number=phone)

    msg = MIMEText(message)
    msg["From"] = settings.email_user
    msg["To"] = to_addr
    msg["Subject"] = "CHILI Alert"

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.email_user, settings.email_password)
            server.sendmail(settings.email_user, to_addr, msg.as_string())
        logger.info(f"[sms] Sent via email gateway to {to_addr}")
        return True
    except Exception as e:
        logger.error(f"[sms] Email gateway send failed: {e}")
        return False


def send_sms(message: str) -> bool:
    """Send an SMS alert. Returns True on success.

    Tries Twilio first (if configured), then falls back to email-to-SMS.
    """
    if not settings.alerts_enabled:
        return False
    if not settings.sms_phone:
        return False

    if _has_twilio():
        if _send_via_twilio(message):
            return True
        logger.warning("[sms] Twilio failed, trying email gateway fallback")

    if _has_email_gateway():
        return _send_via_email_gateway(message)

    logger.warning("[sms] No SMS provider configured")
    return False


def get_sms_status() -> dict:
    """Status info for the alerts settings UI."""
    phone = settings.sms_phone
    carrier = settings.sms_carrier
    return {
        "configured": is_configured(),
        "phone": phone[-4:] if phone and len(phone) >= 4 else "",
        "carrier": carrier,
        "provider": "twilio" if _has_twilio() else ("email_gateway" if _has_email_gateway() else "none"),
        "alerts_enabled": settings.alerts_enabled,
    }
