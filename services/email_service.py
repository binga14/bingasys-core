from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

from config import settings

logger = logging.getLogger(__name__)


def build_password_reset_link(token: str) -> str:
    query = urlencode({"token": token})
    return f"{settings.frontend_url.rstrip('/')}/reset-password?{query}"


def send_password_reset_email(email: str, reset_link: str) -> None:
    if not settings.smtp_host:
        logger.info("Password reset link for %s: %s", email, reset_link)
        return

    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = email
    message["Subject"] = "Reset your Bingasys password"
    message.set_content(
        "\n".join(
            [
                "Use this link to reset your Bingasys password:",
                reset_link,
                "",
                "This link expires soon. If you did not request it, ignore this email.",
            ]
        )
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
