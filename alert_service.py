"""Automatic reorder checks and an SNS outbox."""

from __future__ import annotations

import math

import cloud_services as cloud
import database as db
import forecasting as fc


def check_product(sku: str) -> bool:
    products = db.table("products")
    inventory = db.table("inventory")
    match = products[products["sku"] == sku]
    stock = inventory[inventory["sku"] == sku]
    if match.empty or stock.empty:
        return False
    product = match.iloc[0]
    quantity = int(stock.iloc[0]["quantity"])
    safety = int(product["safety_stock"])
    lead_weeks = max(1, math.ceil(int(product["lead_time_days"]) / 7))
    history = db.weekly_sales(sku)
    if len(history) >= 4:
        forecast, _ = fc.local_forecast(sku, history, lead_weeks)
        reorder_point = math.ceil(float(forecast["forecast_units"].sum()) + safety)
    else:
        reorder_point = safety
    order = max(0, reorder_point - quantity)
    message = (f"Product {sku}: stock {quantity} units; reorder point "
               f"{reorder_point} units; suggested order {order} units.")
    return db.record_reorder_transition(sku, message, quantity <= reorder_point)


def deliver_pending() -> tuple[int, str | None]:
    """Keep undelivered alerts in RDS for a retry on the next app run."""
    if not cloud.sns_topic():
        return 0, None
    sent = 0
    for alert in db.pending_alerts():
        try:
            message_id = cloud.publish_alert(alert["message"])
            db.mark_alert_sent(int(alert["id"]), message_id)
            sent += 1
        except Exception as exc:
            return sent, str(exc)
    return sent, None
