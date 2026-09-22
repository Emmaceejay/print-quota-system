"""Low-balance / over-quota notifications.

Delivery is pluggable (``email``, ``webhook``, ``none``) and every attempt is
written to ``alerts_log`` -- which also provides the cooldown, so a user who
prints thirty jobs while low does not get thirty emails.

A delivery failure is logged, never raised: accounting must not break
because an SMTP host is down.
"""

from __future__ import annotations

import datetime as dt
import json
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.logging import get_logger
from ..db.models import AlertLog, User, utcnow

log = get_logger("notifications")


def _recent_alert_exists(
    session: Session, username: str, alert_type: str, cooldown_hours: int
) -> bool:
    if cooldown_hours <= 0:
        return False
    since = utcnow() - dt.timedelta(hours=cooldown_hours)
    stmt = (
        select(AlertLog)
        .where(AlertLog.username == username)
        .where(AlertLog.alert_type == alert_type)
        .where(AlertLog.sent_at >= since)
    )
    return session.scalars(stmt).first() is not None


def _send_email(settings: Settings, to_address: str, subject: str, body: str) -> bool:
    smtp = settings.section("alerts").get("smtp", {})
    host = (smtp.get("host") or "").strip()
    if not host:
        log.debug("email alert skipped: no SMTP host configured")
        return False
    message = EmailMessage()
    message["From"] = smtp.get("from_address", "printquota@localhost")
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(smtp.get("port", 25)), timeout=10) as client:
            if smtp.get("use_tls"):
                client.starttls()
            if smtp.get("user"):
                client.login(smtp["user"], smtp.get("password", ""))
            client.send_message(message)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("email alert failed", extra={"to": to_address, "error": str(exc)})
        return False


def _send_webhook(settings: Settings, payload: dict) -> bool:
    url = (settings.get("alerts.webhook_url") or "").strip()
    if not url:
        log.debug("webhook alert skipped: no URL configured")
        return False
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("webhook alert failed", extra={"error": str(exc)})
        return False


def send_alert(
    session: Session,
    user: User,
    alert_type: str,
    subject: str,
    body: str,
    settings: Optional[Settings] = None,
    force: bool = False,
) -> Optional[AlertLog]:
    """Dispatch one alert, honouring the cooldown. Returns the log row."""
    settings = settings or get_settings()
    if not settings.get("alerts.enabled", True):
        return None
    channel = str(settings.get("alerts.channel", "email")).lower()
    if channel == "none":
        return None
    cooldown = int(settings.get("alerts.cooldown_hours", 24))
    if not force and _recent_alert_exists(session, user.username, alert_type, cooldown):
        return None

    delivered = False
    if channel == "email":
        if user.email:
            delivered = _send_email(settings, user.email, subject, body)
        else:
            log.debug("no email address on file", extra={"user": user.username})
    elif channel == "webhook":
        delivered = _send_webhook(
            settings,
            {
                "type": alert_type,
                "username": user.username,
                "display_name": user.display_name,
                "subject": subject,
                "message": body,
                "pages_used": user.pages_used,
                "quota_limit": user.quota_limit,
                "remaining": user.remaining,
            },
        )

    entry = AlertLog(
        username=user.username,
        alert_type=alert_type,
        channel=channel,
        message=body,
        delivered=delivered,
    )
    session.add(entry)
    log.info(
        "alert dispatched",
        extra={"user": user.username, "type": alert_type, "channel": channel, "delivered": delivered},
    )
    return entry


def notify_balance_state(
    session: Session, username: str, settings: Optional[Settings] = None
) -> Optional[AlertLog]:
    """Send a low-balance or over-quota alert if the user now qualifies."""
    settings = settings or get_settings()
    user = session.get(User, username)
    if user is None or not user.is_active:
        return None
    remaining = user.remaining

    if remaining <= 0:
        return send_alert(
            session,
            user,
            AlertLog.TYPE_OVER_QUOTA,
            "Print quota exhausted",
            (
                f"Hello {user.display_name or user.username},\n\n"
                f"You have used {user.pages_used} of your {user.quota_limit} page allowance "
                f"for the current period. Further print jobs will be denied until your quota "
                f"resets or an administrator raises it.\n"
            ),
            settings=settings,
        )
    if remaining <= user.low_balance_threshold:
        return send_alert(
            session,
            user,
            AlertLog.TYPE_LOW_BALANCE,
            "Print quota running low",
            (
                f"Hello {user.display_name or user.username},\n\n"
                f"You have {remaining} of {user.quota_limit} pages left for the current period.\n"
            ),
            settings=settings,
        )
    return None


def notify_repeated_denials(
    session: Session, username: str, count: int, settings: Optional[Settings] = None
) -> Optional[AlertLog]:
    """Flag a user who keeps hitting denials (usually a stuck client)."""
    settings = settings or get_settings()
    user = session.get(User, username)
    if user is None:
        return None
    return send_alert(
        session,
        user,
        AlertLog.TYPE_REPEATED_DENIAL,
        "Repeated print denials",
        f"{count} print jobs from {user.username} were denied in the last hour.",
        settings=settings,
    )
