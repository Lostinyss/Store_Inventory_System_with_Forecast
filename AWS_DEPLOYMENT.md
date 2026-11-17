# AWS deployment checklist

The application runs on EC2, with RDS PostgreSQL, a private S3 bucket, an S3-triggered Lambda importer, and an SNS email topic. These instructions configure the integration; the code does not create AWS resources. Use one Region for all services. **Complete the login configuration in section 6 before starting the app or running the checks below.**

## 1. Prepare RDS and the EC2 app

After configuring login, start the app and sign in with a Cognito account with `DB_HOST`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` configured. It creates the `import_jobs` and `alert_state` tables and updates the existing `alerts` table. Confirm the sidebar says **RDS PostgreSQL**. Existing SQLite data is not migrated. Do not commit credentials to Git.

Every website page requires OIDC login. Any account in the configured Cognito user pool can access the website, so keep self-registration disabled and manage accounts in Cognito. Configure the login and HTTPS steps in section 6 before opening the website publicly. Streamlit must be restarted after changing environment variables or secrets.

## 2. Create S3 storage

Create a **private** S3 bucket with Block Public Access enabled. Use its bucket name in `AWS_S3_BUCKET`. The application writes `sales-imports/<sha256>.csv` and `app-data/...` model files. It restores model files from S3 when the app process starts. Uploaded sales CSVs do not subtract from current inventory.

Attach an EC2 IAM role with permissions to `s3:ListBucket` on the bucket; `s3:GetObject` and `s3:PutObject` on `sales-imports/*` and `app-data/*`. Do not put access keys in environment variables or Git. Set `AWS_DEFAULT_REGION` to the bucket's Region.

## 3. Create the SNS email topic

Create a **Standard** SNS topic and subscribe your email address. Click the confirmation link in the email. Add `sns:Publish` on that topic ARN to the EC2 IAM role. Set `SNS_TOPIC_ARN` on EC2. A sale that subtracts stock, a stock edit, or a forecast page view checks the threshold. The app records one alert when stock goes from above to at/below the reorder point; it can alert again after stock rises above and later falls below. Alert delivery status appears on the Alerts page. The app retries pending messages when a page runs, but a crash after SNS accepts a message can cause a duplicate email.

## 4. Deploy the CSV Lambda function

Create a Python **3.13** Lambda function with handler `handler.lambda_handler`. On a Linux host with the same architecture as the Lambda function (for example, your x86_64 EC2 instance), run `bash aws_lambda/build.sh`. Upload the resulting `aws_lambda/sales-import-lambda.zip` to Lambda. The script uses uv to install Linux packages and places `handler.py` and all dependencies at the ZIP root. Set timeout and memory for your largest expected CSV; the application limits uploads to 20 MB and the handler to 200,000 rows.

Lambda environment variables:

| Name | Value |
|---|---|
| `AWS_S3_BUCKET` | The private bucket name |
| `DB_SECRET_ARN` | Secrets Manager ARN holding the database username, password, host, and port |
| `DB_NAME` | `store_inv` |
| `DB_HOST` | RDS endpoint (optional if the secret has `host`) |
| `DB_USER` | Database user (optional if the secret has `username`) |

Create or select a Secrets Manager secret for the RDS user. The Lambda execution role needs CloudWatch Logs permissions, `s3:GetObject` for `sales-imports/*`, and `secretsmanager:GetSecretValue` for that secret (plus `kms:Decrypt` if using a customer-managed KMS key). Attach Lambda to private subnets in the **same VPC** as RDS; let its security group reach RDS TCP 5432. A VPC-attached Lambda also needs access to S3 and Secrets Manager APIs through NAT or appropriate VPC endpoints (S3 Gateway and Secrets Manager Interface endpoints are typical).

On the S3 bucket, create an **ObjectCreated** event notification filtered to prefix `sales-imports/` and suffix `.csv`, targeting the Lambda. Do not trigger on `app-data/`; otherwise model uploads would invoke the importer. Give S3 permission to invoke the function (the console adds this when you set the trigger).

The Lambda validates date, SKU, quantity, file size, and existing products. It imports all rows in one database transaction and records `completed` or `failed` in `import_jobs`. Re-uploading identical bytes does not duplicate sales. Errors appear under **Sales → Import history** after page refresh. The Lambda does not change stock, because imported sales are historical.

## 5. Update EC2 and check the flow

Pull the new code into the existing repository, install the updated `requirements.txt`, configure these additional variables, and restart Streamlit:

```bash
export AWS_DEFAULT_REGION=us-east-1
export AWS_S3_BUCKET=YOUR_BUCKET_NAME
export SNS_TOPIC_ARN=YOUR_TOPIC_ARN
~/.local/bin/uv pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Keep your existing `DB_*` variables in the **same shell** used to start Streamlit. On EC2 reboot, configure a service with private environment settings; shell `export` values are temporary. Do not place the database password or model admin password in Git.

Check in this order:

1. Open the app and confirm **RDS PostgreSQL · S3 models and CSV imports** in the sidebar.
2. Create a product with SKU `DEMO-001`; upload the sample CSV from `sample_data/`. Check that the S3 object appears and the import job changes to `completed`, then check Sales history. Repeat upload: it should not duplicate sales.
3. Set stock below the reorder point, open Forecast, and check that one alert appears in Alerts with `sent` email status. Confirm the SNS email. Raise stock above the point, then lower it to test a new crossing.
4. Train a model or activate an uploaded version, then verify its objects under `app-data/` in S3. Restart the app and confirm the same model remains active.

The app does not yet implement HTTPS, scheduled checks while nobody uses the app, or guaranteed exactly-once SNS delivery. HTTPS must be configured before public access.

## 6. Deploy website login with HTTPS on EC2

1. Create an OIDC identity provider. For AWS, use a Cognito user pool with an AWS-hosted login domain and an app client that has a client secret and supports the authorization code flow. Add your intended user accounts. Register the exact callback `https://YOUR_DOMAIN/oauth2callback` and add `https://YOUR_DOMAIN/` as an **Allowed sign-out URL**. For SSH-tunnel testing, use `http://localhost:8501/oauth2callback` and `http://localhost:8501/` respectively. Cognito's discovery URL is `https://cognito-idp.REGION.amazonaws.com/USER_POOL_ID/.well-known/openid-configuration` (the user pool endpoint, not the hosted login domain). `cognito_logout.py` adapts Streamlit's logout URL to Cognito's required `logout_uri` parameter; it uses a Streamlit internal route, so check compatibility when upgrading Streamlit. [Cognito discovery endpoints](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-assign-domain.html) [Cognito logout URLs](https://docs.aws.amazon.com/cognito/latest/developerguide/logout-endpoint.html)
2. Create or use a domain name for the website. Request an ACM certificate for that name in the same Region as an Application Load Balancer (ALB). Create an HTTPS listener on port 443 with the ACM certificate, forwarding to a target group that reaches EC2 on port 8501. Redirect HTTP port 80 to HTTPS. [ALB HTTPS certificates](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/https-listener-certificates.html)
3. Allow public 443 traffic to the ALB. Restrict EC2 inbound port 8501 to the **ALB security group only**; never expose the direct Streamlit port publicly. Keep RDS private and reachable only from the EC2/Lambda security groups. [AWS network guidance](https://docs.aws.amazon.com/elasticloadbalancing/latest/userguide/infrastructure-security.html)
4. On EC2, copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` in the app directory. Set the HTTPS `redirect_uri`, a fresh random `cookie_secret`, Cognito client ID, Cognito client secret, and Cognito discovery URL. Keep this file out of Git and readable only by the app's OS user. `streamlit[auth]` in `requirements.txt` installs the required Authlib package. [Streamlit OIDC settings](https://docs.streamlit.io/develop/api-reference/user/st.login)
5. Disable Cognito self-registration and create only the user accounts you want to access the website. The app does not require `STORE_ALLOWED_SUBJECTS` or `STORE_ALLOWED_EMAILS`. Keep `MODEL_ADMIN_PASSWORD` private if you want separate model administration.
6. Install the updated requirements and restart the Streamlit service. Test an anonymous browser, a Cognito account, sign out, and a Model Settings action. Check that `https://YOUR_DOMAIN` works and direct public access to port 8501 does not.

Website sign-in applies to Streamlit pages and actions. The S3-triggered Lambda uses its IAM role and private RDS connection; restrict direct uploads to `sales-imports/` through IAM so a person cannot bypass the website's CSV validation flow.
