"""Where dumps go. Add a backend by writing a class with .upload() and registering it."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, get_args

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from backy.config import Settings, StorageBackendName

log = logging.getLogger(__name__)

# The SDK default is 64 MB, and *below* that threshold upload_blob() does
# stream.read(length) -- the whole dump in RAM. This forces block uploads instead, so
# memory stays flat no matter how large the database gets.
MAX_SINGLE_PUT_SIZE = 4 * 1024 * 1024


class StorageBackend(Protocol):
    def upload(self, path: Path, name: str) -> str: ...


class AzureBlobStorage:
    def __init__(self, settings: Settings) -> None:
        # Settings' model_validator guarantees this; narrowing it for the type checker.
        assert settings.azure_storage_container
        self._container = settings.azure_storage_container

        if settings.azure_storage_connection_string:
            # Also the Azurite path in tests: a custom http BlobEndpoint on a random
            # port is accepted, because the https-only check applies to token credentials.
            self._service = BlobServiceClient.from_connection_string(
                settings.azure_storage_connection_string,
                max_single_put_size=MAX_SINGLE_PUT_SIZE,
            )
        else:
            self._service = BlobServiceClient(
                f"https://{settings.azure_storage_account}.blob.core.windows.net",
                credential=DefaultAzureCredential(),
                max_single_put_size=MAX_SINGLE_PUT_SIZE,
            )

    def upload(self, path: Path, name: str) -> str:
        blob = self._service.get_blob_client(container=self._container, blob=name)
        log.info("uploading %s to %s", path.name, blob.url)
        with path.open("rb") as handle:
            # overwrite=False: names are timestamped, so a collision means a double run.
            # Failing beats silently replacing a known-good backup with an unknown one.
            blob.upload_blob(handle, overwrite=False)
        return blob.url


STORAGE_BACKENDS: dict[StorageBackendName, Callable[[Settings], StorageBackend]] = {
    "azure": AzureBlobStorage,
}

# Fails at import if a backend is added to the Literal without an implementation.
assert set(get_args(StorageBackendName)) == STORAGE_BACKENDS.keys(), (
    "every StorageBackendName needs an implementation"
)


def get_storage(settings: Settings) -> StorageBackend:
    return STORAGE_BACKENDS[settings.storage_backend](settings)
