"""Alert notification service for CHILI trading alerts.

Provider priority:
  1. Telegram Bot (free, no quota, instant — preferred)
  2. Twilio SMS (paid, reliable)
  3. Email-to-SMS gateway (free but Gmail rate-limited)

Includes rate-limit backoff for the email gateway: when Gmail returns 550
(daily limit exceeded), the gateway is paused to avoid hammering.
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
import time as _time
import urllib.request
import urllib.error
from email.mime.text import MIMEText

from ..config import settings

logger = logging.getLogger(__name__)

_gateway_backoff_until: float = 0.0
_GATEWAY_BACKOFF_SECS = 3600

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
    return re.sub(r"\D", "", raw)


# ── Provider checks ──────────────────────────────────────────────────

def _has_telegram() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


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


def is_configured() -> bool:
    return _has_telegram() or (
        bool(settings.sms_phone) and (_has_twilio() or _has_email_gateway())
    )


# ── Telegram ─────────────────────────────────────────────────────────

def _send_via_telegram(message: str) -> bool:
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                logger.info(f"[alerts] Sent via Telegram to chat {chat_id}")
                return True
            logger.warning(f"[alerts] Telegram API returned ok=false: {body}")
            return False
    except urllib.error.HTTPError as e:
        logger.error(f"[alerts] Telegram send failed (HTTP {e.code}): {e.read().decode()[:200]}")
        return False
    except Exception as e:
        logger.error(f"[alerts] Telegram send failed: {e}")
        return False


# ── Twilio ───────────────────────────────────────────────────────────

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
        logger.info(f"[alerts] Sent via Twilio to +{phone}")
        return True
    except Exception as e:
        logger.error(f"[alerts] Twilio send failed: {e}")
        return False


# ── Email-to-SMS gateway ────────────────────────────────────────────

def _send_via_email_gateway(message: str) -> bool:
    global _gateway_backoff_until

    if _time.time() < _gateway_backoff_until:
        remaining = int(_gateway_backoff_until - _time.time())
        logger.debug(f"[alerts] Email gateway paused (rate-limit backoff, {remaining}s left)")
        return False

    carrier = (settings.sms_carrier or "").lower().strip()
    template = _CARRIER_GATEWAYS.get(carrier)
    if not template:
        logger.warning(f"[alerts] Unknown carrier '{carrier}'")
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
        logger.info(f"[alerts] Sent via email gateway to {to_addr}")
        return True
    except smtplib.SMTPDataError as e:
        if e.smtp_code == 550 and b"sending limit" in (e.smtp_error or b""):
            _gateway_backoff_until = _time.time() + _GATEWAY_BACKOFF_SECS
            logger.warning(
                f"[alerts] Gmail daily sending limit hit — pausing email gateway for "
                f"{_GATEWAY_BACKOFF_SECS // 60} minutes"
            )
        else:
            logger.error(f"[alerts] Email gateway send failed: {e}")
        return False
    except Exception as e:
        logger.error(f"[alerts] Email gateway send failed: {e}")
        return False


# ── Main send function ───────────────────────────────────────────────

def send_sms(message: str) -> bool:
    """Send an alert notification. Returns True on success.

    Priority: Telegram -> Twilio -> Email-to-SMS gateway.
    """
    if not settings.alerts_enabled:
        return False

    if _has_telegram():
        if _send_via_telegram(message):
            return True
        logger.warning("[alerts] Telegram failed, trying fallback providers")

    if not settings.sms_phone:
        return False

    if _has_twilio():
        if _send_via_twilio(message):
            return True
        logger.warning("[alerts] Twilio failed, trying email gateway fallback")

    if _has_email_gateway():
        return _send_via_email_gateway(message)

    logger.warning("[alerts] No alert provider configured")
    return False


def get_sms_status() -> dict:
    """Status info for the alerts settings UI."""
    phone = settings.sms_phone
    carrier = settings.sms_carrier
    backoff_remaining = max(0, int(_gateway_backoff_until - _time.time()))

    if _has_telegram():
        provider = "telegram"
    elif _has_twilio():
        provider = "twilio"
    elif _has_email_gateway():
        provider = "email_gateway"
    else:
        provider = "none"

    return {
        "configured": is_configured(),
        "phone": phone[-4:] if phone and len(phone) >= 4 else "",
        "carrier": carrier,
        "provider": provider,
        "alerts_enabled": settings.alerts_enabled,
        "telegram_configured": _has_telegram(),
        "gateway_paused": backoff_remaining > 0,
        "gateway_resumes_in_s": backoff_remaining,
    }
