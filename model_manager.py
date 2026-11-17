"""Validate and activate administrator-supplied forecast artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import tempfile
import uuid
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

import database as db
import forecasting as fc
import cloud_services as cloud


MAX_MODEL_BYTES = 50 * 1024 * 1024
MAX_CSV_BYTES = 20 * 1024 * 1024
GLOBAL_NAMES = {"Last4", "Mean8", "Ridge", "ExtraTrees", "HistGradientBoosting"}


def admin_key() -> str:
    configured = os.environ.get("MODEL_ADMIN_PASSWORD")
    if configured:
        return configured
    path = db.DATA_DIR / "model_settings" / "admin_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(secrets.token_urlsafe(24))
        except FileExistsError:
            pass
    return path.read_text(encoding="utf-8").strip()


def _size_check(payload: bytes, limit: int, label: str) -> None:
    if not payload or len(payload) > limit:
        raise ValueError(f"{label} must be between 1 byte and {limit // (1024 * 1024)} MB")


def _read_global_history(payload: bytes) -> pd.DataFrame:
    _size_check(payload, MAX_CSV_BYTES, "History CSV")
    frame = pd.read_csv(io.BytesIO(payload))
    required = {"Year", "weeknum", "week_start", "label", "actual", "weekly_equivalent"}
    if not required.issubset(frame.columns) or len(frame) < fc.MIN_HISTORY:
        raise ValueError("History CSV needs the Kaggle weekly columns and at least 52 rows")
    if frame[list(required)].isna().any().any():
        raise ValueError("History CSV has missing values")
    for field in ("Year", "weeknum", "actual", "weekly_equivalent"):
        values = pd.to_numeric(frame[field], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy()).all():
            raise ValueError(f"History CSV has invalid {field} values")
    if pd.to_datetime(frame["week_start"], errors="coerce").isna().any():
        raise ValueError("History CSV has invalid week_start dates")
    return frame


def validate_global(model_bytes: bytes, history_bytes: bytes | None = None,
                    metrics_bytes: bytes | None = None) -> dict:
    _size_check(model_bytes, MAX_MODEL_BYTES, "Model file")
    # joblib is a pickle format. Only an unlocked administrator may reach this call.
    bundle = joblib.load(io.BytesIO(model_bytes))
    if not isinstance(bundle, dict) or bundle.get("model_name") not in GLOBAL_NAMES:
        raise ValueError("Expected a Kaggle forecast bundle with a supported model_name")
    if bundle["model_name"] not in {"Last4", "Mean8"} and not hasattr(bundle.get("model"), "predict"):
        raise ValueError("This Kaggle model has no usable predictor")
    _, current_history, _ = fc.active_global_paths()
    history_bytes = history_bytes if history_bytes is not None else current_history.read_bytes()
    history = _read_global_history(history_bytes)
    if bundle.get("last_label") != str(history.iloc[-1]["label"]):
        raise ValueError("Model and weekly history end at different weeks. Upload the matching history CSV.")
    if metrics_bytes is not None:
        _size_check(metrics_bytes, MAX_CSV_BYTES, "Metrics CSV")
        metrics = pd.read_csv(io.BytesIO(metrics_bytes))
        if not {"Model", "MAE", "RMSE", "WAPE"}.issubset(metrics.columns):
            raise ValueError("Metrics CSV needs Model, MAE, RMSE, and WAPE columns")
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        model_path = folder / "model.joblib"
        history_path = folder / "history.csv"
        model_path.write_bytes(model_bytes)
        history_path.write_bytes(history_bytes)
        _, future, _, _ = fc.global_demo_forecast(
            4, model_path=model_path, history_path=history_path,
            metrics_path=folder / "no_metrics.csv")
        if not np.isfinite(future["forecast_units"].to_numpy()).all():
            raise ValueError("Model produced non-finite predictions")
    return {"model_name": bundle["model_name"], "last_label": bundle["last_label"],
            "history_rows": len(history), "has_metrics": metrics_bytes is not None}


def validate_product(model_bytes: bytes, sku: str, history: pd.Series) -> dict:
    _size_check(model_bytes, MAX_MODEL_BYTES, "Model file")
    artifact = joblib.load(io.BytesIO(model_bytes))
    if not isinstance(artifact, dict) or artifact.get("model_name") not in {"Ridge", "Last4"}:
        raise ValueError("Expected a saved product forecast bundle (Ridge or Last4)")
    if artifact.get("sku") != sku:
        raise ValueError(f"This model belongs to {artifact.get('sku')!r}, not {sku!r}")
    if history.empty or artifact.get("trained_through") != history.index[-1].date().isoformat():
        raise ValueError("Model training date does not match this product's latest sales week")
    score = artifact.get("test_wape")
    if score is not None and (not isinstance(score, (int, float)) or
                              not np.isfinite(score) or score < 0):
        raise ValueError("Model test_wape must be a nonnegative finite number")
    if artifact["model_name"] == "Ridge":
        if len(history) < fc.MIN_HISTORY or not hasattr(artifact.get("model"), "predict"):
            raise ValueError("Ridge model needs a predictor and at least 52 sales weeks")
        sample = fc._predict_local(history, artifact["model"], 1)
        if not np.isfinite(sample["forecast_units"].to_numpy()).all():
            raise ValueError("Model produced non-finite predictions")
    elif len(history) < 4:
        raise ValueError("Last4 needs at least four recorded weeks")
    return {"model_name": artifact["model_name"], "sku": sku,
            "trained_through": artifact["trained_through"], "test_wape": score}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def _read_state(path: Path, default: str) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"active": default}


def global_state() -> dict:
    return _read_state(fc.GLOBAL_REGISTRY, "bundled")


def product_state(sku: str) -> dict:
    return _read_state(fc._local_pointer_path(sku), "trained")


def activate_global(model_bytes: bytes, history_bytes: bytes | None = None,
                    metrics_bytes: bytes | None = None) -> str:
    validate_global(model_bytes, history_bytes, metrics_bytes)
    revision = uuid.uuid4().hex
    folder = fc.GLOBAL_REVISIONS / revision
    folder.mkdir(parents=True, exist_ok=False)
    _, current_history, _ = fc.active_global_paths()
    (folder / "store_forecaster.joblib").write_bytes(model_bytes)
    (folder / "weekly_history.csv").write_bytes(
        history_bytes if history_bytes is not None else current_history.read_bytes())
    if metrics_bytes is not None:
        (folder / "test_metrics.csv").write_bytes(metrics_bytes)
    current = global_state()
    _write_json(fc.GLOBAL_REGISTRY, {"active": revision,
                                     "previous": current.get("active", "bundled")})
    cloud.persist_models()
    return revision


def restore_global() -> None:
    state = global_state()
    previous = state.get("previous")
    if previous is None:
        raise ValueError("No previous Kaggle model is available")
    _write_json(fc.GLOBAL_REGISTRY, {"active": previous, "previous": state["active"]})
    cloud.persist_models()


def activate_product(model_bytes: bytes, sku: str, history: pd.Series) -> str:
    validate_product(model_bytes, sku, history)
    revision = uuid.uuid4().hex
    folder = fc.LOCAL_MODELS / "versions" / fc._local_model_path(sku).stem
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{revision}.joblib").write_bytes(model_bytes)
    current = product_state(sku)
    _write_json(fc._local_pointer_path(sku), {"active": revision,
                                              "previous": current.get("active", "trained")})
    cloud.persist_models()
    return revision


def restore_product(sku: str) -> None:
    state = product_state(sku)
    previous = state.get("previous")
    if previous is None:
        raise ValueError("No previous product model is available")
    _write_json(fc._local_pointer_path(sku), {"active": previous,
                                              "previous": state["active"]})
    cloud.persist_models()


def fingerprint(*parts: bytes | str | None) -> str:
    digest = hashlib.sha256()
    for part in parts:
        payload = part.encode("utf-8") if isinstance(part, str) else part or b""
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()
