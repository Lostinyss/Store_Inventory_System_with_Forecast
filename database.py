"""Store data: local SQLite by default, PostgreSQL when DB_HOST is configured."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "store.db"
STORE_ID = "__SINGLE_STORE__"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS branches (
                branch_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                storage_location TEXT NOT NULL DEFAULT '',
                lead_time_days INTEGER NOT NULL DEFAULT 7 CHECK (lead_time_days >= 0),
                safety_stock INTEGER NOT NULL DEFAULT 0 CHECK (safety_stock >= 0)
            );
            CREATE TABLE IF NOT EXISTS inventory (
                branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
                sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
                quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                PRIMARY KEY (branch_id, sku)
            );
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_date TEXT NOT NULL,
                branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
                sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                source TEXT NOT NULL,
                source_key TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS sales_series_date
                ON sales(branch_id, sku, sale_date);
            CREATE TABLE IF NOT EXISTS imports (
                file_hash TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                imported_rows INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Consolidate records once, preserving a copy of the pre-conversion DB.
        if db.execute("SELECT COUNT(*) FROM branches WHERE branch_id != ?", (STORE_ID,)).fetchone()[0]:
            backup = DB_PATH.with_name("store_before_single_store.db")
            if not backup.exists():
                with sqlite3.connect(backup) as copy:
                    db.backup(copy)
            db.execute("INSERT OR IGNORE INTO branches(branch_id,name,location) VALUES(?,?,?)",
                       (STORE_ID, "My store", ""))
            db.execute("""INSERT INTO inventory(branch_id,sku,quantity)
                SELECT ?, sku, SUM(quantity) FROM inventory WHERE branch_id != ? GROUP BY sku
                ON CONFLICT(branch_id,sku) DO UPDATE SET quantity=quantity+excluded.quantity""",
                (STORE_ID, STORE_ID))
            db.execute("DELETE FROM inventory WHERE branch_id != ?", (STORE_ID,))
            db.execute("UPDATE sales SET branch_id=? WHERE branch_id != ?", (STORE_ID, STORE_ID))
            db.execute("UPDATE alerts SET branch_id=? WHERE branch_id != ?", (STORE_ID, STORE_ID))
            db.execute("DELETE FROM branches WHERE branch_id != ?", (STORE_ID,))
        else:
            db.execute("INSERT OR IGNORE INTO branches(branch_id,name,location) VALUES(?,?,?)",
                       (STORE_ID, "My store", ""))


def table(name: str) -> pd.DataFrame:
    allowed = {
        "products": "SELECT * FROM products ORDER BY sku",
        "inventory": """
            SELECT i.sku, p.name AS product_name, i.quantity, p.safety_stock,
                   p.lead_time_days
            FROM inventory i JOIN products p USING(sku)
            WHERE i.branch_id='__SINGLE_STORE__' ORDER BY i.sku
        """,
        "sales": "SELECT sale_date, sku, quantity, source FROM sales ORDER BY sale_date DESC, id DESC",
        "imports": "SELECT * FROM imports ORDER BY created_at DESC",
        "alerts": "SELECT id, sku, message, created_at FROM alerts ORDER BY id DESC",
    }
    if name not in allowed:
        raise ValueError("Unknown table")
    with connect() as db:
        return pd.read_sql_query(allowed[name], db)


def save_product(sku: str, name: str, category: str, storage_location: str,
                 lead_time_days: int, safety_stock: int) -> None:
    sku, name = sku.strip(), name.strip()
    if not sku or not name:
        raise ValueError("SKU and product name are required")
    with connect() as db:
        db.execute("""INSERT INTO products
            (sku,name,category,storage_location,lead_time_days,safety_stock)
            VALUES(?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET
            name=excluded.name, category=excluded.category,
            storage_location=excluded.storage_location,
            lead_time_days=excluded.lead_time_days,
            safety_stock=excluded.safety_stock""",
            (sku, name, category.strip(), storage_location.strip(),
             int(lead_time_days), int(safety_stock)))


def delete_product(sku: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM products WHERE sku=?", (sku,))


def set_inventory(sku: str, quantity: int) -> None:
    if quantity < 0:
        raise ValueError("Stock cannot be negative")
    with connect() as db:
        db.execute("""INSERT INTO inventory(branch_id,sku,quantity) VALUES(?,?,?)
            ON CONFLICT(branch_id,sku) DO UPDATE SET quantity=excluded.quantity""",
            (STORE_ID, sku, int(quantity)))


def delete_inventory(sku: str) -> None:
    """Remove only the current-stock record; historical sales remain intact."""
    with connect() as db:
        db.execute("DELETE FROM inventory WHERE branch_id=? AND sku=?",
                   (STORE_ID, sku))


def add_sale(sale_date: date, sku: str, quantity: int,
             reduce_stock: bool = False) -> None:
    if quantity <= 0:
        raise ValueError("Quantity sold must be positive")
    with connect() as db:
        if reduce_stock:
            updated = db.execute("""UPDATE inventory SET quantity=quantity-?
                WHERE branch_id=? AND sku=? AND quantity>=?""",
                (quantity, STORE_ID, sku, quantity)).rowcount
            if updated != 1:
                raise ValueError("Not enough stock recorded for this product")
        db.execute("""INSERT INTO sales
            (sale_date,branch_id,sku,quantity,source,source_key)
            VALUES(?,?,?,?,'manual',?)""",
            (sale_date.isoformat(), STORE_ID, sku, int(quantity), uuid.uuid4().hex))


def import_sales_csv(file_bytes: bytes, file_name: str) -> int:
    """Validate the full file before an atomic, idempotent import."""
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    with connect() as db:
        if db.execute("SELECT 1 FROM imports WHERE file_hash=?", (file_hash,)).fetchone():
            return 0
    try:
        frame = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
    except Exception as exc:
        raise ValueError(f"Cannot read CSV: {exc}") from exc
    frame.columns = frame.columns.str.strip()
    required = {"sale_date", "sku", "quantity_sold"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Missing CSV columns: {', '.join(sorted(required - set(frame.columns)))}")
    if frame.empty:
        raise ValueError("CSV is empty")
    if "branch_id" in frame.columns:
        raise ValueError("Remove branch_id from the CSV. This app now has one store.")
    parsed_dates = pd.to_datetime(frame["sale_date"], format="%Y-%m-%d", errors="coerce")
    qty = pd.to_numeric(frame["quantity_sold"], errors="coerce")
    if parsed_dates.isna().any():
        raise ValueError("sale_date must use YYYY-MM-DD in every row")
    if qty.isna().any() or (qty <= 0).any() or (qty % 1 != 0).any():
        raise ValueError("quantity_sold must be a positive whole number")
    sku = frame["sku"].fillna("").str.strip()
    if (sku == "").any():
        raise ValueError("sku cannot be blank")
    with connect() as db:
        known_skus = {r[0] for r in db.execute("SELECT sku FROM products")}
        missing_skus = set(sku) - known_skus
        if missing_skus:
            raise ValueError(f"Create products first. Missing SKUs: {sorted(missing_skus)}")
        rows = [(parsed_dates.iloc[i].date().isoformat(), STORE_ID,
                 sku.iloc[i], int(qty.iloc[i]), "csv", f"{file_hash}:{i}")
                for i in range(len(frame))]
        db.executemany("""INSERT INTO sales
            (sale_date,branch_id,sku,quantity,source,source_key)
            VALUES(?,?,?,?,?,?)""", rows)
        db.execute("INSERT INTO imports(file_hash,file_name,imported_rows) VALUES(?,?,?)",
                   (file_hash, file_name, len(rows)))
    upload_dir = DATA_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{file_hash}.csv").write_bytes(file_bytes)
    return len(frame)


def weekly_sales(sku: str) -> pd.Series:
    with connect() as db:
        frame = pd.read_sql_query("""SELECT sale_date,quantity FROM sales
            WHERE branch_id=? AND sku=? ORDER BY sale_date""",
            db, params=(STORE_ID, sku))
    if frame.empty:
        return pd.Series(dtype=float)
    frame["sale_date"] = pd.to_datetime(frame["sale_date"])
    weekly = frame.groupby(pd.Grouper(key="sale_date", freq="W-SUN"))["quantity"].sum()
    return weekly.asfreq("W-SUN", fill_value=0).astype(float)


def log_alert(sku: str, message: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO alerts(branch_id,sku,message) VALUES(?,?,?)",
                   (STORE_ID, sku, message))


BACKEND = "sqlite"
if os.environ.get("DB_HOST"):
    from postgres_database import (  # noqa: F401
        add_sale, connect, delete_inventory, delete_product, import_sales_csv,
        init_db, log_alert, save_product, set_inventory, table, weekly_sales,
    )

    BACKEND = "postgresql"

