"""Failure notifications. Add a channel by writing a function and registering it."""

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol, get_args

import httpx

from backy.config import NotifyChannel, Settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
RESEND_URL = "https://api.resend.com/emails"


class Notifier(Protocol):
    def __call__(self, settings: Settings, subject: str, body: str) -> None: ...


def _webhook(settings: Settings, subject: str, body: str) -> None:
    """Generic JSON POST. Also covers Coolify, Discord, Teams and ntfy via the URL alone."""
    assert settings.webhook_url  # guaranteed by Settings validation
    response = httpx.post(
        settings.webhook_url,
        json={"subject": subject, "body": body, "database": settings.db_name},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _slack(settings: Settings, subject: str, body: str) -> None:
    assert settings.slack_webhook_url
    response = httpx.post(
        settings.slack_webhook_url,
        json={"text": f"*{subject}*\n```{body}```"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _smtp(settings: Settings, subject: str, body: str) -> None:
    assert settings.smtp_host and settings.smtp_from and settings.smtp_to
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = settings.smtp_to  # a comma-separated list works as-is
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_SECONDS) as server:
        if settings.smtp_starttls:
            server.starttls()
        if settings.smtp_user and settings.smtp_password:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)


def _resend(settings: Settings, subject: str, body: str) -> None:
    """Resend's HTTP API -- SMTP without an SMTP server."""
    assert settings.resend_api_key and settings.resend_from and settings.resend_to
    response = httpx.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={
            "from": settings.resend_from,
            "to": [address.strip() for address in settings.resend_to.split(",")],
            "subject": subject,
            "text": body,
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


NOTIFIERS: dict[NotifyChannel, Notifier] = {
    "webhook": _webhook,
    "slack": _slack,
    "smtp": _smtp,
    "resend": _resend,
}

# Fails at import if a channel is added to the Literal without an implementation.
assert set(get_args(NotifyChannel)) == NOTIFIERS.keys(), "every NotifyChannel needs a notifier"


def notify_all(settings: Settings, subject: str, body: str) -> None:
    """Send to every configured channel. Never raises."""
    for channel in settings.notify_channels:
        try:
            NOTIFIERS[channel](settings, subject, body)
            log.info("notified via %s", channel)
        except Exception:
            # A broken notifier must never replace the failure it was sent to report.
            log.exception("notifier %r failed", channel)
