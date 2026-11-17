"""S3 ObjectCreated handler for sales-imports/*.csv; deploy as handler.lambda_handler."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from datetime import date
from urllib.parse import unquote_plus

import boto3
import psycopg


STORE_ID = "__SINGLE_STORE__"
MAX_BYTES = 20 * 1024 * 1024


class InvalidCSV(ValueError):
    pass


def _connection():
    secret = json.loads(boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
    return psycopg.connect(
        host=os.environ.get("DB_HOST") or secret["host"],
        dbname=os.environ["DB_NAME"],
        user=os.environ.get("DB_USER") or secret["username"],
        password=secret["password"],
        port=int(os.environ.get("DB_PORT", secret.get("port", 5432))),
        sslmode="require", autocommit=True,
    )


def _validate(payload: bytes) -> list[tuple[str, str, int]]:
    if not payload or len(payload) > MAX_BYTES:
        raise InvalidCSV("Sales CSV must be between 1 byte and 20 MB")
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        if not reader.fieldnames:
            raise InvalidCSV("CSV header is missing")
        reader.fieldnames = [field.strip() for field in reader.fieldnames]
        required = {"sale_date", "sku", "quantity_sold"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise InvalidCSV("Missing CSV columns: " + ", ".join(sorted(missing)))
        if "branch_id" in reader.fieldnames:
            raise InvalidCSV("Remove branch_id; this app has one store")
        rows = []
        for number, row in enumerate(reader, start=2):
            if number > 200_002:
                raise InvalidCSV("CSV has too many rows (maximum 200,000)")
            raw_day = row["sale_date"].strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_day):
                raise InvalidCSV(f"Invalid date on row {number}; use YYYY-MM-DD")
            day = date.fromisoformat(raw_day).isoformat()
            sku = row["sku"].strip()
            raw_quantity = row["quantity_sold"].strip()
            if not sku or not raw_quantity.isdigit() or int(raw_quantity) < 1:
                raise InvalidCSV(f"Invalid SKU or quantity on row {number}")
            rows.append((day, sku, int(raw_quantity)))
        if not rows:
            raise InvalidCSV("CSV has no sales rows")
        return rows
    except (UnicodeDecodeError, csv.Error, KeyError, AttributeError, TypeError) as exc:
        raise InvalidCSV(f"Invalid CSV content: {exc}") from exc
    except ValueError as exc:
        if isinstance(exc, InvalidCSV):
            raise
        raise InvalidCSV(f"Invalid date or quantity: {exc}") from exc


def _job(conn, digest: str, filename: str, key: str, status: str, count=0, error=""):
    conn.execute("""INSERT INTO import_jobs(file_hash,file_name,s3_key,status,imported_rows,error)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT(file_hash) DO UPDATE SET status=EXCLUDED.status,
        imported_rows=EXCLUDED.imported_rows,error=EXCLUDED.error,
        updated_at=CURRENT_TIMESTAMP""",
        (digest, filename[:255], key, status, count, error[:1000]))


def _process(bucket: str, key: str) -> dict:
    if not key.startswith("sales-imports/") or not key.endswith(".csv"):
        return {"skipped": key}
    obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    if obj.get("ContentLength", 0) > MAX_BYTES:
        raise InvalidCSV("Sales CSV exceeds 20 MB")
    payload = obj["Body"].read(MAX_BYTES + 1)
    digest = hashlib.sha256(payload).hexdigest()
    if key != f"sales-imports/{digest}.csv":
        raise InvalidCSV("S3 object key does not match CSV checksum")
    filename = obj.get("Metadata", {}).get("original-name", key.rsplit("/", 1)[-1])
    with _connection() as conn:
        _job(conn, digest, filename, key, "processing")
        try:
            rows = _validate(payload)
            with conn.transaction():
                known = {r[0] for r in conn.execute("SELECT sku FROM products")}
                missing = {sku for _, sku, _ in rows} - known
                if missing:
                    raise InvalidCSV("Create products first. Missing SKUs: " + ", ".join(sorted(missing)))
                inserted = conn.execute("""INSERT INTO imports(file_hash,file_name,imported_rows)
                    VALUES (%s,%s,%s) ON CONFLICT(file_hash) DO NOTHING
                    RETURNING file_hash""", (digest, filename[:255], len(rows))).fetchone()
                if inserted:
                    with conn.cursor() as cursor:
                        cursor.executemany("""INSERT INTO sales
                            (sale_date,branch_id,sku,quantity,source,source_key)
                            VALUES (%s,%s,%s,%s,'csv',%s)""",
                            [(day, STORE_ID, sku, qty, f"{digest}:{index}")
                             for index, (day, sku, qty) in enumerate(rows)])
                _job(conn, digest, filename, key, "completed", len(rows))
            return {"file": filename, "imported_rows": len(rows) if inserted else 0}
        except InvalidCSV as exc:
            _job(conn, digest, filename, key, "failed", error=str(exc))
            return {"file": filename, "error": str(exc)}
        except Exception as exc:
            _job(conn, digest, filename, key, "failed", error=str(exc))
            raise


def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        if bucket != os.environ["AWS_S3_BUCKET"]:
            raise ValueError("Unexpected S3 bucket")
        results.append(_process(bucket, key))
    return {"results": results}
