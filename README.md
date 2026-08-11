# backy

A backup sidecar. It runs in its own container, dumps one or more databases out of
**sibling** containers, uploads each dump to blob storage, and tells you when that fails.

One run, one exit code. `0` all uploaded, `1` at least one failed, `2` misconfigured.
A database that fails does not stop the rest of the list.

```
backy/
  config.py    Settings + the databases file — every knob, validated at startup
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

```json
"container_label": "com.docker.compose.service=postgres"
```

Names survive restarts but not recreates, and ids survive neither — a Coolify redeploy
changes both. Compose and Coolify re-apply their service labels every time, so a label is
the only identifier that holds. backy requires the selector to match **exactly one**
running container and fails otherwise, rather than dumping an arbitrary match.

## Configuration

Two files. `.env` holds the settings shared by every backup — storage, notifications,
logging. `databases.json` holds the list of databases, and is **mounted into the container**
at `/config/databases.json` (override with `DATABASES_FILE`):

```json
[
  {
    "name": "app",
    "user": "app",
    "container_label": "com.docker.compose.service=postgres"
  },
  {
    "name": "analytics",
    "user": "analytics",
    "container_label": "com.docker.compose.service=analytics-db",
    "overrides": {
      "azure_storage_container": "analytics-backups",
      "notify_channels": ["slack"],
      "slack_webhook_url": "https://hooks.slack.com/services/..."
    }
  }
]
```

`password` is usually unnecessary — `pg_dump` runs inside the database container over the
unix socket, which the official postgres image trusts. `engine` defaults to `postgres`.

`overrides` is optional and may set **any** storage or notification setting from `.env` for
that database alone; anything left out is inherited from the environment. The merged result
is validated per database at startup, so an override that names a notification channel
without its URL exits `2` before the first dump runs — as does an unknown key, a duplicate
`name`, an empty list, or a file that is not mounted.

```bash
cp .env.example .env                          # shared settings
cp databases.example.json databases.json      # the list of databases
docker build -t backy .
```

The Azure storage container must already exist; backy does not create it, so it needs no
container-create permission. `databases.json` is gitignored — it can hold passwords.

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
on the `<name>/` prefix. Blobs are named `<name>/<name>-<UTC timestamp>.dump` precisely so
those rules can target one database at a time, and so one storage container holds them all.

## Notifications

Set `NOTIFY_CHANNELS` to any comma-separated mix of `webhook`, `slack`, `smtp`, `resend`. Every
configured channel gets a copy, once per failed database (each debounced on its own, so one
permanently broken database cannot silence the first alert for another); a channel that errors is logged and skipped, so a broken
notifier can never swallow the failure it was sent to report.

`webhook` is a plain JSON POST, which also covers Coolify, Discord, Teams and ntfy — the
channel is a URL, not code.

On Azure specifically there is **no free native email sender**: Communication Services
Email is paid and needs a verified domain, and Azure Monitor alerting would mean shipping
logs into Log Analytics first. So `smtp` means bring your own (Gmail app password, …), and
a webhook is the cheaper default. `resend` is the same email over Resend's HTTP API instead
— an API key and a verified sending domain, no SMTP server to reach.

Check your channels without touching a database:

```bash
docker compose run --rm backy --test-notify           # one-shot container
docker compose exec backy uv run --no-sync python -m backy --test-notify   # Coolify/idling
```

It sends one test message per database through that database's own channels — the exact
path a real failure takes, overrides included — and dumps nothing.

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
    build_command=lambda db: ["mysqldump", "-u", db.user, db.name],
    extension="sql",
    build_env=lambda db: {"MYSQL_PWD": db.password} if db.password else {},
)
```

`storage.py` wants a class with `upload(path, name) -> str` registered in
`STORAGE_BACKENDS`; `notify.py` wants a `(settings, database, subject, body) -> None` function in
`NOTIFIERS`.

## Tests

```bash
uv run pytest
```

Real containers, no mocks: testcontainers starts a labelled `postgres:17-alpine` and a real
Azurite blob emulator, and the suite calls the same `main()` the sidecar runs. The headline
test dumps, uploads, downloads, `pg_restore`s into a fresh database and asserts the row
count survived — the only assertion that actually proves a dump is complete rather than
truncated. Others prove that a failed `pg_dump` uploads **nothing**, that a failure reaches
a live HTTP webhook listener, and that one broken database in the list neither skips the
others nor loses its own alert.

First run pulls images and takes a few minutes; after that the suite is ~30s.

`git config core.hooksPath .githooks` once per clone makes `pre-push` run `pyrefly check`
and the suite before every push. It needs a working Docker; `--no-verify` skips it.
