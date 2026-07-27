# backy

A backup sidecar. It runs in its own container, dumps a database out of a **sibling**
container, uploads the dump to blob storage, and tells you when that fails.

One run, one exit code. `0` uploaded, `1` failed, `2` misconfigured.

```
backy/
  config.py    Settings — every knob, validated at startup
  dump.py      docker exec streaming dump + the engine registry
  storage.py   StorageBackend protocol + Azure Blob
  notify.py    webhook / Slack / SMTP / Resend + fan-out
  __main__.py  entrypoint
```

## How it dumps

It `docker exec`s the database's *own* `pg_dump` inside the database container and streams
stdout back over the socket. Two consequences worth knowing:

- This image contains **no database client**, so it never has to match the server's
  Postgres major version. Upgrade Postgres, change nothing here.
- It needs `/var/run/docker.sock`, which is **root-equivalent access to the host**. That is
  the real cost of this approach. The alternative — `pg_dump -h postgres` over the docker
  network — drops the socket but needs database credentials in the environment and a
  version-matched client in this image.

The container to dump is chosen by **label selector**, not name or id:

```
DB_CONTAINER_LABEL=com.docker.compose.service=postgres
```

Names survive restarts but not recreates, and ids survive neither — a Coolify redeploy
changes both. Compose and Coolify re-apply their service labels every time, so a label is
the only identifier that holds. backy requires the selector to match **exactly one**
running container and fails otherwise, rather than dumping an arbitrary match.

## Setup

```bash
cp .env.example .env      # then fill it in
docker build -t backy .
```

The Azure storage container must already exist; backy does not create it, so it needs no
container-create permission.

## Scheduling

There is no cron in the container. Point something external at it:

- **Coolify** — add a Scheduled Task on the service.
- **host cron** — `0 3 * * * docker compose run --rm backy`
- **Kubernetes** — a `CronJob`.

The exit code is the whole success signal, so a failed run is visible to whatever runs it.
See `compose.example.yml`.

## Retention

Not backy's job. Azure Blob Storage does it natively and for free: add a **lifecycle
management** rule on the storage container, e.g. delete blobs older than 30 days, filtered
on the `<db_name>/` prefix. Blobs are named `<db_name>/<db_name>-<UTC timestamp>.dump`
precisely so those rules can target one database at a time.

## Notifications

Set `NOTIFY_CHANNELS` to any comma-separated mix of `webhook`, `slack`, `smtp`, `resend`. Every
configured channel gets a copy; a channel that errors is logged and skipped, so a broken
notifier can never swallow the failure it was sent to report.

`webhook` is a plain JSON POST, which also covers Coolify, Discord, Teams and ntfy — the
channel is a URL, not code.

On Azure specifically there is **no free native email sender**: Communication Services
Email is paid and needs a verified domain, and Azure Monitor alerting would mean shipping
logs into Log Analytics first. So `smtp` means bring your own (Gmail app password, …), and
a webhook is the cheaper default. `resend` is the same email over Resend's HTTP API instead
— an API key and a verified sending domain, no SMTP server to reach.

**Known gap:** notification happens *inside* the process, so if the container never starts
or dies before the handler runs, nothing is sent. `NOTIFY_ON_SUCCESS=true` is the cheap
cover — no message is itself the alarm. The real fix is a dead-man's switch: ping
healthchecks.io or an Uptime Kuma push monitor on success and let the monitor alert when
the ping fails to arrive. See the `ponytail:` note in `__main__.py`.

## Adding an engine, a backend, a channel

Each is one registry entry, not a class hierarchy. Add the name to the `Literal` in
`config.py` and the implementation to the registry — a module-level `assert` fails at
import if you do one without the other.

```python
# dump.py — MySQL
DUMP_SPECS["mysql"] = DumpSpec(
    build_command=lambda s: ["mysqldump", "-u", s.db_user, s.db_name],
    extension="sql",
    build_env=lambda s: {"MYSQL_PWD": s.db_password} if s.db_password else {},
)
```

`storage.py` wants a class with `upload(path, name) -> str` registered in
`STORAGE_BACKENDS`; `notify.py` wants a `(settings, subject, body) -> None` function in
`NOTIFIERS`.

## Tests

```bash
uv run pytest
```

Real containers, no mocks: testcontainers starts a labelled `postgres:17-alpine` and a real
Azurite blob emulator, and the suite calls the same `main()` the sidecar runs. The headline
test dumps, uploads, downloads, `pg_restore`s into a fresh database and asserts the row
count survived — the only assertion that actually proves a dump is complete rather than
truncated. Others prove that a failed `pg_dump` uploads **nothing**, and that a failure
reaches a live HTTP webhook listener.

First run pulls images and takes a few minutes; after that the suite is ~30s.

`git config core.hooksPath .githooks` once per clone makes `pre-push` run `pyrefly check`
and the suite before every push. It needs a working Docker; `--no-verify` skips it.
