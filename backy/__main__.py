"""Entrypoint: back up every configured database, then exit. 0 = all uploaded, 1 = at
least one failed, 2 = misconfigured.

    python -m backy                  back up every database in the databases file
    python -m backy --test-notify    send a test alert through each database's channels

Scheduling lives outside the container (Coolify scheduled task, host cron, k8s CronJob) so
there is no cron daemon in here and the exit code is the whole success signal.
"""

import logging
import sys
import tempfile
import traceback
from collections.abc import Sequence
from pathlib import Path

from backy.config import Database, Settings, load_databases, settings_for
from backy.dump import blob_name, dump_database
from backy.notify import clear_debounce, notify_all
from backy.storage import get_storage

log = logging.getLogger("backy")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISCONFIGURED = 2


def run_backup(settings: Settings, database: Database) -> str:
    """Dump, upload, return the blob URL. The temp dir cleans up the dump for us."""
    with tempfile.TemporaryDirectory(prefix="backy-") as tmp:
        dump_path = dump_database(settings, database, Path(tmp))
        return get_storage(settings).upload(dump_path, blob_name(database, dump_path))


def main(argv: Sequence[str] = ()) -> int:
    # One flag does not earn argparse. Defaulting to () rather than reading sys.argv keeps
    # pytest's own arguments out of here; __main__ below passes the real ones.
    test_notify = list(argv) == ["--test-notify"]
    if argv and not test_notify:
        log.error("usage: python -m backy [--test-notify]")
        return EXIT_MISCONFIGURED

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    try:
        settings = Settings()  # pyrefly: ignore  # values come from the environment
        logging.getLogger().setLevel(settings.log_level.upper())
        # Every database is resolved and validated before the first dump runs, so a bad
        # override on the last one still fails at startup rather than three dumps in.
        plan = [(database, settings_for(database)) for database in load_databases(settings)]
    except ValueError as error:
        # ValidationError is a ValueError in pydantic v2, so this covers both the
        # environment and the databases file.
        # Nothing to notify with -- the channels live in the config that just failed.
        # A loud log plus a distinct exit code is all that is available here.
        log.error("invalid configuration:\n%s", error)
        return EXIT_MISCONFIGURED

    if test_notify:
        # Deliberately per database and through notify_all: this exercises the exact path
        # a 3am failure takes, including any per-database channel override.
        for database, db_settings in plan:
            notify_all(
                db_settings,
                database.name,
                f"Backup TEST: {database.name}",
                "Test notification from backy. Nothing was dumped or uploaded.",
            )
        return EXIT_OK

    failed = 0
    for database, db_settings in plan:
        # Per-database debounce key: with one shared key, a permanently broken database
        # would suppress the first alert for every other one.
        debounce_key = f"failure:{database.name}"
        try:
            url = run_backup(db_settings, database)
        except Exception as error:
            failed += 1
            log.exception("backup of %r failed", database.name)
            notify_all(
                db_settings,
                database.name,
                f"Backup FAILED: {database.name}",
                f"{type(error).__name__}: {error}\n\n{traceback.format_exc()}",
                debounce_key=debounce_key,
            )
            continue  # one broken database must not skip the rest

        log.info("backup complete: %s", url)
        clear_debounce(db_settings, debounce_key)  # a fresh outage alerts immediately
        # ponytail: failure-only notification is silent if this container never starts or
        # dies before reaching the handler above -- no process, no notification.
        # NOTIFY_ON_SUCCESS is the poor man's cover. Upgrade path: ping healthchecks.io or
        # an Uptime Kuma push monitor here, and let the monitor alert when no ping arrives.
        if db_settings.notify_on_success:
            notify_all(db_settings, database.name, f"Backup OK: {database.name}", url)

    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
