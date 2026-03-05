"""Email service for sending pairing codes via Gmail SMTP."""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from .config import settings


def is_configured() -> bool:
    return bool(settings.email_user and settings.email_password)


def send_pairing_code(to_email: str, code: str, user_name: str) -> bool:
    """Send a pairing verification code. Returns True on success."""
    if not is_configured():
        return False

    subject = f"CHILI Pairing Code: {code}"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:400px;margin:0 auto;padding:20px;">
      <h2 style="color:#2563eb;">CHILI Home Copilot</h2>
      <p>Hi {user_name},</p>
      <p>Your device pairing code is:</p>
      <div style="font-size:2rem;font-weight:700;letter-spacing:.3em;text-align:center;
                  padding:16px;background:#f3f4f6;border-radius:8px;margin:16px 0;">
        {code}
      </div>
      <p style="color:#6b7280;font-size:.85rem;">
        This code expires in 10 minutes. Enter it on your device to link it to your account.
      </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_user
    msg["To"] = to_email
    msg.attach(MIMEText(f"Your CHILI pairing code is: {code}\nValid for 10 minutes.", "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.email_user, settings.email_password)
            server.sendmail(settings.email_user, to_email, msg.as_string())
        return True
    except Exception as e:
        from .logger import log_info
        log_info("email", f"send_failed to={to_email} error={e}")
        return False
