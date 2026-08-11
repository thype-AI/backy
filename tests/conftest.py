"""Live fixtures: a real labelled postgres container and a real Azurite blob service.

Tests run on the host against the host docker socket, so backy sees the testcontainers
postgres exactly the way the deployed sidecar sees its sibling container.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import docker
import pytest
from azure.storage.blob import BlobServiceClient, ContainerClient
from docker.models.containers import Container
from testcontainers.community.azurite import AzuriteContainer
from testcontainers.community.postgres import PostgresContainer

from backy.config import Settings
from backy.dump import exec_capture, find_container

LABEL_KEY = "com.docker.compose.service"
# Unique per session so concurrent runs can never resolve each other's container, and so
# find_container's "exactly one match" rule stays honest on a busy dev machine.
LABEL_VALUE = f"backy-test-{uuid4().hex[:8]}"
LABEL_SELECTOR = f"{LABEL_KEY}={LABEL_VALUE}"

DB_NAME = "backy_test"
DB_USER = "backy"
DB_PASSWORD = "s3cret"
SEED_TABLE = "widgets"
SEED_ROWS = 25
# Always present in a postgres container, so a second backup target costs no fixture.
OTHER_DB_NAME = "postgres"


def database_entry(**overrides: object) -> dict[str, object]:
    """One entry for databases.json, pointing at the fixture container."""
    return {
        "name": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "container_label": LABEL_SELECTOR,
        **overrides,
    }


@pytest.fixture(scope="session")
def docker_client() -> Iterator[docker.DockerClient]:
    # pyrefly: ignore  # see get_client(): the docker-py stub is narrower than runtime
    client = docker.from_env(timeout=None)
    yield client
    client.close()


@pytest.fixture(scope="session")
def postgres_container(docker_client: docker.DockerClient) -> Iterator[Container]:
    """A postgres container carrying the compose-style label backy selects on."""
    # labels go through __init__ -> DockerContainer._kwargs -> containers.run(labels=...),
    # where create_labels merges them with the org.testcontainers.* ones. Passed here
    # rather than via with_kwargs(), which *assigns* _kwargs and would clobber them.
    container = PostgresContainer(
        "postgres:17-alpine",
        username=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
        driver=None,  # no sqlalchemy/psycopg2 needed; readiness is an in-container psql
        labels={LABEL_KEY: LABEL_VALUE},
    )
    with container:
        db = find_container(docker_client, LABEL_SELECTOR)
        _seed(docker_client, db)
        yield db


def _seed(client: docker.DockerClient, container: Container) -> None:
    """Create a table with a known row count. Uses backy's own exec helper on purpose."""
    statements = (
        f"CREATE TABLE {SEED_TABLE} (id int PRIMARY KEY, name text NOT NULL);"
        f"INSERT INTO {SEED_TABLE} "
        f"SELECT i, 'widget-' || i FROM generate_series(1, {SEED_ROWS}) AS i;"
    )
    exec_capture(
        client,
        container,
        ["psql", "-U", DB_USER, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1", "-c", statements],
    )


@pytest.fixture(scope="session")
def azurite_connection_string() -> Iterator[str]:
    # --skipApiVersionCheck because azure-storage-blob 12.30 negotiates a newer REST API
    # version than Azurite recognises. Relaxing the emulator keeps the fix in the test
    # environment; pinning api_version on the client would put it in production code.
    # The rest of the command line is the image's own default CMD, which this replaces.
    azurite = AzuriteContainer().with_command(
        "azurite -l /data --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0"
        " --skipApiVersionCheck"
    )
    with azurite:
        # Emits BlobEndpoint=http://<host>:<mapped-port>/devstoreaccount1, which the SDK
        # accepts over plain http for shared-key auth. Never UseDevelopmentStorage=true --
        # that hardcodes port 10000 and cannot see the mapped port.
        yield azurite.get_connection_string()


@pytest.fixture
def new_blob_container(azurite_connection_string: str) -> Iterator[Callable[[], ContainerClient]]:
    """Make blob containers on demand -- a per-database storage override needs two."""
    service = BlobServiceClient.from_connection_string(azurite_connection_string)
    created: list[ContainerClient] = []

    def make() -> ContainerClient:
        client = service.create_container(f"backups-{uuid4().hex[:8]}")
        created.append(client)
        return client

    yield make
    for client in created:
        client.delete_container()


@pytest.fixture
def blob_container(new_blob_container: Callable[[], ContainerClient]) -> ContainerClient:
    """A fresh blob container per test, so 'nothing was uploaded' assertions are exact."""
    return new_blob_container()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Callable[..., None]:
    """Wipe every backy setting from the environment, then apply the given ones.

    chdir keeps Settings' env_file=".env" from picking up the developer's real .env.
    """

    def apply(**values: object) -> None:
        monkeypatch.chdir(tmp_path)
        for field in Settings.model_fields:
            monkeypatch.delenv(field.upper(), raising=False)
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))

    return apply


@pytest.fixture
def backup_env(
    clean_env: Callable[..., None],
    postgres_container: Container,
    azurite_connection_string: str,
    blob_container: ContainerClient,
    tmp_path: Path,
) -> Callable[..., None]:
    """Apply a complete working configuration: a databases file plus the env settings.

    Pass `databases` to describe the targets, anything else to override an env setting.
    """

    def apply(databases: list[dict[str, object]] | None = None, **overrides: object) -> None:
        # Absolute path: clean_env chdirs, and DATABASES_FILE is not relative to cwd.
        databases_file = tmp_path / "databases.json"
        databases_file.write_text(json.dumps(databases or [database_entry()]))
        clean_env(
            **{
                "DATABASES_FILE": databases_file,
                "STORAGE_BACKEND": "azure",
                "AZURE_STORAGE_CONTAINER": blob_container.container_name,
                "AZURE_STORAGE_CONNECTION_STRING": azurite_connection_string,
                **overrides,
            }
        )

    return apply
