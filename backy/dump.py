"""Dump a database by exec'ing its own client tools inside its container.

Running pg_dump *inside* the database container means this image never has to match the
server's Postgres major version, and needs no database client installed at all.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import docker
from docker.models.containers import Container
from pydantic import BaseModel, ConfigDict

from backy import BackupError
from backy.config import Database, DbEngine, Settings

log = logging.getLogger(__name__)

Sink = Callable[[bytes], object]


class DumpSpec(BaseModel):
    """How to dump one engine. Adding MySQL is one entry below, not a new class."""

    model_config = ConfigDict(frozen=True)

    build_command: Callable[[Database], list[str]]
    extension: str
    build_env: Callable[[Database], dict[str, str]]


DUMP_SPECS: dict[DbEngine, DumpSpec] = {
    "postgres": DumpSpec(
        # --format=custom is compressed by pg_dump itself and restores with pg_restore,
        # so there is no compression code here.
        build_command=lambda db: [
            "pg_dump",
            "--format=custom",
            "--username",
            db.user,
            "--dbname",
            db.name,
        ],
        extension="dump",
        build_env=lambda db: {"PGPASSWORD": db.password} if db.password else {},
    ),
}

# Fails at import if an engine is added to the Literal without a spec.
assert set(get_args(DbEngine)) == DUMP_SPECS.keys(), "every DbEngine needs a DumpSpec"


def get_client(settings: Settings) -> docker.DockerClient:
    # timeout must be None (settings default): docker-py reuses the client timeout as the
    # *socket read* timeout on exec streams, and _read_from_socket is the one streaming
    # helper that never disables it. Any finite value kills a dump that pauses for longer.
    #
    # pyrefly: ignore  # docker-py types timeout as int, but passes it straight to
    # requests, which documents None as "no timeout". Verified against docker-py 7.2.0.
    return docker.from_env(timeout=settings.docker_timeout)


def find_container(client: docker.DockerClient, label: str) -> Container:
    """Resolve a key=value label selector to exactly one running container."""
    matches = client.containers.list(filters={"label": label})
    if len(matches) != 1:
        found = ", ".join(c.name or c.short_id for c in matches) or "none"
        raise BackupError(
            f"label {label!r} matched {len(matches)} running containers ({found}); "
            f"expected exactly 1"
        )
    return matches[0]


def _exec(
    client: docker.DockerClient,
    container: Container,
    command: list[str],
    sink: Sink,
    env: dict[str, str] | None = None,
) -> None:
    """Run a command in a container, streaming stdout into sink, and verify it succeeded.

    Uses the low-level API on purpose: container.exec_run(stream=True) returns
    (None, generator) and throws away the exec id, so the exit code is unrecoverable --
    which would let a half-written dump look like a success.
    """
    api = client.api
    # command stays a list: exec_create only shlex-splits strings. No shell, so no
    # quoting to get wrong -- and critically no redirect, which would write the dump
    # inside the database container and stream us nothing.
    exec_id = api.exec_create(container.id, command, environment=env or {})["Id"]
    stderr_chunks: list[bytes] = []

    stream = api.exec_start(exec_id, stream=True, demux=True)
    try:
        for stdout_chunk, stderr_chunk in stream:
            if stdout_chunk:
                sink(stdout_chunk)
            if stderr_chunk:
                stderr_chunks.append(stderr_chunk)
    finally:
        # With stream=True docker-py deliberately leaves the response open and hands
        # ownership to us. CancellableStream has no __enter__, hence the try/finally.
        stream.close()

    info = api.exec_inspect(exec_id)
    stderr = b"".join(stderr_chunks).decode(errors="replace").strip()
    # ExitCode can still be None if the process has not been reaped, so check Running too.
    if info["Running"] or info["ExitCode"] != 0:
        raise BackupError(
            f"{command[0]} in {container.name} failed "
            f"(exit={info['ExitCode']}, running={info['Running']}): {stderr or '<no stderr>'}"
        )
    if stderr:
        log.warning("%s stderr: %s", command[0], stderr)


def exec_to_file(
    client: docker.DockerClient,
    container: Container,
    command: list[str],
    dest: Path,
    env: dict[str, str] | None = None,
) -> None:
    """Stream a command's stdout straight to disk -- never buffers the dump in memory."""
    with dest.open("wb") as handle:
        _exec(client, container, command, handle.write, env)


def exec_capture(
    client: docker.DockerClient,
    container: Container,
    command: list[str],
    env: dict[str, str] | None = None,
) -> bytes:
    """Run a command and return its stdout. For small outputs only."""
    captured = bytearray()
    _exec(client, container, command, captured.extend, env)
    return bytes(captured)


def dump_database(settings: Settings, database: Database, dest_dir: Path) -> Path:
    """Dump one database into dest_dir and return the file written."""
    spec = DUMP_SPECS[database.engine]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"{database.name}-{timestamp}.{spec.extension}"

    client = get_client(settings)
    try:
        container = find_container(client, database.container_label)
        log.info("dumping %r from container %s", database.name, container.name)
        exec_to_file(
            client, container, spec.build_command(database), dest, spec.build_env(database)
        )
    finally:
        client.close()

    size = dest.stat().st_size
    if size == 0:
        # pg_dump can exit 0 having written nothing if it is pointed somewhere strange.
        # An empty backup that uploads cleanly is the worst outcome available.
        raise BackupError(f"dump of {database.name!r} is empty")
    log.info("dumped %s bytes to %s", size, dest.name)
    return dest


def blob_name(database: Database, dump_path: Path) -> str:
    """name/ prefix so one storage container can hold several databases,
    and so Azure lifecycle rules can target them individually."""
    return f"{database.name}/{dump_path.name}"
