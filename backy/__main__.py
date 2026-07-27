"""Entrypoint: run one backup, then exit. 0 = uploaded, 1 = failed, 2 = misconfigured.

Scheduling lives outside the container (Coolify scheduled task, host cron, k8s CronJob) so
there is no cron daemon in here and the exit code is the whole success signal.
"""

import logging
import sys
import tempfile
import traceback
from pathlib import Path

from pydantic import ValidationError

from backy.config import Settings
from backy.dump import blob_name, dump_database
from backy.notify import clear_debounce, notify_all
from backy.storage import get_storage

log = logging.getLogger("backy")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISCONFIGURED = 2


def run_backup(settings: Settings) -> str:
    """Dump, upload, return the blob URL. The temp dir cleans up the dump for us."""
    with tempfile.TemporaryDirectory(prefix="backy-") as tmp:
        dump_path = dump_database(settings, Path(tmp))
        return get_storage(settings).upload(dump_path, blob_name(settings, dump_path))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    try:
        settings = Settings()  # pyrefly: ignore  # values come from the environment
    except ValidationError as error:
        # Nothing to notify with -- the channels live in the config that just failed.
        # A loud log plus a distinct exit code is all that is available here.
        log.error("invalid configuration:\n%s", error)
        return EXIT_MISCONFIGURED

    logging.getLogger().setLevel(settings.log_level.upper())

    try:
        url = run_backup(settings)
    except Exception as error:
        log.exception("backup of %r failed", settings.db_name)
        notify_all(
            settings,
            f"Backup FAILED: {settings.db_name}",
            f"{type(error).__name__}: {error}\n\n{traceback.format_exc()}",
            debounce_key="failure",
        )
        return EXIT_FAILED

    log.info("backup complete: %s", url)
    clear_debounce(settings, "failure")  # a fresh outage after a success alerts immediately
    # ponytail: failure-only notification is silent if this container never starts or dies
    # before reaching the handler above -- no process, no notification. NOTIFY_ON_SUCCESS is
    # the poor man's cover. Upgrade path: ping healthchecks.io or an Uptime Kuma push
    # monitor here, and let the monitor alert when the ping fails to arrive.
    if settings.notify_on_success:
        notify_all(settings, f"Backup OK: {settings.db_name}", url)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
