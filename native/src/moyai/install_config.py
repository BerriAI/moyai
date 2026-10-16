"""Configure a fresh Docker bundle without a host Python or Node installation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path

from . import deployment_checks
from .deployment_config import SECRET_NAMES, InstallationError, build_revision, configuration, new_state

ENVIRONMENT_FILE = "compose.environment.json"
SECRETS = (
    "MOYAI_DATABASE_URL", "LITELLM_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
    "OBJECT_STORAGE_ACCESS_KEY_ID", "OBJECT_STORAGE_SECRET_ACCESS_KEY", "OBJECT_STORAGE_SESSION_TOKEN",
    "TEMPORAL_API_KEY", "MOYAI_TEMPORAL_DATABASE_PASSWORD",
    "CLOUDFLARE_ACCESS_CLIENT_ID", "CLOUDFLARE_ACCESS_CLIENT_SECRET", *SECRET_NAMES,
)
DEFAULTS = {
    "MOYAI_INSTALLATION_ID": "moyai",
    "MOYAI_BIND_ADDRESS": "127.0.0.1",
    "MOYAI_PORT": "8787",
    "MOYAI_PUBLIC_URL": "",
    "MOYAI_DATABASE_URL": "",
    "OBJECT_STORAGE_BUCKET": "",
    "OBJECT_STORAGE_REGION": "us-east-1",
    "OBJECT_STORAGE_ENDPOINT": "",
    "OBJECT_STORAGE_PREFIX": "",
    "MOYAI_NATIVE_OBJECT_PREFIX": "",
    "LITELLM_API_BASE": "",
    "AGENT_MODEL": "",
    "MODAL_APP_NAME": "",
    "TEMPORAL_ADDRESS": "",
    "TEMPORAL_NAMESPACE": "",
    "TEMPORAL_TLS": "true",
    **dict.fromkeys(SECRETS, ""),
}
PROMPTS = (
    ("MOYAI_INSTALLATION_ID", "Installation ID (1–8 lowercase letters/digits)", False),
    ("MOYAI_PUBLIC_URL", "Public HTTPS origin", False),
    ("MOYAI_DATABASE_URL", "PostgreSQL connection URL", True),
    ("OBJECT_STORAGE_BUCKET", "Private S3 bucket", False),
    ("OBJECT_STORAGE_REGION", "S3 region", False),
    ("OBJECT_STORAGE_ENDPOINT", "S3 endpoint (blank for AWS)", False),
    ("OBJECT_STORAGE_ACCESS_KEY_ID", "S3 access key (blank for workload credentials)", True),
    ("OBJECT_STORAGE_SECRET_ACCESS_KEY", "S3 secret key (blank for workload credentials)", True),
    ("LITELLM_API_BASE", "Model gateway URL, including /v1", False),
    ("LITELLM_API_KEY", "Model gateway API key", True),
    ("AGENT_MODEL", "Responses-capable model name", False),
    ("MODAL_TOKEN_ID", "Modal token ID", True),
    ("MODAL_TOKEN_SECRET", "Modal token secret", True),
    ("WORKSPACE_PASSWORD", "Initial administrator password (blank to generate)", True),
)


def prompt(label: str, default: str, *, secret: bool = False) -> str:
    suffix = " [set; Enter keeps it]" if secret and default else f" [{default}]" if default else ""
    value = getpass.getpass(label + suffix + ": ") if secret else input(label + suffix + ": ")
    return value if value else default


def choice(label: str, options: tuple[str, ...], default: str) -> str:
    while True:
        value = prompt(f"{label} ({'/'.join(options)})", default)
        if value in options:
            return value
        print("Choose " + " or ".join(options) + ".")


def prepare(
    environment: Mapping[str, str], *, image: str, topology: str, temporal: str, secrets_from_env: bool,
) -> tuple[dict[str, str], dict | None]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", image):
        raise InstallationError("Select the image supplied with the release bundle.")
    if topology not in {"standalone", "split"} or temporal not in {"external", "bundled"}:
        raise InstallationError("Choose standalone/split and external/bundled Temporal.")
    values = {name: environment.get(name, default) for name, default in DEFAULTS.items()}
    values.update(MOYAI_IMAGE=image, MOYAI_INSTALL_TOPOLOGY=topology, MOYAI_TEMPORAL_MODE=temporal)
    name = values["MOYAI_INSTALLATION_ID"]
    values["COMPOSE_PROJECT_NAME"] = environment.get("COMPOSE_PROJECT_NAME") or f"moyai-{name}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", values["COMPOSE_PROJECT_NAME"]):
        raise InstallationError("COMPOSE_PROJECT_NAME must contain lowercase letters, digits, underscores or hyphens.")
    if not values["MOYAI_PORT"].isdigit() or not 1 <= int(values["MOYAI_PORT"]) <= 65535:
        raise InstallationError("MOYAI_PORT must be a valid TCP port.")
    files = ["compose.split.yaml" if topology == "split" else "compose.yaml"]
    generated_temporal_password = False
    if temporal == "bundled":
        files.append("compose.temporal.yaml")
        if not values["MOYAI_TEMPORAL_DATABASE_PASSWORD"]:
            values["MOYAI_TEMPORAL_DATABASE_PASSWORD"] = secrets.token_hex(24)
            generated_temporal_password = True
        if not re.fullmatch(r"[A-Za-z0-9_-]+", values["MOYAI_TEMPORAL_DATABASE_PASSWORD"]):
            raise InstallationError("Use letters, digits, underscores or hyphens for MOYAI_TEMPORAL_DATABASE_PASSWORD.")
        values.update(TEMPORAL_ADDRESS="", TEMPORAL_TLS="false", TEMPORAL_API_KEY="")
    if values["TEMPORAL_TLS"] not in {"true", "false"}:
        raise InstallationError("TEMPORAL_TLS must be true or false.")
    for field, value in values.items():
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise InstallationError(f"{field} cannot contain line breaks or NUL characters.")
    configured = configuration({**values, "MOYAI_BUILD_SHA": build_revision(environment)}, None)
    # Initialization generates and retains missing keys; these are only for offline validation.
    deployment_checks.validate(configuration(configured, new_state(values, configured)))
    overlay = None
    if secrets_from_env:
        services = ["setup", "app"] if topology == "standalone" else [
            "setup", "coordinator", "broker", "worker", "api", "activate",
        ]
        if temporal == "bundled":
            services.append("temporal-namespace")
        forwarded = [field for field in SECRETS if field != "MOYAI_TEMPORAL_DATABASE_PASSWORD"]
        overlay = {"services": {
            service: {"environment": dict.fromkeys(forwarded)} for service in services
        }}
        files.append(ENVIRONMENT_FILE)
        values = {
            field: value for field, value in values.items()
            if field not in SECRETS or field == "MOYAI_TEMPORAL_DATABASE_PASSWORD" and generated_temporal_password
        }
    values["COMPOSE_FILE"] = ":".join(files)
    values["COMPOSE_PATH_SEPARATOR"] = ":"
    return values, overlay


def write_configuration(directory: Path, values: dict[str, str], overlay: dict | None) -> None:
    """Publish complete files exclusively; an existing installation is never overwritten."""
    targets = {".env": "".join(f"{field}={value}\n" for field, value in values.items())}
    if overlay is not None:
        targets = {ENVIRONMENT_FILE: json.dumps(overlay, indent=2) + "\n", **targets}
    if any((directory / name).exists() or (directory / name).is_symlink() for name in (".env", ENVIRONMENT_FILE)):
        raise InstallationError("Configuration already exists. Edit it explicitly; use the upgrade procedure for a new image.")
    planned: dict[Path, tuple[int, int]] = {}

    def owned(path: Path) -> bool:
        try:
            stat = path.lstat()
            return planned.get(path) == (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            return False

    try:
        for name, content in targets.items():
            descriptor, temporary = tempfile.mkstemp(prefix=".moyai-config-", dir=directory)
            try:
                with os.fdopen(descriptor, "w") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                target = directory / name
                stat = Path(temporary).stat()
                planned[target] = stat.st_dev, stat.st_ino
                os.link(temporary, target)  # Fails rather than replacing a concurrently created file.
            finally:
                Path(temporary).unlink(missing_ok=True)
    finally:
        # An interrupt may arrive immediately after link() succeeds. Observe
        # ownership on disk rather than treating a lost acknowledgement as failure.
        if not owned(directory / ".env"):
            for path in planned:
                if owned(path):
                    path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.cwd())
    parser.add_argument("--image", required=True)
    parser.add_argument("--topology", choices=["standalone", "split"])
    parser.add_argument("--temporal", choices=["external", "bundled"])
    parser.add_argument("--from-env", action="store_true", help="Use exported settings without prompting.")
    parser.add_argument("--secrets-from-env", action="store_true", help="Keep credentials in the invoking environment.")
    args = parser.parse_args()
    try:
        directory = args.directory.resolve(strict=True)
        if (directory / ".env").exists() or (directory / ".env").is_symlink():
            raise InstallationError("Configuration already exists. It has not been changed.")
        metadata = directory / "release.json"
        if metadata.exists():
            release = json.loads(metadata.read_text())
            if (
                not isinstance(release, dict) or release.get("image") != args.image
                or release.get("revision") != build_revision(os.environ)
            ):
                raise InstallationError("The image and installation bundle do not describe the same release.")
        values = {field: os.environ.get(field, default) for field, default in DEFAULTS.items()}
        topology = args.topology or (
            os.environ.get("MOYAI_INSTALL_TOPOLOGY", "standalone") if args.from_env
            else choice("Application topology", ("standalone", "split"), "standalone")
        )
        temporal = args.temporal or (
            os.environ.get("MOYAI_TEMPORAL_MODE", "external") if args.from_env
            else choice("Temporal", ("bundled", "external"), "bundled")
        )
        if not args.from_env:
            for field, label, hidden in PROMPTS:
                if not (args.secrets_from_env and field in SECRETS):
                    values[field] = prompt(label, values[field], secret=hidden)
            if temporal == "external":
                for field, label in (
                    ("TEMPORAL_ADDRESS", "Temporal address (host:port)"),
                    ("TEMPORAL_NAMESPACE", "Existing Temporal namespace"),
                ):
                    values[field] = prompt(label, values[field])
                values["TEMPORAL_TLS"] = choice("Temporal TLS", ("true", "false"), values["TEMPORAL_TLS"])
                if not args.secrets_from_env:
                    values["TEMPORAL_API_KEY"] = prompt("Temporal API key (blank if not required)",
                                                       values["TEMPORAL_API_KEY"], secret=True)
        values.update(
            MOYAI_BUILD_SHA=build_revision(os.environ),
            COMPOSE_PROJECT_NAME=os.environ.get("COMPOSE_PROJECT_NAME", ""),
        )
        configured, overlay = prepare(values, image=args.image, topology=topology, temporal=temporal,
                                      secrets_from_env=args.secrets_from_env)
        write_configuration(directory, configured, overlay)
    except InstallationError as error:
        parser.exit(1, str(error) + "\n")
    except OSError:
        parser.exit(1, "Configuration could not be saved. Check the bundle and directory permissions.\n")
    except (ValueError, TypeError):
        parser.exit(1, "Configuration is invalid. Check the required settings and bundle metadata.\n")
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "Configuration interrupted. Check for .env before retrying.\n")
    print("Configuration saved with private permissions. Run: sh moyai.sh up")
    if args.secrets_from_env:
        print("Keep the required credentials exported when running the package.")


if __name__ == "__main__":
    main()
