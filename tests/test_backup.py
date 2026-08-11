"""End-to-end tests against live containers -- the exact scenario the sidecar runs."""

import io
import json
import tarfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import docker
import pytest
from azure.storage.blob import ContainerClient
from docker.models.containers import Container
from pydantic import ValidationError

from backy import notify
from backy.__main__ import EXIT_FAILED, EXIT_MISCONFIGURED, EXIT_OK, main
from backy.config import Settings
from backy.dump import exec_capture, find_container
from tests.conftest import (
    DB_NAME,
    DB_USER,
    LABEL_SELECTOR,
    OTHER_DB_NAME,
    SEED_ROWS,
    SEED_TABLE,
    database_entry,
)


def test_backup_roundtrip(
    backup_env: Callable[..., None],
    docker_client: docker.DockerClient,
    postgres_container: Container,
    blob_container: ContainerClient,
    tmp_path: Path,
) -> None:
    """Dump -> upload -> download -> restore -> the rows are all still there."""
    backup_env()

    assert main() == EXIT_OK

    blobs = list(blob_container.list_blobs())
    assert len(blobs) == 1, f"expected exactly one blob, got {[b.name for b in blobs]}"
    blob = blobs[0]
    assert blob.name.startswith(f"{DB_NAME}/"), blob.name
    assert blob.size > 0

    local_dump = tmp_path / Path(blob.name).name
    local_dump.write_bytes(blob_container.download_blob(blob.name).readall())

    # Restoring is the only assertion that proves the dump is complete and not truncated:
    # the old implementation produced a 0-byte file that uploaded perfectly happily.
    restored_db = f"restored_{uuid4().hex[:8]}"
    _copy_into(postgres_container, local_dump, "/tmp")
    exec_capture(docker_client, postgres_container, ["createdb", "-U", DB_USER, restored_db])
    exec_capture(
        docker_client,
        postgres_container,
        ["pg_restore", "-U", DB_USER, "-d", restored_db, f"/tmp/{local_dump.name}"],
    )
    count = exec_capture(
        docker_client,
        postgres_container,
        ["psql", "-U", DB_USER, "-d", restored_db, "-tAc", f"SELECT count(*) FROM {SEED_TABLE}"],
    )

    assert int(count.strip()) == SEED_ROWS


def test_failed_dump_uploads_nothing(
    backup_env: Callable[..., None], blob_container: ContainerClient
) -> None:
    """A nonzero pg_dump exit must abort before upload, not ship a partial backup."""
    backup_env([database_entry(name="database_that_does_not_exist")])

    assert main() == EXIT_FAILED
    assert list(blob_container.list_blobs()) == []


def test_failure_notifies_webhook(
    backup_env: Callable[..., None], blob_container: ContainerClient
) -> None:
    with _webhook_server() as (url, received):
        backup_env(
            [database_entry(container_label="com.docker.compose.service=no-such-service")],
            NOTIFY_CHANNELS="webhook",
            WEBHOOK_URL=url,
        )
        assert main() == EXIT_FAILED

    assert len(received) == 1, received
    payload = received[0]
    assert payload["database"] == DB_NAME
    assert "FAILED" in payload["subject"]
    assert "no-such-service" in payload["body"]
    assert list(blob_container.list_blobs()) == []


def test_resend_notifier_posts_an_email(
    clean_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Resend API shape: a list of recipients and a plain-text body."""
    with _webhook_server() as (url, received):
        monkeypatch.setattr(notify, "RESEND_URL", url)
        clean_env(
            AZURE_STORAGE_CONTAINER="backups",
            AZURE_STORAGE_ACCOUNT="acct",
            NOTIFY_CHANNELS="resend",
            RESEND_API_KEY="re_test",
            RESEND_FROM="backups@example.com",
            RESEND_TO="a@example.com, b@example.com",
        )
        # pyrefly: ignore  # values come from the environment
        notify.notify_all(Settings(), "app", "Backup FAILED: app", "boom")

    assert received == [
        {
            "from": "backups@example.com",
            "to": ["a@example.com", "b@example.com"],
            "subject": "Backup FAILED: app",
            "text": "boom",
        }
    ]


def test_failure_debounce_persists_and_clears(
    clean_env: Callable[..., None], tmp_path: Path
) -> None:
    """Repeat failures within the window send once; a success re-arms the alert."""
    with _webhook_server() as (url, received):
        clean_env(
            AZURE_STORAGE_CONTAINER="backups",
            AZURE_STORAGE_ACCOUNT="acct",
            NOTIFY_CHANNELS="webhook",
            WEBHOOK_URL=url,
            NOTIFY_DEBOUNCE_MINUTES=60,
            NOTIFY_STATE_FILE=tmp_path / "notify-state.json",
        )
        settings = Settings()  # pyrefly: ignore  # values come from the environment

        notify.notify_all(settings, "app", "Backup FAILED: app", "boom", debounce_key="failure")
        # A fresh Settings instance == a fresh container: state must come from the file.
        notify.notify_all(settings, "app", "Backup FAILED: app", "boom", debounce_key="failure")
        assert len(received) == 1, "second failure within the window must be suppressed"

        notify.clear_debounce(settings, "failure")
        notify.notify_all(settings, "app", "Backup FAILED: app", "boom", debounce_key="failure")
        assert len(received) == 2, "after a success clears the state, alert again"


def test_broken_notifier_does_not_mask_the_failure(
    backup_env: Callable[..., None],
) -> None:
    """An unreachable webhook must still leave the exit code reporting the real failure."""
    backup_env(
        [database_entry(container_label="com.docker.compose.service=no-such-service")],
        NOTIFY_CHANNELS="webhook",
        # Port 1 is reserved and never listening, so the POST raises.
        WEBHOOK_URL="http://127.0.0.1:1/hook",
    )

    assert main() == EXIT_FAILED


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"NOTIFY_CHANNELS": "slack"}, "SLACK_WEBHOOK_URL"),
        ({"NOTIFY_CHANNELS": "webhook"}, "WEBHOOK_URL"),
        ({"NOTIFY_CHANNELS": "smtp"}, "SMTP_HOST"),
        ({"AZURE_STORAGE_CONTAINER": ""}, "AZURE_STORAGE_CONTAINER"),
    ],
)
def test_settings_rejects_incomplete_config(
    clean_env: Callable[..., None], overrides: dict[str, str], expected_message: str
) -> None:
    """Misconfiguration must fail at startup, not at 3am when a notification is needed."""
    clean_env(
        **{
            "AZURE_STORAGE_CONTAINER": "backups",
            "AZURE_STORAGE_ACCOUNT": "acct",
            **overrides,
        }
    )

    with pytest.raises(ValidationError, match=expected_message):
        Settings()  # pyrefly: ignore  # values come from the environment


def test_missing_config_exits_misconfigured(clean_env: Callable[..., None]) -> None:
    clean_env()

    assert main() == EXIT_MISCONFIGURED


def test_label_must_match_exactly_one_container(
    docker_client: docker.DockerClient, postgres_container: Container
) -> None:
    # The bare label key also matches the postgres container, but find_container only
    # accepts one match -- silently taking [0] would let it dump an arbitrary database.
    find_container(docker_client, LABEL_SELECTOR)  # the happy path still resolves

    with pytest.raises(Exception, match="matched 0 running containers"):
        find_container(docker_client, "com.docker.compose.service=definitely-not-running")


def test_backs_up_every_database_in_the_file(
    backup_env: Callable[..., None], blob_container: ContainerClient
) -> None:
    """One run, one container, several databases -- each under its own blob prefix."""
    backup_env([database_entry(), database_entry(name=OTHER_DB_NAME)])

    assert main() == EXIT_OK

    prefixes = sorted(blob.name.split("/")[0] for blob in blob_container.list_blobs())
    assert prefixes == sorted([DB_NAME, OTHER_DB_NAME])


def test_one_broken_database_does_not_skip_the_others(
    backup_env: Callable[..., None], blob_container: ContainerClient
) -> None:
    """A failure must report itself and keep going, not abandon the remaining databases."""
    with _webhook_server() as (url, received):
        backup_env(
            [database_entry(name="database_that_does_not_exist"), database_entry()],
            NOTIFY_CHANNELS="webhook",
            WEBHOOK_URL=url,
        )
        assert main() == EXIT_FAILED

    blobs = [blob.name for blob in blob_container.list_blobs()]
    assert len(blobs) == 1 and blobs[0].startswith(f"{DB_NAME}/"), blobs
    assert [payload["database"] for payload in received] == ["database_that_does_not_exist"]


def test_database_overrides_its_own_storage_container(
    backup_env: Callable[..., None],
    blob_container: ContainerClient,
    new_blob_container: Callable[[], ContainerClient],
) -> None:
    """An override applies to its own database only; the other keeps the global setting."""
    other = new_blob_container()
    backup_env(
        [
            database_entry(),
            database_entry(
                name=OTHER_DB_NAME,
                overrides={"azure_storage_container": other.container_name},
            ),
        ]
    )

    assert main() == EXIT_OK

    assert [b.name.split("/")[0] for b in blob_container.list_blobs()] == [DB_NAME]
    assert [b.name.split("/")[0] for b in other.list_blobs()] == [OTHER_DB_NAME]


@pytest.mark.parametrize(
    ("contents", "expected_message"),
    [
        ("[]", "at least 1 item"),
        ("{ not json", "Invalid JSON"),
        ('[{"name": "a", "user": "a", "container_label": "svc=pg", "extra_knob": 1}]',
         "extra_knob"),
        ('[{"name": "a", "user": "a", "container_label": "pg"}]', "key=value"),
        ('[{"name": "a", "user": "a", "container_label": "svc=pg"},'
         ' {"name": "a", "user": "a", "container_label": "svc=other"}]', "duplicate"),
        # A per-database override is validated exactly like the environment it replaces.
        ('[{"name": "a", "user": "a", "container_label": "svc=pg",'
         ' "overrides": {"notify_channels": ["slack"]}}]', "SLACK_WEBHOOK_URL"),
    ],
)
def test_bad_databases_file_exits_misconfigured(
    clean_env: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    contents: str,
    expected_message: str,
) -> None:
    """Every database is validated before the first dump, so a bad file never half-runs."""
    databases_file = tmp_path / "databases.json"
    databases_file.write_text(contents)
    clean_env(
        DATABASES_FILE=databases_file,
        AZURE_STORAGE_CONTAINER="backups",
        AZURE_STORAGE_ACCOUNT="acct",
    )

    assert main() == EXIT_MISCONFIGURED
    assert expected_message in caplog.text


def test_missing_databases_file_exits_misconfigured(
    clean_env: Callable[..., None], caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """The common deployment mistake: the volume mount is missing. Say so, do not crash."""
    clean_env(
        DATABASES_FILE=tmp_path / "not-mounted.json",
        AZURE_STORAGE_CONTAINER="backups",
        AZURE_STORAGE_ACCOUNT="acct",
    )

    assert main() == EXIT_MISCONFIGURED
    assert "mount the databases file" in caplog.text


def test_test_notify_alerts_without_backing_anything_up(
    backup_env: Callable[..., None], blob_container: ContainerClient
) -> None:
    """--test-notify proves the channels work: one alert per database, zero dumps."""
    with _webhook_server() as (url, received):
        backup_env(
            [database_entry(), database_entry(name=OTHER_DB_NAME)],
            NOTIFY_CHANNELS="webhook",
            WEBHOOK_URL=url,
        )
        assert main(["--test-notify"]) == EXIT_OK

    assert sorted(payload["database"] for payload in received) == sorted(
        [DB_NAME, OTHER_DB_NAME]
    )
    assert list(blob_container.list_blobs()) == []


def test_unknown_argument_exits_misconfigured(backup_env: Callable[..., None]) -> None:
    backup_env()

    assert main(["--backup-everything-twice"]) == EXIT_MISCONFIGURED


def _copy_into(container: Container, source: Path, dest_dir: str) -> None:
    """docker cp, the API way: put_archive only speaks tar."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(source, arcname=source.name)
    assert container.put_archive(dest_dir, buffer.getvalue())


@contextmanager
def _webhook_server() -> Iterator[tuple[str, list[dict[str, str]]]]:
    """A real HTTP listener, so the notifier's actual request path is exercised."""
    received: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep pytest output readable

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", received
    finally:
        server.shutdown()
        server.server_close()
