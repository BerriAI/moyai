"""Package only the current native workspace tools and Lambda transport."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import zipfile

import boto3

ROOT = Path(__file__).resolve().parents[1]


def package(destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "native/lambda_guest").iterdir()):
            if path.is_file() and path.name in {"Dockerfile", "service.py", "checkpoint.py"}:
                archive.write(path, path.name)
        for name in ("desktop.py", "browser_guest.py"):
            archive.write(ROOT / "native" / name, name)
        for path in sorted((ROOT / "native/skills").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT / "native"))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def image_request(region: str, name: str, bucket: str, key: str, role: str) -> dict:
    return {
        "name": name, "clientToken": uuid4().hex,
        "baseImageArn": f"arn:aws:lambda:{region}:aws:microvm-image:al2023-1",
        "buildRoleArn": role, "codeArtifact": {"uri": f"s3://{bucket}/{key}"},
        "cpuConfigurations": [{"architecture": "ARM_64"}],
        "resources": [{"minimumMemoryInMiB": 8192}],
        "egressNetworkConnectors": [
            f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS",
        ],
        "hooks": {"port": 80, "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 1800}},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".data/lambda-native-image.zip")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--name", default="moyai-native-workspace")
    parser.add_argument("--bucket")
    parser.add_argument("--build-role-arn")
    args = parser.parse_args()
    if args.create and (not args.bucket or not args.build_role_arn):
        parser.error("--create requires --bucket and --build-role-arn")
    digest = package(args.output)
    print(json.dumps({"package": str(args.output), "sha256": digest}))
    if args.create:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        key = f"moyai-image-builds/{digest}.zip"
        with closing(session.client("s3")) as s3:
            s3.upload_file(str(args.output), args.bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
        with closing(session.client("lambda-microvms")) as client:
            result = client.create_microvm_image(**image_request(
                args.region, args.name, args.bucket, key, args.build_role_arn,
            ))
        print(json.dumps({key: result.get(key) for key in ("imageArn", "imageVersion", "state")}))
