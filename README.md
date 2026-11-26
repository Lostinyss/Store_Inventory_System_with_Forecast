# Store Forecast Studio

A single-store inventory and demand forecasting demo. It uses Streamlit, SQLite, and local model files. The dashboard opens directly and creates its SQLite database on first run.

## Run locally

Use Python 3.13. From this folder in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Open http://localhost:8501.

## Deploy on Streamlit Community Cloud

Select your GitHub repository, branch `main`, and entry point `app.py`. Select Python 3.13 under Advanced settings. The app starts with no application secrets.

Use sample data on Community Cloud. The host may clear local files, including the SQLite database and trained product models. Visitors who can open the app share the same data and can edit it. Choose the app visibility in Community Cloud accordingly.

## Try the demo

1. In **Inventory**, add a product with Product ID `DEMO-001` and set its stock in the **Stock** tab.
2. In **Sales**, import `sample_data/demo_product_sales_104_weeks.csv` to load synthetic history. A sales CSV needs `sale_date,product_id,quantity_sold`, dates in YYYY-MM-DD format, and positive whole quantities. Existing CSVs with `sku` in place of `product_id` are also accepted. Create each product before importing. Importing the same file again does not duplicate sales. Historical imports do not change current stock.
3. In **Forecast → My products**, open the product forecast and stock simulation. The baseline uses the last four weeks. With at least 80 consecutive weeks, **Train or refresh model** compares Ridge with the baseline and saves the selected model locally.
4. Record a sale in **Sales** and choose whether to deduct the quantity from stock. Check **Alerts** for reorder notices when stock reaches the threshold. Alerts are displayed in the app.

The **Historical Kaggle model** tab shows a separate demonstration based on combined historical Global Superstore sales. Product forecasts use the selected product's sales in SQLite. Missing weeks between a product's first and last sale are treated as zero sales. Synthetic data is intended to demonstrate app behavior.

Local records are stored in `data/store.db`. Upload copies and product models are also stored under `data/`, which is excluded from Git.
