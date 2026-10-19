"""Write the API's OpenAPI document for the generated frontend client.

The app is built in-process with a synthetic product configuration so every
optional router is mounted. Nothing connects to the configured services.
"""

from __future__ import annotations

import json
import sys
import tempfile
import warnings
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic_settings import BaseSettings

SYNTHETIC_PRODUCT = {
    "MOYAI_CORE": "pydantic", "MOYAI_PRODUCT_MODE": "existing", "MOYAI_RUNTIME_ROLE": "api",
    "MOYAI_DATABASE_URL": "postgresql://synthetic:password@127.0.0.1/isolated",
    "MOYAI_DATABASE_SCHEMA": "moyai", "MOYAI_NATIVE_SCHEMA": "moyai_native_schema",
    "MOYAI_SCHEMA_MODE": "verify", "MOYAI_DATABASE_INITIALIZE": "false",
    "TEMPORAL_ENABLED": "true", "MOYAI_SEPARATE_BROKER": "true", "MOYAI_COORDINATOR_HANDOFF": "true",
    "SESSION_SECRET": "synthetic-original-session-secret",
    "MOYAI_NATIVE_ENCRYPTION_KEY": "synthetic-native-key-with-at-least-32-characters",
    "MOYAI_NATIVE_OBJECT_PREFIX": "pydantic-v1", "OBJECT_STORAGE_PREFIX": "moyai",
    "OBJECT_STORAGE_BUCKET": "synthetic-bucket", "OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:9000",
    "OBJECT_STORAGE_ACCESS_KEY_ID": "synthetic-key-id", "OBJECT_STORAGE_SECRET_ACCESS_KEY": "synthetic-secret",
    "OBJECT_STORAGE_SESSION_TOKEN": "synthetic-session",
    "TEMPORAL_ADDRESS": "127.0.0.1:7233", "TEMPORAL_NAMESPACE": "isolated",
    "TEMPORAL_TASK_QUEUE": "existing-sessions-v1", "MOYAI_NATIVE_TASK_QUEUE": "isolated-pydantic-v1",
    "MOYAI_PUBLIC_URL": "http://127.0.0.1:8796",
    "LITELLM_API_BASE": "http://127.0.0.1:8000/v1", "LITELLM_API_KEY": "synthetic-gateway",
    "AGENT_MODEL": "openai/gpt-6-astra",
}


@contextmanager
def schema_app() -> Iterator[FastAPI]:
    """The app as the frontend sees it: product mode with every optional router mounted."""
    from .api import create_app
    from .product_config import NATIVE_KEY_FORMAT, NATIVE_OBJECT_FORMAT, load_product_config
    from .runtime import Runtime
    from .settings import Settings

    with tempfile.TemporaryDirectory() as data_dir:
        config = load_product_config({
            **SYNTHETIC_PRODUCT,
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "MOYAI_NATIVE_KEY_FORMAT": NATIVE_KEY_FORMAT, "MOYAI_NATIVE_OBJECT_FORMAT": NATIVE_OBJECT_FORMAT,
            "DATA_DIR": data_dir,
        })
        settings = cast(Settings, cast(type[BaseSettings], Settings)(_env_file=None))
        yield create_app(Runtime(settings, product_config=config), run_dispatcher=False)


def openapi_document() -> dict[str, Any]:
    with schema_app() as app, warnings.catch_warnings():
        # Duplicate operation IDs are reported below as a hard failure.
        warnings.simplefilter("ignore", UserWarning)
        document = app.openapi()
    duplicates = [
        name for name, count in Counter(
            operation["operationId"] for item in document["paths"].values() for operation in item.values()
        ).items() if count > 1
    ]
    if duplicates:
        raise SystemExit(f"Duplicate OpenAPI operation IDs: {', '.join(sorted(duplicates))}")
    return document


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit("usage: python -m moyai.openapi_dump <output.json>")
    Path(argv[0]).write_text(json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
