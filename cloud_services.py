"""AWS integrations. An EC2 instance role supplies credentials; never use keys in code."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
_models_restored = False


def bucket_name() -> str:
    return os.environ.get("AWS_S3_BUCKET", "").strip()


def sns_topic() -> str:
    return os.environ.get("SNS_TOPIC_ARN", "").strip()


def upload_sales_csv(payload: bytes, filename: str) -> tuple[str, str]:
    if not bucket_name():
        raise RuntimeError("AWS_S3_BUCKET is not configured")
    if not payload or len(payload) > 20 * 1024 * 1024:
        raise ValueError("Sales CSV must be between 1 byte and 20 MB")
    digest = hashlib.sha256(payload).hexdigest()
    key = f"sales-imports/{digest}.csv"
    import boto3

    safe_name = filename.encode("ascii", "ignore").decode("ascii")[:255] or "sales.csv"
    boto3.client("s3").put_object(
        Bucket=bucket_name(), Key=key, Body=payload, ContentType="text/csv",
        Metadata={"original-name": safe_name}, ServerSideEncryption="AES256",
    )
    return digest, key


def _model_files() -> list[Path]:
    paths = list((DATA / "models").rglob("*.joblib"))
    paths += list((DATA / "models").rglob("*.active.json"))
    paths += list((DATA / "model_settings" / "global_versions").rglob("*"))
    registry = DATA / "model_settings" / "global_active.json"
    if registry.exists():
        paths.append(registry)
    return [p for p in paths if p.is_file()]


def persist_models() -> None:
    """Upload model files before pointers so readers never see an incomplete version."""
    if not bucket_name():
        return
    import boto3

    client = boto3.client("s3")
    files = _model_files()
    for path in [p for p in files if not p.name.endswith(".active.json") and p.name != "global_active.json"]:
        client.upload_file(str(path), bucket_name(), "app-data/" + path.relative_to(DATA).as_posix())
    for path in [p for p in files if p.name.endswith(".active.json") or p.name == "global_active.json"]:
        client.upload_file(str(path), bucket_name(), "app-data/" + path.relative_to(DATA).as_posix())


def restore_models_once() -> None:
    """Restore durable model artifacts to EC2's working cache at process start."""
    global _models_restored
    if _models_restored or not bucket_name():
        return
    import boto3

    client = boto3.client("s3")
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name(), Prefix="app-data/"):
        for item in page.get("Contents", []):
            relative = item["Key"].removeprefix("app-data/")
            path = DATA / relative
            if not relative or not path.resolve().is_relative_to(DATA.resolve()):
                continue
            if relative == "model_settings/admin_key" or not (
                relative.startswith("models/") or relative.startswith("model_settings/global_")
            ):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".s3-download")
            client.download_file(bucket_name(), item["Key"], str(temporary))
            os.replace(temporary, path)
    _models_restored = True


def publish_alert(message: str) -> str:
    if not sns_topic():
        raise RuntimeError("SNS_TOPIC_ARN is not configured")
    import boto3

    result = boto3.client("sns").publish(
        TopicArn=sns_topic(), Subject="Store inventory reorder alert", Message=message,
    )
    return result["MessageId"]
