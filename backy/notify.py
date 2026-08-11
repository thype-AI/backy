"""Failure notifications. Add a channel by writing a function and registering it."""

import json
import logging
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol, get_args

import httpx

from backy.config import NotifyChannel, Settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
RESEND_URL = "https://api.resend.com/emails"


class Notifier(Protocol):
    def __call__(self, settings: Settings, database: str, subject: str, body: str) -> None: ...


def _webhook(settings: Settings, database: str, subject: str, body: str) -> None:
    """Generic JSON POST. Also covers Coolify, Discord, Teams and ntfy via the URL alone."""
    assert settings.webhook_url  # guaranteed by Settings validation
    response = httpx.post(
        settings.webhook_url,
        json={"subject": subject, "body": body, "database": database},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _slack(settings: Settings, database: str, subject: str, body: str) -> None:
    del database  # the name is already in the subject
    assert settings.slack_webhook_url
    response = httpx.post(
        settings.slack_webhook_url,
        json={"text": f"*{subject}*\n```{body}```"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _smtp(settings: Settings, database: str, subject: str, body: str) -> None:
    del database  # the name is already in the subject
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


def _resend(settings: Settings, database: str, subject: str, body: str) -> None:
    """Resend's HTTP API -- SMTP without an SMTP server."""
    del database  # the name is already in the subject
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


def _read_state(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        # Unwritable state must never eat a notification -- it only disables the debounce.
        log.warning("cannot write %s; debounce will not persist (mount a volume there)", path)


def _debounced(settings: Settings, key: str) -> bool:
    last = _read_state(settings.notify_state_file).get(key)
    if not last:
        return False
    try:
        sent = datetime.fromisoformat(last)
    except ValueError:
        return False
    return datetime.now(UTC) - sent < timedelta(minutes=settings.notify_debounce_minutes)


def clear_debounce(settings: Settings, key: str) -> None:
    """Forget a debounce key so the next failure alerts immediately. Never raises."""
    state = _read_state(settings.notify_state_file)
    if state.pop(key, None) is not None:
        _write_state(settings.notify_state_file, state)


def notify_all(
    settings: Settings,
    database: str,
    subject: str,
    body: str,
    debounce_key: str | None = None,
) -> None:
    """Send to every configured channel. Never raises.

    With a debounce_key and NOTIFY_DEBOUNCE_MINUTES > 0, repeats of the same key within
    the window are suppressed -- persisted to notify_state_file so it survives restarts.
    """
    if debounce_key and settings.notify_debounce_minutes:
        if _debounced(settings, debounce_key):
            log.info(
                "suppressed %r (already notified within the last %s min)",
                subject,
                settings.notify_debounce_minutes,
            )
            return
        state = _read_state(settings.notify_state_file)
        state[debounce_key] = datetime.now(UTC).isoformat()
        _write_state(settings.notify_state_file, state)
    for channel in settings.notify_channels:
        try:
            NOTIFIERS[channel](settings, database, subject, body)
            log.info("notified via %s", channel)
        except Exception:
            # A broken notifier must never replace the failure it was sent to report.
            log.exception("notifier %r failed", channel)
