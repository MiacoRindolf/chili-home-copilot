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

# ── Discord webhook ──────────────────────────────────────────────────

def _has_discord() -> bool:
    return bool(getattr(settings, "discord_webhook_url", None))


def _send_via_discord(message: str) -> bool:
    """Post to a Discord webhook. The message is sent as an embed for rich formatting."""
    url = getattr(settings, "discord_webhook_url", "")
    if not url:
        return False
    payload = json.dumps({
        "embeds": [{
            "title": "CHILI Trading Alert",
            "description": message[:4096],
            "color": 0x8B5CF6,
        }],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                logger.info("[alerts] Sent via Discord webhook")
                return True
            logger.warning("[alerts] Discord webhook returned %s", resp.status)
            return False
    except Exception as e:
        logger.error("[alerts] Discord webhook failed: %s", e)
        return False


# ── Web Push (PWA) ───────────────────────────────────────────────────

_push_subscriptions: list[dict] = []


def register_push_subscription(subscription: dict) -> None:
    """Store a Web Push subscription for later dispatch."""
    if subscription not in _push_subscriptions:
        _push_subscriptions.append(subscription)
        logger.info("[alerts] Registered push subscription (total: %d)", len(_push_subscriptions))


def _send_via_push(message: str) -> bool:
    """Send Web Push notifications to all registered subscriptions."""
    if not _push_subscriptions:
        return False
    vapid_key = getattr(settings, "vapid_private_key", "")
    vapid_email = getattr(settings, "vapid_contact_email", "")
    if not vapid_key:
        return False
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.debug("[alerts] pywebpush not installed — skipping push")
        return False

    sent = 0
    failed_subs: list[dict] = []
    for sub in _push_subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": "CHILI Alert", "body": message[:512]}),
                vapid_private_key=vapid_key,
                vapid_claims={"sub": f"mailto:{vapid_email}"} if vapid_email else {},
            )
            sent += 1
        except Exception as e:
            logger.debug("[alerts] Push to one subscription failed: %s", e)
            if "410" in str(e) or "404" in str(e):
                failed_subs.append(sub)
    for fs in failed_subs:
        try:
            _push_subscriptions.remove(fs)
        except ValueError:
            pass
    if sent:
        logger.info("[alerts] Sent push notification to %d/%d subscriptions", sent, len(_push_subscriptions) + len(failed_subs))
        return True
    return False


# ── Notification preferences ─────────────────────────────────────────

_DEFAULT_PREFS: dict[str, dict[str, bool]] = {
    "telegram": {"A": True, "B": True, "C": False},
    "sms": {"A": True, "B": False, "C": False},
    "discord": {"A": True, "B": True, "C": False},
    "push": {"A": True, "B": True, "C": True},
    "in_app": {"A": True, "B": True, "C": True},
}

_notification_prefs: dict[str, dict[str, bool]] = {}


def get_notification_preferences() -> dict[str, dict[str, bool]]:
    """Return current notification preferences (channel -> tier -> enabled)."""
    return {**_DEFAULT_PREFS, **_notification_prefs}


def set_notification_preferences(prefs: dict[str, dict[str, bool]]) -> None:
    """Update notification preferences."""
    global _notification_prefs
    _notification_prefs = prefs
    logger.info("[alerts] Updated notification preferences: %s", prefs)


def _should_send(channel: str, tier: str) -> bool:
    """Check if a notification should be sent for a given channel and tier."""
    combined = get_notification_preferences()
    ch_prefs = combined.get(channel, {})
    return ch_prefs.get(tier, False)


# ── Main send function ───────────────────────────────────────────────

def send_sms(message: str, tier: str = "A") -> bool:
    """Send an alert notification. Returns True if at least one channel succeeded.

    *tier* controls routing: "A" (highest) dispatches to all enabled channels,
    "B" to push + in-app, "C" to in-app only — unless overridden by preferences.
    """
    if not settings.alerts_enabled:
        return False

    any_sent = False

    if _has_telegram() and _should_send("telegram", tier):
        if _send_via_telegram(message):
            any_sent = True
        else:
            logger.warning("[alerts] Telegram failed")

    if _has_discord() and _should_send("discord", tier):
        if _send_via_discord(message):
            any_sent = True

    if _push_subscriptions and _should_send("push", tier):
        if _send_via_push(message):
            any_sent = True

    if settings.sms_phone and _should_send("sms", tier):
        if _has_twilio():
            if _send_via_twilio(message):
                any_sent = True
            else:
                logger.warning("[alerts] Twilio failed, trying email gateway")
                if _has_email_gateway() and _send_via_email_gateway(message):
                    any_sent = True
        elif _has_email_gateway():
            if _send_via_email_gateway(message):
                any_sent = True

    if not any_sent:
        logger.warning("[alerts] No alert provider delivered for tier=%s", tier)

    return any_sent


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
        "discord_configured": _has_discord(),
        "push_subscriptions": len(_push_subscriptions),
        "gateway_paused": backoff_remaining > 0,
        "gateway_resumes_in_s": backoff_remaining,
        "notification_preferences": get_notification_preferences(),
    }
