"""Global historical demo plus trainable local product forecasting."""

from __future__ import annotations

import hashlib
import json
import math
import re
import os
from datetime import date, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent
GLOBAL_MODEL = ROOT / "models" / "store_forecaster.joblib"
GLOBAL_HISTORY = ROOT / "models" / "weekly_history.csv"
GLOBAL_METRICS = ROOT / "models" / "test_metrics.csv"
LOCAL_MODELS = ROOT / "data" / "models"
GLOBAL_REGISTRY = ROOT / "data" / "model_settings" / "global_active.json"
GLOBAL_REVISIONS = ROOT / "data" / "model_settings" / "global_versions"
MIN_HISTORY = 52


def _features(history: list[float] | np.ndarray, weeknum: int, time_index: int) -> dict:
    h = np.asarray(history, dtype=float)
    if len(h) < MIN_HISTORY:
        raise ValueError("At least 52 weeks of sales history are needed")
    out = {
        "time_index": time_index,
        "week_sin": np.sin(2 * np.pi * int(weeknum) / 53),
        "week_cos": np.cos(2 * np.pi * int(weeknum) / 53),
    }
    for lag in (1, 2, 3, 4, 8, 13, 26, 52):
        out[f"lag_{lag}"] = h[-lag]
    for window in (4, 8, 13, 26):
        out[f"mean_{window}"] = h[-window:].mean()
        out[f"std_{window}"] = h[-window:].std()
    return out


def _week_bucket(year: int, week: int) -> tuple[date, date, int]:
    jan1, dec31 = date(year, 1, 1), date(year, 12, 31)
    offset = (6 - jan1.weekday()) % 7 or 7
    next_sunday = jan1 + timedelta(days=offset)
    if week == 1:
        start, end = jan1, next_sunday - timedelta(days=1)
    else:
        start = next_sunday + timedelta(weeks=week - 2)
        end = min(start + timedelta(days=6), dec31)
    if start > dec31:
        raise ValueError("Week number exceeds this year")
    return start, end, (end - start).days + 1


def active_global_paths() -> tuple[Path, Path, Path]:
    if GLOBAL_REGISTRY.exists():
        revision = json.loads(GLOBAL_REGISTRY.read_text(encoding="utf-8")).get("active")
        if revision != "bundled" and isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{32}", revision):
            folder = GLOBAL_REVISIONS / revision
            return folder / "store_forecaster.joblib", folder / "weekly_history.csv", folder / "test_metrics.csv"
    return GLOBAL_MODEL, GLOBAL_HISTORY, GLOBAL_METRICS


def global_demo_forecast(horizon: int = 4, *, model_path: Path | None = None,
                         history_path: Path | None = None,
                         metrics_path: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Forecast weeks after 2014 using the original Kaggle model."""
    active_model, active_history, active_metrics = active_global_paths()
    model_path = model_path or active_model
    history_path = history_path or active_history
    metrics_path = metrics_path or active_metrics
    if not model_path.exists() or not history_path.exists():
        raise FileNotFoundError("Global demo model/history files are missing")
    if not 1 <= horizon <= 10:
        raise ValueError("Choose 1 to 10 weeks")
    bundle = joblib.load(model_path)
    history = pd.read_csv(history_path)
    model_name = bundle["model_name"]
    values = history["weekly_equivalent"].astype(float).tolist()
    last_year = int(history.iloc[-1]["Year"])
    next_year = last_year + 1
    rows = []
    for week in range(1, horizon + 1):
        start, end, days = _week_bucket(next_year, week)
        if model_name == "Last4":
            equivalent = values[-4]
        elif model_name == "Mean8":
            equivalent = float(np.mean(values[-8:]))
        else:
            X = pd.DataFrame([_features(values, week, len(values))])
            equivalent = float(np.expm1(bundle["model"].predict(X)[0]))
        equivalent = max(0.0, equivalent)
        values.append(equivalent)
        rows.append({"label": f"{next_year}-W{week:02d}",
                     "week_start": start.isoformat(), "week_end": end.isoformat(),
                     "days": days, "forecast_units": equivalent * days / 7})
    future = pd.DataFrame(rows)
    metrics = pd.read_csv(metrics_path) if metrics_path.exists() else pd.DataFrame()
    return history, future, metrics, bundle


def _local_model_path(sku: str) -> Path:
    digest = hashlib.sha256(sku.encode("utf-8")).hexdigest()[:20]
    return LOCAL_MODELS / f"{digest}.joblib"


def _local_pointer_path(sku: str) -> Path:
    return _local_model_path(sku).with_suffix(".active.json")


def active_local_model_path(sku: str) -> Path:
    pointer = _local_pointer_path(sku)
    if pointer.exists():
        revision = json.loads(pointer.read_text(encoding="utf-8")).get("active")
        if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{32}", revision):
            return LOCAL_MODELS / "versions" / _local_model_path(sku).stem / f"{revision}.joblib"
    return _local_model_path(sku)


def _train_ridge(history: pd.Series) -> object:
    y = history.to_numpy(dtype=float)
    X = pd.DataFrame([
        _features(y[:i], int(history.index[i].isocalendar().week), i)
        for i in range(MIN_HISTORY, len(y))
    ])
    model = make_pipeline(StandardScaler(), Ridge(alpha=30.0))
    model.fit(X, np.log1p(y[MIN_HISTORY:]))
    return model


def _predict_local(history: pd.Series, model: object, horizon: int) -> pd.DataFrame:
    values = history.to_numpy(dtype=float).tolist()
    dates = pd.date_range(history.index[-1] + pd.Timedelta(days=7),
                          periods=horizon, freq="W-SUN")
    preds = []
    for week_end in dates:
        X = pd.DataFrame([_features(values, int(week_end.isocalendar().week), len(values))])
        prediction = max(0.0, float(np.expm1(model.predict(X)[0])))
        values.append(prediction)
        preds.append(prediction)
    return pd.DataFrame({"week_end": dates, "forecast_units": preds})


def train_local_model(sku: str, history: pd.Series) -> dict:
    """Backtest latest 12 weeks, then fit a model on all observed weeks."""
    if len(history) < 80:
        raise ValueError(f"Need at least 80 consecutive weeks; this series has {len(history)}")
    split = len(history) - 12
    candidate = _train_ridge(history.iloc[:split])
    actual, predicted = [], []
    for origin in range(split, len(history), 4):
        block = history.iloc[origin:min(origin + 4, len(history))]
        guess = _predict_local(history.iloc[:origin], candidate, len(block))
        actual.extend(block.to_numpy(dtype=float))
        predicted.extend(guess["forecast_units"].to_numpy(dtype=float))
    actual_arr, pred_arr = np.asarray(actual), np.asarray(predicted)
    wape = float(np.abs(actual_arr - pred_arr).sum() / max(np.abs(actual_arr).sum(), 1e-9))
    baseline = []
    for origin in range(split, len(history), 4):
        block = history.iloc[origin:min(origin + 4, len(history))]
        last4 = history.iloc[origin - 4:origin].to_numpy(dtype=float)
        baseline.extend(last4[:len(block)])
    baseline_wape = float(np.abs(actual_arr - np.asarray(baseline)).sum()
                          / max(np.abs(actual_arr).sum(), 1e-9))
    selected = "Ridge" if wape < baseline_wape else "Last4"
    final_model = _train_ridge(history) if selected == "Ridge" else None
    artifact = {"model_name": selected, "model": final_model,
                "trained_through": history.index[-1].date().isoformat(),
                "test_wape": min(wape, baseline_wape),
                "ridge_test_wape": wape, "baseline_test_wape": baseline_wape,
                "sku": sku}
    LOCAL_MODELS.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, _local_model_path(sku))
    pointer = _local_pointer_path(sku)
    if pointer.exists():
        state = json.loads(pointer.read_text(encoding="utf-8"))
        new_state = {"active": "trained", "previous": state.get("active", "trained")}
        temporary = pointer.with_suffix(".tmp")
        temporary.write_text(json.dumps(new_state), encoding="utf-8")
        os.replace(temporary, pointer)
    return artifact


def local_forecast(sku: str, history: pd.Series,
                   horizon: int = 4) -> tuple[pd.DataFrame, dict]:
    if history.empty:
        raise ValueError("No sales history yet")
    dates = pd.date_range(history.index[-1] + pd.Timedelta(days=7),
                          periods=horizon, freq="W-SUN")
    if len(history) < 4:
        estimate = max(0.0, float(history.mean()))
        info = {"model_name": "Average weekly sales (limited history)",
                "trained_through": None, "test_wape": None}
        return pd.DataFrame({"week_end": dates, "forecast_units": estimate}), info
    path = active_local_model_path(sku)
    artifact = joblib.load(path) if path.exists() else None
    if artifact and artifact["trained_through"] != history.index[-1].date().isoformat():
        artifact = None  # New sales require explicit retraining.
    if artifact and artifact["model_name"] == "Ridge" and len(history) >= MIN_HISTORY:
        return _predict_local(history, artifact["model"], horizon), artifact
    values = history.to_numpy(dtype=float).tolist()
    preds = []
    for _ in dates:
        pred = max(0.0, float(values[-4]))
        values.append(pred)
        preds.append(pred)
    info = artifact or {"model_name": "Last4 baseline", "trained_through": None,
                        "test_wape": None}
    return pd.DataFrame({"week_end": dates, "forecast_units": preds}), info


def _backtest_window(history: pd.Series, start: int, end: int,
                     model: object | None = None) -> pd.DataFrame:
    """Forecast four weeks at a time, revealing actuals only after each block."""
    rows = []
    for origin in range(start, end, 4):
        actual = history.iloc[origin:min(origin + 4, end)]
        if model is None:
            predicted = history.iloc[origin - 4:origin].to_numpy(dtype=float)[:len(actual)]
        else:
            predicted = _predict_local(history.iloc[:origin], model, len(actual))[
                "forecast_units"].to_numpy(dtype=float)
        rows.extend({"week_end": week_end, "actual_units": float(value),
                     "forecast_units": float(guess)}
                    for (week_end, value), guess in zip(actual.items(), predicted))
    return pd.DataFrame(rows)


def _accuracy_metrics(predictions: pd.DataFrame, model_name: str) -> dict:
    actual = predictions["actual_units"].to_numpy(dtype=float)
    forecast = predictions["forecast_units"].to_numpy(dtype=float)
    absolute_error = np.abs(actual - forecast)
    total_actual = np.abs(actual).sum()
    return {"Model": model_name,
            "MAE": float(absolute_error.mean()),
            "RMSE": float(np.sqrt(np.mean((actual - forecast) ** 2))),
            "WAPE (%)": float(100 * absolute_error.sum() / total_actual)
            if total_actual > 0 else np.nan}


def evaluate_product_accuracy(history: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame, str, str]:
    """Independent chronological backtest; never fits on the displayed test weeks."""
    n = len(history)
    if n < 8:
        raise ValueError("At least eight recorded weeks are needed for an accuracy check")
    if n >= 80:
        validation_start, test_start = n - 24, n - 12
        validation_baseline = _backtest_window(history, validation_start, test_start)
        validation_model = _train_ridge(history.iloc[:validation_start])
        validation_ridge = _backtest_window(history, validation_start, test_start,
                                            validation_model)
        baseline_mae = _accuracy_metrics(validation_baseline, "Last4")["MAE"]
        ridge_mae = _accuracy_metrics(validation_ridge, "Ridge")["MAE"]
        selected = "Ridge" if ridge_mae < baseline_mae else "Last4"
        test_baseline = _backtest_window(history, test_start, n)
        test_model = _train_ridge(history.iloc[:test_start])
        test_ridge = _backtest_window(history, test_start, n, test_model)
        metrics = pd.DataFrame([_accuracy_metrics(test_baseline, "Last4"),
                                _accuracy_metrics(test_ridge, "Ridge")])
        predictions = test_ridge if selected == "Ridge" else test_baseline
        detail = "Ridge and Last4 were compared on the previous 12 weeks; the latest 12 weeks were reserved for this test."
    else:
        test_weeks = min(12, max(4, n // 4))
        predictions = _backtest_window(history, n - test_weeks, n)
        metrics = pd.DataFrame([_accuracy_metrics(predictions, "Last4")])
        selected = "Last4"
        detail = f"Only Last4 can be checked with this history. The latest {test_weeks} weeks were reserved for testing."
    predictions = predictions.copy()
    predictions["absolute_error"] = (predictions["actual_units"] -
                                      predictions["forecast_units"]).abs()
    return metrics, predictions, selected, detail


def simulate_inventory(stock: int, forecast: pd.DataFrame,
                       lead_time_days: int, safety_stock: int) -> tuple[pd.DataFrame, dict]:
    future = forecast.copy()
    future["projected_stock"] = stock - future["forecast_units"].cumsum()
    lead_weeks = max(1, math.ceil(lead_time_days / 7))
    demand_during_lead = float(future["forecast_units"].head(lead_weeks).sum())
    reorder_point = math.ceil(demand_during_lead + safety_stock)
    suggested_order = max(0, reorder_point - stock)
    return future, {"reorder_point": reorder_point,
                    "suggested_order": suggested_order,
                    "risk": "REORDER" if stock <= reorder_point else "LOW"}

