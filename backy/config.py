"""All configuration, validated at startup so a misconfigured container fails loudly."""

from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DbEngine = Literal["postgres"]
StorageBackendName = Literal["azure"]
NotifyChannel = Literal["webhook", "slack", "smtp", "resend"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # --- database ---
    db_engine: DbEngine = "postgres"
    # Label selector, not a name or id: Coolify redeploys recreate the container, which
    # changes both. Compose/Coolify always re-apply their service labels.
    db_container_label: str
    db_name: str
    db_user: str
    # Optional: the official postgres image trusts local socket connections, and pg_dump
    # runs *inside* that container, so it usually needs no password.
    db_password: str | None = None

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

    # --- misc ---
    log_level: str = "INFO"
    # docker.from_env() reads DOCKER_HOST itself, so it needs no field here.
    docker_timeout: int | None = Field(
        default=None,
        description="Docker client timeout in seconds. None disables it, which is required: "
        "the exec stream reuses this as its socket read timeout, so any finite value kills "
        "a dump that pauses for longer than it.",
    )

    @field_validator("notify_channels", mode="before")
    @classmethod
    def _split_channels(cls, value: object) -> object:
        """Accept NOTIFY_CHANNELS=webhook,slack instead of forcing JSON list syntax."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("db_container_label")
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
