"""All configuration, validated at startup so a misconfigured container fails loudly."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DbEngine = Literal["postgres"]
StorageBackendName = Literal["azure"]
NotifyChannel = Literal["webhook", "slack", "smtp", "resend"]


class BackupSettings(BaseModel):
    """Everything one backup needs beyond the database itself.

    Split out of Settings so a database entry can override any of it without duplicating
    the field list: Database.overrides is this same model, with nothing required.
    """

    # --- storage ---
    storage_backend: StorageBackendName = "azure"
    azure_storage_container: str | None = None
    azure_storage_account: str | None = None
    azure_storage_connection_string: str | None = None

    # --- notifications ---
    notify_channels: Annotated[list[NotifyChannel], NoDecode] = []
    notify_on_success: bool = False
    webhook_url: str | None = None
    slack_webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_starttls: bool = True
    resend_api_key: str | None = None
    resend_from: str | None = None
    resend_to: str | None = None

    @field_validator("notify_channels", mode="before")
    @classmethod
    def _split_channels(cls, value: object) -> object:
        """Accept NOTIFY_CHANNELS=webhook,slack instead of forcing JSON list syntax."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class Settings(BackupSettings, BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # The mounted JSON list of databases to back up. See databases.example.json.
    databases_file: Path = Path("/config/databases.json")
    # Same JSON, pasted inline instead of mounted. Wins over databases_file when set --
    # for hosts (Coolify) whose compose file mounts cannot carry content.
    databases_json: str | None = None

    # --- notifications, global only: state and debounce are per-container, not per-db ---
    # After one failure alert, stay quiet for this many minutes. 0 disables. State lives
    # in notify_state_file -- mount a volume there or the debounce resets every run.
    notify_debounce_minutes: int = 0
    notify_state_file: Path = Path("/state/notify-state.json")

    # --- misc ---
    log_level: str = "INFO"
    # docker.from_env() reads DOCKER_HOST itself, so it needs no field here.
    docker_timeout: int | None = Field(
        default=None,
        description="Docker client timeout in seconds. None disables it, which is required: "
        "the exec stream reuses this as its socket read timeout, so any finite value kills "
        "a dump that pauses for longer than it.",
    )

    @model_validator(mode="after")
    def _check_backend_and_channels(self) -> Self:
        missing: list[str] = []

        if self.storage_backend == "azure":
            if not self.azure_storage_container:
                missing.append("AZURE_STORAGE_CONTAINER (required for storage_backend=azure)")
            if not (self.azure_storage_connection_string or self.azure_storage_account):
                missing.append(
                    "AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT "
                    "(one is required for storage_backend=azure)"
                )

        # Checked here rather than at send time: a notifier that turns out to be
        # unconfigured during a 3am failure is a notification you never get.
        required_per_channel: dict[NotifyChannel, list[tuple[str, object]]] = {
            "webhook": [("WEBHOOK_URL", self.webhook_url)],
            "slack": [("SLACK_WEBHOOK_URL", self.slack_webhook_url)],
            "smtp": [
                ("SMTP_HOST", self.smtp_host),
                ("SMTP_FROM", self.smtp_from),
                ("SMTP_TO", self.smtp_to),
            ],
            "resend": [
                ("RESEND_API_KEY", self.resend_api_key),
                ("RESEND_FROM", self.resend_from),
                ("RESEND_TO", self.resend_to),
            ],
        }
        for channel in self.notify_channels:
            for name, value in required_per_channel[channel]:
                if not value:
                    missing.append(f"{name} (required for notify channel {channel!r})")

        if missing:
            raise ValueError("missing configuration: " + "; ".join(missing))
        return self


class Database(BaseModel):
    """One database to back up -- one entry in the mounted databases file."""

    # extra="forbid": a typo'd key in the JSON is a silently skipped setting otherwise,
    # and this file is the only place the databases are described.
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    user: str
    # Label selector, not a name or id: Coolify redeploys recreate the container, which
    # changes both. Compose/Coolify always re-apply their service labels.
    container_label: str
    # Optional: the official postgres image trusts local socket connections, and pg_dump
    # runs *inside* that container, so it usually needs no password.
    password: str | None = None
    engine: DbEngine = "postgres"
    # Any field set here replaces the environment value for this database only.
    overrides: BackupSettings = BackupSettings()

    @field_validator("container_label")
    @classmethod
    def _label_has_value(cls, value: str) -> str:
        # A bare "key" filter matches every container carrying the key, which is almost
        # never what you want when the result picks which database gets dumped.
        if "=" not in value:
            raise ValueError(
                f"must be a key=value label selector, e.g. "
                f"'com.docker.compose.service=postgres' (got {value!r})"
            )
        return value


# min_length=1: an empty list is a container that runs, exits 0 and backs up nothing.
_DATABASES = TypeAdapter(Annotated[list[Database], Field(min_length=1)])


def load_databases(settings: Settings) -> list[Database]:
    """Parse the databases config. Raises ValueError, so a bad file exits 2, not 1."""
    if settings.databases_json:
        return _validate(settings.databases_json.encode(), "DATABASES_JSON")
    try:
        raw = settings.databases_file.read_bytes()
    except OSError as error:
        raise ValueError(
            f"cannot read {settings.databases_file} ({error.strerror}); mount the databases "
            f"file there, or point DATABASES_FILE somewhere else"
        ) from error

    return _validate(raw, str(settings.databases_file))


def _validate(raw: bytes, source: str) -> list[Database]:
    databases = _DATABASES.validate_json(raw)
    names = [database.name for database in databases]
    if len(set(names)) != len(names):
        # They would share a blob prefix, so their backups interleave under one folder.
        raise ValueError(f"duplicate database names in {source}: {names}")
    return databases


def settings_for(database: Database) -> Settings:
    """The environment settings with this database's overrides layered on and re-validated.

    Only keys actually present in the JSON are passed (model_fields_set), so an unset
    override inherits rather than resetting to its default. Init kwargs outrank env vars
    in pydantic-settings, which makes that the whole merge.
    """
    explicit = database.overrides.model_dump(include=database.overrides.model_fields_set)
    return Settings(**explicit)  # pyrefly: ignore  # the rest comes from the environment

