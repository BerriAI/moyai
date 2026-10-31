"""Complete the Terraform quickstart without putting credentials in state or SSM parameters."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "native/src"))
sys.path.insert(0, str(ROOT))
from scripts.build_docker_bundle import FILES
from scripts.lambda_image import image_request, package


def load_module():
    spec = importlib.util.spec_from_file_location("aws_bootstrap", HERE / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings(directory: Path, terraform: str) -> dict:
    result = subprocess.run([terraform, f"-chdir={directory}", "output", "-json", "quickstart"], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def json_object(s3, bucket: str, key: str) -> dict | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def upload(s3, bucket: str, key: str, content: bytes) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=content, ServerSideEncryption="AES256")


def initialize_secret(session, config: dict, supplied: Path | None) -> None:
    manager = session.client("secretsmanager")
    requested = json.loads(supplied.read_text()) if supplied else {}
    if not isinstance(requested, dict):
        raise TypeError("Supply a JSON object of runtime settings")
    try:
        current = json.loads(manager.get_secret_value(SecretId=config["secret_arn"])["SecretString"])
    except manager.exceptions.ResourceNotFoundException:
        current = None
    if current is not None:
        load_module().validate_secret(current)
        if any(current.get(key) != value for key, value in requested.items()):
            raise ValueError("Runtime secret already exists with different values; it was not overwritten")
        print("Existing runtime secret retained.")
        return
    for name in ("WORKSPACE_PASSWORD", "database_password", "gateway_key"):
        requested.setdefault(name, secrets.token_hex(32))
    load_module().validate_secret(requested)
    manager.put_secret_value(SecretId=config["secret_arn"], SecretString=json.dumps(requested))
    print("Runtime credentials stored in Secrets Manager. Retrieve WORKSPACE_PASSWORD there; it is never printed.")


def guest_archive(destination: Path) -> str:
    """Reuse the canonical packager, normalizing ZIP timestamps for stable retries."""
    raw = destination.with_suffix(".raw.zip")
    package(raw)
    with zipfile.ZipFile(raw) as source, zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name in sorted(source.namelist()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, source.read(name))
    raw.unlink()
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def build_image(session, config: dict, directory: Path) -> dict:
    s3 = session.client("s3")
    client = session.client("lambda-microvms")
    archive = directory / "guest.zip"
    digest = guest_archive(archive)
    key = f"moyai-image-builds/{digest}.zip"
    uri = f"s3://{config['bucket']}/{key}"
    receipt_key = "deployments/guest-image.json"
    receipt = json_object(s3, config["bucket"], receipt_key)
    if receipt and receipt["sha256"] != digest:
        raise ValueError("The pinned guest source changed. Build a new named image deliberately; existing checkpoints still need their original version.")
    if receipt is None:
        upload(s3, config["bucket"], key, archive.read_bytes())
        request = image_request(config["region"], config["name"] + "-workspace", config["bucket"], key, config["image_build_role_arn"])
        request["clientToken"] = digest
        try:
            result = client.create_microvm_image(**request)
        except client.exceptions.ConflictException:
            # Recover a lost create reply only if the existing version uses this artifact.
            result = client.get_microvm_image_version(imageIdentifier=config["image_arn"], imageVersion="1.0")
        if result.get("codeArtifact", {}).get("uri") != uri:
            raise ValueError("Existing image does not match this checkout's guest artifact")
        receipt = {"image_arn": result["imageArn"], "image_version": result["imageVersion"], "sha256": digest}
        upload(s3, config["bucket"], receipt_key, json.dumps(receipt).encode())
    for _ in range(240):
        result = client.get_microvm_image_version(imageIdentifier=receipt["image_arn"], imageVersion=receipt["image_version"])
        if result["state"] == "SUCCESSFUL":
            print(f"Guest image ready: {receipt['image_arn']} version {receipt['image_version']}")
            return receipt
        if result["state"] in {"FAILED", "DELETED", "DELETING"}:
            raise RuntimeError("Guest build failed; inspect this image's AWS build logs")
        print("Waiting for guest image build: " + result["state"], flush=True)
        time.sleep(15)
    raise TimeoutError("Guest image build still pending; rerun to observe the same build")


def application_bundle() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name in FILES:
            path = ROOT / "deploy/docker" / name
            if not path.is_file() or path.is_symlink():
                raise ValueError("Missing or symlinked deployment input: " + name)
            content = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(output.getvalue(), mtime=0)


def start_install(session, config: dict, receipt: dict) -> str:
    s3 = session.client("s3")
    bundle = application_bundle()
    digest = hashlib.sha256(bundle).hexdigest()
    prefix = f"deployments/{digest}"
    bootstrap = (HERE / "bootstrap.py").read_bytes()
    bootstrap_digest = hashlib.sha256(bootstrap).hexdigest()
    bootstrap_key = f"deployments/bootstrap/{bootstrap_digest}.py"
    manifest = {**config, **receipt, "bundle_sha256": digest}
    encoded = json.dumps(manifest, sort_keys=True).encode()
    config_key = f"deployments/config/{hashlib.sha256(encoded).hexdigest()}.json"
    upload(s3, config["bucket"], prefix + "/bundle.tar.gz", bundle)
    upload(s3, config["bucket"], bootstrap_key, bootstrap)
    upload(s3, config["bucket"], config_key, encoded)
    # SSM parameters contain only identifiers and hashes. Secret values are fetched on-host.
    def copy(key: str, path: str) -> str:
        return shlex.join(["aws", "--region", config["region"], "s3", "cp", f"s3://{config['bucket']}/{key}", path, "--only-show-errors"])
    command = "\n".join([
        "set -eu", "umask 077", "install -d -m 700 /opt/moyai-aws/install",
        "cd /opt/moyai-aws/install", "flock -n 9 || exit 1",
        copy(prefix + "/bundle.tar.gz", "bundle.tar.gz"), copy(bootstrap_key, "bootstrap.py"), copy(config_key, "config.json"),
        f"echo '{bootstrap_digest}  bootstrap.py' | sha256sum -c -",
        "python3 bootstrap.py --config config.json --archive bundle.tar.gz",
    ])
    # The lock covers the whole script, including downloads into its private directory.
    wrapped = "install -d -m 700 /opt/moyai-aws\n" + "(\n" + command + "\n) 9>/opt/moyai-aws/install.lock"
    result = session.client("ssm").send_command(InstanceIds=[config["instance_id"]], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [wrapped], "executionTimeout": ["1800"]}, Comment="Moyai Terraform quickstart installation")
    return result["Command"]["CommandId"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["secrets", "image", "install", "status"])
    parser.add_argument("--directory", type=Path, default=HERE)
    parser.add_argument("--terraform", default="terraform")
    parser.add_argument("--profile")
    parser.add_argument("--secret-file", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    config = settings(args.directory, args.terraform)
    directory = args.directory / ".quickstart"
    directory.mkdir(mode=0o700, exist_ok=True)
    session = boto3.Session(profile_name=args.profile, region_name=config["region"])
    if session.client("sts").get_caller_identity()["Account"] != config["account"]:
        raise ValueError("AWS credentials do not match the Terraform account")
    if args.action == "secrets":
        initialize_secret(session, config, args.secret_file)
    elif args.action == "image":
        build_image(session, config, directory)
    elif args.action == "install":
        receipt = build_image(session, config, directory)
        command_id = start_install(session, config, receipt)
        (directory / "command-id").write_text(command_id)
        print("Installation started: " + command_id + ". Run the status command to check completion.")
    else:
        command_id = (directory / "command-id").read_text().strip()
        result = session.client("ssm").get_command_invocation(CommandId=command_id, InstanceId=config["instance_id"])
        print(json.dumps({"command_id": command_id, "status": result["Status"], "exit_code": result["ResponseCode"]}))
        if result["Status"] == "Success":
            print(config["url"] + " — sign in, then install Slack through Connections.")
        elif result["Status"] in {"Failed", "Cancelled", "TimedOut"}:
            raise SystemExit("Installation did not finish. Inspect the SSM invocation and host service logs; secrets are not printed here.")


if __name__ == "__main__":
    main()
