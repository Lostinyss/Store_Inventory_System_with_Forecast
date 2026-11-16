# Store Forecast Studio

This Streamlit app uses SQLite locally and can use Amazon RDS for PostgreSQL when database environment variables are configured. Models and uploaded CSV files are still stored locally. S3, Lambda, and SNS are not connected yet.

## Run locally

Use Python 3.13. In PowerShell, from this folder:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Open `http://localhost:8501`. Add your products and stock on the Inventory page, then record or import sales.

## Use RDS for PostgreSQL

Install the requirements above, then set these environment variables on the server before starting Streamlit:

| Variable | Value |
|---|---|
| `DB_HOST` | RDS instance endpoint (hostname only) |
| `DB_PORT` | `5432` (optional; this is the default) |
| `DB_NAME` | `store_inv` |
| `DB_USER` | `store_admin` |
| `DB_PASSWORD` | RDS database password |
| `DB_SSLMODE` | `require` (optional; this is the default) |

When `DB_HOST` is set, the app connects to PostgreSQL and creates its tables if absent. Keep the password in server configuration, never in GitHub or a command committed to the repository. Existing SQLite records are not copied automatically; move real data with a separate, reviewed migration. The RDS security group must allow PostgreSQL traffic from the EC2 security group. The app needs a writable `data/` directory for model versions, CSV uploads, and its local model admin key unless `MODEL_ADMIN_PASSWORD` is configured.

## What works

- Navigate with sidebar buttons; the current page is highlighted. The **Inventory** page has Products and Stock tabs, with Edit and Delete buttons on each row. Editing opens a modal, and deletion asks for confirmation. A product with stock or sales cannot be deleted; deleting a stock row keeps its sales history.
- Set current stock by product for one store.
- The **Sales** page shows sales history directly. Use **Record sale** or **Import CSV** to open their forms in a modal. Import history is below the sales table.
- Add a sale manually, optionally subtracting it from current stock.
- Import a CSV with `sale_date,sku,quantity_sold`. Create the SKU first. Importing the same file twice does not duplicate sales. Historical imports do not alter current stock. Old CSVs containing `branch_id` need that column removed.
- Forecast a product. With fewer than 80 weeks, use a last-four-weeks baseline. With 80 or more consecutive weeks, click **Train or refresh model**. The training action compares Ridge to that baseline on the latest 12 weeks and keeps the better method.
- Use the **Accuracy** page to backtest a product against past sales. It needs eight recorded weeks for a baseline check. With 80 or more weeks, it chooses Ridge or Last4 on a 12-week validation period and scores both methods on a separate 12-week test period. It shows MAE, RMSE, WAPE, a chart, and downloadable weekly comparisons. This check does not modify the saved forecasting model.
- Simulate remaining stock without replenishment and log an alert locally.
- View the Kaggle model as a **separate historical demo**. The bundled model forecasts all-product demand after 2014, not the live product data. Its saved test metrics apply to the original four-week horizon. If you activate an uploaded Kaggle model without its own metrics CSV, the old scores are hidden.
- The **Settings** page accepts compatible `.joblib` product or Kaggle model bundles. Unlock it with the key stored in `data/model_settings/admin_key` (or set `MODEL_ADMIN_PASSWORD` before starting the server), upload the model, validate it, then activate it. The previous version can be restored. Product models must match the selected SKU and latest sales week. Kaggle models can include a matching `weekly_history.csv` and optional `test_metrics.csv`; without new metrics, the historical page does not show old model scores. Only upload model files you created or trust, because loading `.joblib` executes Python pickle data.

Existing branch records are consolidated automatically on first start: stock is summed by SKU, sales and alerts are retained, and the old branch names are removed from the active database. A copy of the original database is saved at `data/store_before_single_store.db`. Previously trained branch-specific model files remain on disk but must be trained again for the combined store history.

## Forecasting data requirements

The original `Global Superstore.csv` has no usable order date (`Order Date` is `00:00.0` throughout) and no Store ID. Its 2011–2014 model cannot provide valid per-product or 2026 forecasts. The product forecast in this app learns only from sales entered into the local database. The sample data is synthetic; metrics on it do not establish real-world accuracy.

The importer expects real dates in `YYYY-MM-DD` and positive whole-number quantities. A product with missing weeks is interpreted as zero sales for those weeks. Maintain a complete sales log for meaningful forecasts.

## Planned AWS mapping

| Local component | Planned AWS service |
|---|---|
| Streamlit and forecast training | EC2 |
| SQLite tables | RDS for PostgreSQL (supported when configured) |
| Local CSV/model/report files | S3 |
| CSV validation/import function | Lambda |
| Local alert log | SNS email topic |

Before publishing, add access control and HTTPS, attach IAM roles, and move model/upload file persistence to S3. The current app does not yet provide these production features.

