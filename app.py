"""Local Store Forecast Studio. Run: streamlit run app.py"""

from __future__ import annotations

import sqlite3
import math
import hmac
from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError

import database as db
import forecasting as fc
import model_manager as mm


st.set_page_config(page_title="Store Forecast Studio", page_icon="📦", layout="wide")
db.init_db()

st.markdown("""
<style>
.block-container {max-width: 1260px; padding-top: 2rem;}
.note {padding: .75rem 1rem; border-radius: 10px; background: #eaf4f6;
       color: #244b53; margin-bottom: 1rem;}
[data-testid="stSidebar"] button[kind="primary"] {
    background: #147c83; color: white; border-color: #147c83;
    font-weight: 700; box-shadow: inset 4px 0 0 #83e3dd;
}
[data-testid="stSidebar"] button[kind="secondary"] {
    background: transparent; border-color: transparent; text-align: left;
}
[data-testid="stSidebar"] button[kind="secondary"]:hover {
    background: #d8e9ea; border-color: #b5d6d8;
}
</style>
""", unsafe_allow_html=True)

PAGES = ["Overview", "Inventory", "Sales", "Forecast", "Accuracy", "Alerts", "Settings"]
if st.session_state.get("page") not in PAGES:
    st.session_state.page = PAGES[0]


def navigate(destination: str) -> None:
    st.session_state.page = destination


with st.sidebar:
    st.title("📦 Store Forecast")
    st.caption("WORKSPACE")
    for index, destination in enumerate(PAGES):
        active = st.session_state.page == destination
        st.button(destination, key=f"nav_{index}",
                  type="primary" if active else "secondary",
                  width="stretch", on_click=navigate, args=(destination,))
    page = st.session_state.page
    st.divider()
    st.caption(f"{'RDS PostgreSQL' if db.BACKEND == 'postgresql' else 'Local SQLite'} · local model and upload files")

def dataframe(frame: pd.DataFrame) -> None:
    st.dataframe(frame, width="stretch", hide_index=True)


def failure(exc: Exception) -> None:
    if isinstance(exc, (sqlite3.IntegrityError, DatabaseIntegrityError)):
        st.error("This product is in use by stock or sales records. Remove those references first.")
    else:
        st.error(str(exc))


@st.dialog("Product details", width="small")
def product_editor(sku: str | None = None) -> None:
    products = db.table("products")
    row = products.set_index("sku").loc[sku] if sku is not None else None
    st.subheader("Edit product" if row is not None else "Add product")
    with st.form("product_dialog_form"):
        chosen_sku = st.text_input("SKU", value="" if row is None else sku,
                                   disabled=row is not None)
        name = st.text_input("Product name", value="" if row is None else row["name"])
        category = st.text_input("Category", value="" if row is None else row["category"])
        storage = st.text_input("Storage location", value="" if row is None else row["storage_location"])
        lead = st.number_input("Supplier lead time (days)", min_value=0,
                               value=7 if row is None else int(row["lead_time_days"]))
        safety = st.number_input("Safety stock (units)", min_value=0,
                                 value=0 if row is None else int(row["safety_stock"]))
        save = st.form_submit_button("Save changes" if row is not None else "Add product",
                                     type="primary")
    if save:
        try:
            db.save_product(chosen_sku, name, category, storage, int(lead), int(safety))
            st.rerun()
        except Exception as exc:
            failure(exc)


@st.dialog("Stock details", width="small")
def inventory_editor(sku: str | None = None) -> None:
    products = db.table("products")
    if products.empty:
        st.warning("Create a product first.")
        return
    editing = sku is not None
    current = 0
    if editing:
        matches = db.table("inventory")
        matches = matches[matches["sku"] == sku]
        if matches.empty:
            st.error("This stock record no longer exists.")
            return
        current = int(matches.iloc[0]["quantity"])
    st.subheader("Edit stock" if editing else "Set stock")
    skus = products["sku"].tolist()
    with st.form("stock_dialog_form"):
        chosen_sku = st.selectbox("Product", skus,
                                  index=skus.index(sku) if editing else 0,
                                  disabled=editing)
        quantity = st.number_input("Current stock", min_value=0, value=current, step=1)
        save = st.form_submit_button("Save stock", type="primary")
    if save:
        try:
            db.set_inventory(chosen_sku, int(quantity))
            st.rerun()
        except Exception as exc:
            failure(exc)


@st.dialog("Record sale", width="small")
def record_sale_dialog() -> None:
    products = db.table("products")
    with st.form("sale_dialog_form"):
        sale_date = st.date_input("Date of sale", value=date.today())
        sku = st.selectbox("Product", products["sku"].tolist())
        quantity = st.number_input("Units sold", min_value=1, value=1, step=1)
        reduce_stock = st.checkbox("Also subtract from current stock", value=True)
        save = st.form_submit_button("Record sale", type="primary")
    if save:
        try:
            db.add_sale(sale_date, sku, int(quantity), reduce_stock)
            st.session_state.sales_notice = "Sale recorded"
            st.rerun()
        except Exception as exc:
            failure(exc)


@st.dialog("Import sales CSV", width="small")
def import_sales_dialog() -> None:
    st.caption("Required columns: sale_date, sku, quantity_sold. Use YYYY-MM-DD dates.")
    template = b"sale_date,sku,quantity_sold\n2026-10-01,YOUR-SKU,8\n"
    st.download_button("Download CSV template", template, "sales_template.csv", "text/csv")
    uploaded = st.file_uploader("Sales CSV", type="csv")
    if st.button("Validate and import", type="primary", disabled=uploaded is None):
        try:
            count = db.import_sales_csv(uploaded.getvalue(), uploaded.name)
            if count == 0:
                st.info("This exact file was already imported; no duplicate sales were added.")
            else:
                st.session_state.sales_notice = f"Imported {count} sales rows"
                st.rerun()
        except Exception as exc:
            failure(exc)


@st.dialog("Confirm deletion", width="small")
def delete_dialog(kind: str, sku: str) -> None:
    if kind == "product":
        label = f"product {sku}"
    else:
        label = f"stock for {sku}"
    st.warning(f"Delete {label}? This action cannot be undone.")
    if kind == "inventory":
        st.caption("Historical sales will stay in the database.")
    cancel_col, delete_col = st.columns(2)
    if cancel_col.button("Cancel", width="stretch"):
        st.rerun()
    if delete_col.button("Delete", type="primary", width="stretch"):
        try:
            if kind == "product":
                db.delete_product(sku)
            elif kind == "inventory":
                db.delete_inventory(sku)
            else:
                raise ValueError("Unknown record type")
            st.rerun()
        except Exception as exc:
            failure(exc)


def visible_rows(frame: pd.DataFrame, key: str, size: int = 12) -> list[dict]:
    if frame.empty:
        return []
    page_count = math.ceil(len(frame) / size)
    if page_count == 1:
        return frame.to_dict("records")
    state_key = f"{key}_page"
    if st.session_state.get(state_key, 1) > page_count:
        st.session_state[state_key] = page_count
    page_number = st.selectbox("Page", range(1, page_count + 1),
                               key=state_key, format_func=lambda p: f"{p} of {page_count}")
    return frame.iloc[(page_number - 1) * size:page_number * size].to_dict("records")


if page == "Overview":
    st.subheader("Operations overview")
    products = db.table("products")
    inventory = db.table("inventory")
    sales = db.table("sales")
    low = inventory[inventory["quantity"] <= inventory["safety_stock"]] if not inventory.empty else inventory
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Products", len(products))
    c2.metric("Stock records", len(inventory))
    c3.metric("Units in stock", int(inventory["quantity"].sum()) if not inventory.empty else 0)
    c4.metric("At or below safety stock", len(low))
    st.markdown("<div class='note'>Product-level forecasts use sales entered in this app. The original Kaggle model is a separate historical, all-products demo.</div>", unsafe_allow_html=True)
    left, right = st.columns([1, 1])
    with left:
        st.markdown("#### Stock by SKU")
        dataframe(inventory)
    with right:
        st.markdown("#### Latest sales")
        dataframe(sales.head(15))
    if inventory.empty:
        st.info("Add a product and set its current stock on the Inventory page.")

elif page == "Inventory":
    st.subheader("Products and current stock")
    tab_p, tab_i = st.tabs(["Products", "Stock"])
    with tab_p:
        products = db.table("products")
        top_left, top_right = st.columns([3, 2])
        top_left.markdown("#### Products")
        if top_right.button("＋ Add product", type="primary", width="stretch"):
            product_editor()
        if products.empty:
            st.info("No products yet. Add one to get started.")
        for row in visible_rows(products, "product"):
            with st.container(border=True):
                info_col, edit_col, delete_col = st.columns([3.4, 1.2, 1.2])
                info_col.write(f"**{row['sku']}** · {row['name']}")
                info_col.caption(
                    f"{row['category'] or 'Uncategorized'} · "
                    f"{row['storage_location'] or 'No location'} · "
                    f"Lead {row['lead_time_days']} days · Safety {row['safety_stock']} units")
                if edit_col.button("Edit", key=f"product_edit_{row['sku']}", width="stretch"):
                    product_editor(row["sku"])
                if delete_col.button("Delete", key=f"product_delete_{row['sku']}", width="stretch"):
                    delete_dialog("product", sku=row["sku"])

    with tab_i:
        inventory = db.table("inventory")
        products = db.table("products")
        top_left, top_right = st.columns([3, 2])
        top_left.markdown("#### Inventory")
        if top_right.button("＋ Set stock", type="primary", width="stretch",
                            disabled=products.empty):
            inventory_editor()
        if products.empty:
            st.info("Create a product first.")
        elif inventory.empty:
            st.info("No stock records yet. Set stock for a product.")
        for row in visible_rows(inventory, "inventory"):
            with st.container(border=True):
                info_col, edit_col, delete_col = st.columns([3.4, 1.2, 1.2])
                info_col.write(f"**{row['sku']}** · {row['quantity']} units")
                info_col.caption(f"{row['product_name']} · Safety {row['safety_stock']} units")
                if edit_col.button("Edit", key=f"inventory_edit_{row['sku']}", width="stretch"):
                    inventory_editor(row["sku"])
                if delete_col.button("Delete", key=f"inventory_delete_{row['sku']}", width="stretch"):
                    delete_dialog("inventory", sku=row["sku"])

elif page == "Sales":
    title_col, record_col, import_col = st.columns([3, 1, 1])
    title_col.subheader("Sales records")
    products = db.table("products")
    if record_col.button("Record sale", type="primary", width="stretch",
                         disabled=products.empty):
        record_sale_dialog()
    if import_col.button("Import CSV", width="stretch", disabled=products.empty):
        import_sales_dialog()
    st.caption("Use real dates in YYYY-MM-DD format. Historical CSV import does not subtract from current stock.")
    if notice := st.session_state.pop("sales_notice", None):
        st.success(notice)
    if products.empty:
        st.info("Create a product on the Inventory page first.")
    st.markdown("#### Sales history")
    dataframe(db.table("sales"))
    with st.expander("Import history"):
        dataframe(db.table("imports"))

elif page == "Forecast":
    st.subheader("Demand forecast and stock simulation")
    local_tab, demo_tab = st.tabs(["My products", "Historical Kaggle model"])
    with local_tab:
        products = db.table("products")
        inventory = db.table("inventory")
        if products.empty:
            st.info("Add a product on the Inventory page first.")
        else:
            c1, c2 = st.columns(2)
            sku = c1.selectbox("Product", products["sku"].tolist(), key="forecast_sku")
            horizon = c2.selectbox("Weeks ahead", [4, 8, 10], key="forecast_horizon")
            history = db.weekly_sales(sku)
            if len(history) < 4:
                st.warning("This product needs at least four recorded weeks of sales.")
            else:
                st.caption(f"Recorded weeks: {len(history)} · Last week: {history.index[-1].date()}")
                if len(history) >= 80 and st.button("Train or refresh model for this product"):
                    try:
                        info = fc.train_local_model(sku, history)
                        st.success(f"Selected {info['model_name']}; holdout WAPE {info['test_wape']:.1%}")
                        st.rerun()
                    except Exception as exc:
                        failure(exc)
                elif len(history) < 80:
                    st.info("Using a simple last-four-weeks baseline. Trainable model needs 80 consecutive weeks.")
                try:
                    future, info = fc.local_forecast(sku, history, horizon)
                    st.write("Forecast method:", info["model_name"])
                    if info.get("test_wape") is not None:
                        st.write(f"Last holdout WAPE: {info['test_wape']:.1%}")
                    plot = pd.DataFrame(index=history.tail(16).index.union(pd.DatetimeIndex(future["week_end"])))
                    plot["Actual units"] = history.tail(16)
                    plot["Forecast units"] = future.set_index("week_end")["forecast_units"]
                    st.line_chart(plot, x_label="Week ending", y_label="Units sold per week")
                    dataframe(future.assign(week_end=future["week_end"].dt.date))
                    st.download_button("Download forecast CSV", future.to_csv(index=False),
                                       f"forecast_{sku}.csv", "text/csv")
                    match = inventory[inventory["sku"] == sku]
                    if match.empty:
                        st.info("Set current stock on the Inventory tab to run a simulation.")
                    else:
                        product = products.set_index("sku").loc[sku]
                        stock = int(match.iloc[0]["quantity"])
                        simulated, recommendation = fc.simulate_inventory(
                            stock, future, int(product["lead_time_days"]),
                            int(product["safety_stock"]))
                        st.markdown("#### Stock without replenishment")
                        a, b, c = st.columns(3)
                        a.metric("Current stock", stock)
                        b.metric("Reorder point", recommendation["reorder_point"])
                        c.metric("Suggested order", recommendation["suggested_order"])
                        st.line_chart(simulated.set_index("week_end")["projected_stock"],
                                      x_label="Week ending", y_label="Stock remaining (units)")
                        if recommendation["risk"] == "REORDER":
                            st.warning("Reorder threshold reached. This is a planning estimate.")
                            if st.button("Record alert locally"):
                                db.log_alert(sku,
                                             f"Reorder {recommendation['suggested_order']} units; stock {stock}")
                                st.success("Alert recorded. SNS will replace this step on AWS.")
                        st.caption("Projected stock is a scenario, not an automatic stock adjustment.")
                except Exception as exc:
                    failure(exc)
    with demo_tab:
        st.info("Original Kaggle data: all products combined, 2011–2014. This forecast begins in 2015 and cannot represent a particular store or product.")
        demo_horizon = st.selectbox("Demo forecast weeks", [4, 10], key="demo_horizon")
        try:
            history, future, metrics, bundle = fc.global_demo_forecast(demo_horizon)
            st.write("Model:", bundle["model_name"])
            if demo_horizon != int(bundle.get("horizon_weeks", 4)):
                st.warning("This horizon is exploratory; saved test metrics were measured at a different horizon.")
            recent = history.tail(12).copy()
            recent["week_start"] = pd.to_datetime(recent["week_start"])
            chart = pd.DataFrame(index=pd.DatetimeIndex(recent["week_start"]).union(pd.to_datetime(future["week_start"])))
            chart["Historical units"] = pd.Series(recent["actual"].values, index=recent["week_start"])
            chart["Forecast units"] = pd.Series(future["forecast_units"].values,
                                                 index=pd.to_datetime(future["week_start"]))
            st.line_chart(chart, x_label="Week starting", y_label="Units sold per week")
            dataframe(future)
            st.download_button("Download historical demo forecast", future.to_csv(index=False),
                               f"historical_demo_{demo_horizon}_weeks.csv", "text/csv")
            st.markdown("#### Saved model test metrics")
            if metrics.empty:
                st.info("No test metrics were uploaded with this model.")
            else:
                dataframe(metrics)
        except Exception as exc:
            failure(exc)

elif page == "Accuracy":
    st.subheader("Forecast accuracy")
    st.caption("Check past predictions against recorded sales. This page runs a fresh backtest and does not change the saved product model.")
    products = db.table("products")
    if products.empty:
        st.info("Add a product on the Inventory page first.")
    else:
        sku = st.selectbox("Product", products["sku"].tolist(), key="accuracy_sku")
        sales_sources = db.table("sales")
        if ((sales_sources["sku"] == sku) &
                (sales_sources["source"] == "synthetic_demo")).any():
            st.warning("This product includes synthetic demo sales. Its score does not measure real-store accuracy.")
        history = db.weekly_sales(sku)
        if len(history) < 8:
            st.info(f"{sku} has {len(history)} recorded weeks. Add sales covering at least eight weeks to test accuracy.")
        else:
            try:
                metrics, predictions, selected, detail = fc.evaluate_product_accuracy(history)
                first = predictions["week_end"].iloc[0].date()
                last = predictions["week_end"].iloc[-1].date()
                st.write(f"**Test period:** {first} to {last} · {len(predictions)} weeks")
                st.info(detail)
                st.caption("The backtest forecasts up to four weeks ahead, then uses newly observed sales before forecasting the next block.")
                st.write(f"**Selected by validation:** {selected}" if len(history) >= 80
                         else "**Method:** Last4 baseline")
                chosen = metrics.set_index("Model").loc[selected]
                c1, c2, c3 = st.columns(3)
                c1.metric("MAE (units/week)", f"{chosen['MAE']:.2f}")
                c2.metric("RMSE (units/week)", f"{chosen['RMSE']:.2f}")
                c3.metric("WAPE", f"{chosen['WAPE (%)']:.1f}%"
                          if pd.notna(chosen["WAPE (%)"]) else "N/A")
                st.caption("Lower errors are better. WAPE is unavailable when actual sales total zero during the test.")
                chart = predictions.set_index("week_end")[["actual_units", "forecast_units"]]
                chart.columns = ["Actual units", "Forecast units"]
                st.line_chart(chart, x_label="Week ending", y_label="Units sold per week")
                st.markdown("#### Test metrics")
                dataframe(metrics.round(2))
                st.markdown("#### Weekly comparison")
                dataframe(predictions.assign(week_end=predictions["week_end"].dt.date).round(2))
                st.download_button("Download accuracy results CSV",
                                   predictions.to_csv(index=False),
                                   f"accuracy_{sku}.csv", "text/csv")
            except Exception as exc:
                failure(exc)

elif page == "Alerts":
    st.subheader("Alerts")
    st.caption("Local alert log. Amazon SNS email delivery is planned for the AWS version.")
    dataframe(db.table("alerts"))

else:
    st.subheader("Model settings")
    st.caption("Upload a compatible saved model, check it, then activate it. Only use model files you trust.")
    if not st.session_state.get("model_admin_unlocked", False):
        expected_key = mm.admin_key()
        st.info("Enter the local model admin key to manage models. Find it in data/model_settings/admin_key, or use MODEL_ADMIN_PASSWORD if configured.")
        entered_key = st.text_input("Model admin key", type="password")
        if st.button("Unlock model settings", type="primary"):
            if hmac.compare_digest(entered_key, expected_key):
                st.session_state.model_admin_unlocked = True
                st.rerun()
            else:
                st.error("Incorrect model admin key")
        st.stop()

    if notice := st.session_state.pop("model_notice", None):
        st.success(notice)
    if st.button("Lock settings"):
        st.session_state.model_admin_unlocked = False
        st.session_state.pop("model_validation", None)
        st.rerun()

    target = st.selectbox("Model type", ["Product forecast", "Historical Kaggle forecast"])
    if target == "Product forecast":
        products = db.table("products")
        if products.empty:
            st.info("Add a product before uploading a product model.")
            st.stop()
        sku = st.selectbox("Product", products["sku"].tolist(), key="model_target_sku")
        history = db.weekly_sales(sku)
        state = mm.product_state(sku)
        current_path = fc.active_local_model_path(sku)
        st.write("Active model:", "Trained in this app" if state["active"] == "trained" and current_path.exists()
                 else "Last4 baseline" if not current_path.exists() else f"Uploaded version {state['active'][:8]}")
        st.caption(f"Latest sales week: {history.index[-1].date()}" if not history.empty
                   else "No sales history yet")
        uploaded = st.file_uploader("Product model file (.joblib)", type="joblib", key="product_model_upload")
        model_bytes = uploaded.getvalue() if uploaded else None
        signature = mm.fingerprint("product", sku, model_bytes)
        if st.button("Validate product model", disabled=model_bytes is None):
            try:
                info = mm.validate_product(model_bytes, sku, history)
                st.session_state.model_validation = {"signature": signature, "info": info}
            except Exception as exc:
                st.session_state.pop("model_validation", None)
                failure(exc)
        validation = st.session_state.get("model_validation", {})
        if validation.get("signature") == signature and model_bytes is not None:
            st.success(f"Validated {validation['info']['model_name']} for {sku}, trained through {validation['info']['trained_through']}.")
            if st.button("Activate product model", type="primary"):
                try:
                    mm.activate_product(model_bytes, sku, history)
                    st.session_state.pop("model_validation", None)
                    st.session_state.model_notice = f"Activated uploaded model for {sku}"
                    st.rerun()
                except Exception as exc:
                    failure(exc)
        if state.get("previous") is not None and st.button("Restore previous product model"):
            try:
                mm.restore_product(sku)
                st.session_state.model_notice = f"Restored previous model for {sku}"
                st.rerun()
            except Exception as exc:
                failure(exc)
    else:
        state = mm.global_state()
        st.write("Active model:", "Bundled Kaggle model" if state["active"] == "bundled"
                 else f"Uploaded version {state['active'][:8]}")
        st.caption("Upload matching weekly_history.csv if the model was trained on a different history. Test metrics CSV is optional.")
        uploaded = st.file_uploader("Kaggle model file (.joblib)", type="joblib", key="global_model_upload")
        history_upload = st.file_uploader("Matching weekly history CSV (optional)", type="csv", key="global_history_upload")
        metrics_upload = st.file_uploader("Test metrics CSV (optional)", type="csv", key="global_metrics_upload")
        model_bytes = uploaded.getvalue() if uploaded else None
        history_bytes = history_upload.getvalue() if history_upload else None
        metrics_bytes = metrics_upload.getvalue() if metrics_upload else None
        signature = mm.fingerprint("global", model_bytes, history_bytes, metrics_bytes)
        if st.button("Validate Kaggle model", disabled=model_bytes is None):
            try:
                info = mm.validate_global(model_bytes, history_bytes, metrics_bytes)
                st.session_state.model_validation = {"signature": signature, "info": info}
            except Exception as exc:
                st.session_state.pop("model_validation", None)
                failure(exc)
        validation = st.session_state.get("model_validation", {})
        if validation.get("signature") == signature and model_bytes is not None:
            st.success(f"Validated {validation['info']['model_name']} with {validation['info']['history_rows']} history weeks through {validation['info']['last_label']}.")
            if st.button("Activate Kaggle model", type="primary"):
                try:
                    mm.activate_global(model_bytes, history_bytes, metrics_bytes)
                    st.session_state.pop("model_validation", None)
                    st.session_state.model_notice = "Activated uploaded Kaggle model"
                    st.rerun()
                except Exception as exc:
                    failure(exc)
        if state.get("previous") is not None and st.button("Restore previous Kaggle model"):
            try:
                mm.restore_global()
                st.session_state.model_notice = "Restored previous Kaggle model"
                st.rerun()
            except Exception as exc:
                failure(exc)

