# NovaSeat Churn Prediction — Google Colab

This folder contains the churn prediction ML pipeline, ready to run on Google Colab.

## Files

| File | Description |
|------|-------------|
| `train_colab.ipynb` | **Training notebook** — loads the Telco dataset, engineers features, trains an XGBoost classifier, evaluates it, computes SHAP explainability, and saves model artifacts. |
| `score_colab.ipynb` | **Scoring notebook** — loads trained model artifacts, scores accounts from a CSV, assigns risk tiers (Low/Medium/High/Critical), computes per-account churn drivers, and exports results. |
| `train.py` | Reference Python script for the training pipeline (CLI version). |
| `score.py` | Reference Python script for the scoring pipeline (CLI version). |
| `WA_Fn-UseC_-Telco-Customer-Churn.csv` | IBM Telco Customer Churn dataset used for training. |
| `requirements.txt` | Python dependencies shared by all files. |

## How to deploy to Google Colab

Both notebooks include a **sync cell** that automatically clones (or pulls) this repo from GitHub into the Colab runtime. No manual file upload is needed.

1. Go to [Google Colab](https://colab.research.google.com).
2. Click **File > Upload notebook** and select `train_colab.ipynb` (or open it from GitHub directly).
3. Repeat for `score_colab.ipynb`.
4. Run the notebooks — the sync cell will clone the repo and make all files available at `/content/novaseat-v2/`.

> **Note:** The repo uses an SSH remote. If SSH keys are not configured in your Colab environment, change `REPO_URL` in the sync cell to the HTTPS URL:
> ```
> REPO_URL = "https://github.com/paoloneh/novaseat-v2.git"
> ```

## How to use

### Step 1: Train the model

1. Open `train_colab.ipynb` in Colab.
2. Review the **Configuration** cell and adjust if needed:
   - `USE_DRIVE` — set to `True` to save artifacts to Google Drive (recommended), or `False` to download them as a zip.
   - `SKIP_SHAP` — set to `True` to skip SHAP computation (faster, but no per-account churn drivers).
3. Run all cells (`Runtime > Run all`).
4. The sync cell clones the repo automatically — the dataset CSV is loaded from the cloned repo (`/content/novaseat-v2/colab/WA_Fn-UseC_-Telco-Customer-Churn.csv`).
5. The notebook will:
   - Map the Telco dataset to the NovaSeat DB schema
   - Engineer 19 features (numeric, binary, one-hot encoded)
   - Train an XGBoost classifier with 5-fold stratified cross-validation
   - Print evaluation metrics (AUC-ROC, accuracy, precision, recall, F1)
   - Save artifacts to Google Drive (or download as zip)
   - Export `accounts_seed.csv` with scored accounts for DB seeding

**Artifacts produced:**

| File | Description |
|------|-------------|
| `model.joblib` | Trained XGBoost classifier |
| `scaler.joblib` | Fitted StandardScaler |
| `feature_columns.json` | Ordered list of 19 feature names |
| `training_report.json` | Full metrics, feature importance, metadata |
| `accounts_seed.csv` | All accounts scored with churn probability, risk tier, and SHAP drivers |

### Step 2: Score new accounts

1. Open `score_colab.ipynb` in Colab.
2. Review the **Configuration** cell:
   - `USE_DRIVE` — must match what you used during training (so it can find the artifacts).
   - `COMPUTE_SHAP` — set to `False` to skip SHAP driver computation (faster).
   - `DRY_RUN` — set to `True` to print results without saving.
3. Run all cells.
4. When prompted, upload a CSV of accounts to score. This can be:
   - The `accounts_seed.csv` from training
   - A fresh export from your database
   - Any CSV with the expected columns (see below)
5. The notebook will:
   - Load the trained model from Google Drive (or from an uploaded zip)
   - Prepare features and score all accounts
   - Show the risk tier distribution and high-risk accounts
   - Export `scored_accounts.csv` to Drive or trigger a download

**Required columns in the input CSV:**

`events_per_month_trend`, `has_dedicated_csm`, `plan_type`, `tenure_months`, `monthly_charges`, `annual_revenue`, `days_since_last_login`, `support_ticket_velocity`

Optional columns (default to 0 if missing): `platform_tier`, `payment_auto`, `senior_citizen`, `has_partner`, `has_dependents`, `paperless_billing`, `online_security`, `online_backup`, `streaming_tv`

## Google Drive vs. local mode

Both notebooks support two modes controlled by the `USE_DRIVE` flag:

| Mode | `USE_DRIVE` | How artifacts are shared |
|------|-------------|--------------------------|
| **Google Drive** (default) | `True` | Artifacts are saved to / loaded from `MyDrive/novaseat-model/artifacts`. Both notebooks access the same Drive folder. |
| **Local / download** | `False` | Training notebook downloads a `novaseat_artifacts.zip`. Scoring notebook prompts you to upload that zip. |

Google Drive mode is recommended because artifacts persist between sessions and flow seamlessly from training to scoring.

## Google Cloud Project Setup (for automated scoring via n8n)

To run the scoring notebook automatically via Workflow 1 (Nightly Model Trigger), you need a Google Cloud project configured with Colab Enterprise. Follow these steps from the [Google Cloud Console](https://console.cloud.google.com).

### 1. Create or select a GCP project

- Click the **project dropdown** (top-left bar) → **New Project**
- Name: e.g. `NovaSeat Churn Prevention`
- Note the **Project ID** (e.g. `novaseat-churn`) — you will need it later
- Click **Create**, then select the project from the dropdown

### 2. Enable required APIs

Go to **APIs & Services → Library** and enable each of these:

| API | Why |
|-----|-----|
| **Notebooks API** | Colab Enterprise notebook execution |
| **Vertex AI API** | Runtime templates for notebook compute |
| **Cloud Storage API** | Store notebooks and execution outputs (usually enabled by default) |

### 3. Create a Cloud Storage bucket

- Go to **Cloud Storage → Buckets → Create**
- **Name:** e.g. `novaseat-churn-colab`
- **Location type:** Region → `us-central1`
- **Storage class:** Standard
- **Access control:** Uniform
- Click **Create**

Then upload the scoring notebook:
- Open the bucket → **Create folder** → name it `colab`
- Enter the `colab/` folder → **Upload files** → select `score_colab.ipynb` from this repo
- Optionally create a `colab/outputs/` folder for execution results

Your notebook URI will be: `gs://<bucket-name>/colab/score_colab.ipynb`

### 4. Create a Service Account

- Go to **IAM & Admin → Service Accounts → Create Service Account**
- **Name:** `NovaSeat Colab Runner`
- **ID:** `novaseat-colab-runner`
- Click **Create and Continue**
- Add these roles (click **+ Add Another Role** between each):

| Role | Purpose |
|------|---------|
| Notebooks Runner | Trigger and monitor notebook execution jobs |
| Storage Object Viewer | Read the notebook from GCS |
| Storage Object Creator | Write execution outputs to GCS |
| Vertex AI User | Access runtime templates |

- Click **Done**

### 5. Generate a JSON key (for n8n)

- In **IAM & Admin → Service Accounts**, click on `novaseat-colab-runner`
- Go to the **Keys** tab → **Add Key → Create new key**
- Format: **JSON** → **Create**
- A `.json` file downloads — keep it safe, you will paste its contents into n8n

### 6. Create a Colab Enterprise Runtime Template

> **Note:** Runtime Templates are under **Colab Enterprise**, not the classic Workbench section.

- Go to **Vertex AI → Colab Enterprise → Runtime Templates** (left sidebar, under "Notebooks")
  - If you don't see it, search **"Colab Enterprise"** in the console search bar
  - Make sure billing is enabled — Colab Enterprise is a paid Vertex AI feature
- Click **Create Runtime Template**
- **Name:** `novaseat-scoring-runtime`
- **Region:** `us-central1`
- **Machine type:** `e2-standard-4` (4 vCPU, 16 GB — sufficient for XGBoost + SHAP)
- Leave other settings as defaults → **Create**
- Copy the **Runtime Template ID** from the list

### 7. Configure n8n

#### Add the Google credential

- In n8n → **Settings → Credentials → Add Credential**
- Type: **Google Service Account**
- Name: `NovaSeat Google Service Account`
- Paste the full contents of the JSON key from step 5
- **Save**

#### Set environment variables

The Workflow 1 Code node reads these variables via `$env.VARIABLE_NAME`. Pass them to the n8n process so they are available at runtime.

| Variable | Value |
|----------|-------|
| `GOOGLE_CLOUD_PROJECT_ID` | your GCP project ID (e.g. `novaseat-churn`) |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` |
| `GOOGLE_CLOUD_NOTEBOOK_RUNTIME_TEMPLATE` | runtime template ID from step 6 |
| `COLAB_NOTEBOOK_GCS_URI` | `gs://<bucket>/colab/score_colab.ipynb` |
| `COLAB_OUTPUT_GCS_PREFIX` | `gs://<bucket>/colab/outputs` |
| `DATA_TEAM_EMAIL` | your team's notification email |
| `ALERT_FROM_EMAIL` | sender email for failure alerts |

**How to pass them depends on how you run n8n:**

- **Docker (docker-compose):** add an `env_file: .env` or list them under `environment:` in the n8n service
- **Docker (standalone):** `docker run --env-file .env ...`
- **Directly (npx / global install):** export them in your shell before starting n8n, or use a tool like `dotenv`:
  ```bash
  export $(grep -v '^#' .env | xargs) && npx n8n start
  ```

Restart n8n after changing environment variables.

### 8. Test end-to-end

1. Run `train_colab.ipynb` in Google Colab → artifacts saved to Google Drive
2. Run `score_colab.ipynb` manually → verify scoring works
3. Sync the workflow: `python scripts/sync_n8n_workflows.py`
4. In n8n, open **WF1 — Nightly Model Trigger** → click **Test Workflow** (Manual Trigger) → verify it triggers the Colab API, polls for completion, validates scores in the DB, and sends failure emails on errors

## Model details

- **Algorithm:** XGBoost (`XGBClassifier`)
- **Features:** 19 features (6 numeric, 9 binary, 4 one-hot encoded)
- **Class imbalance:** handled via `scale_pos_weight`
- **Validation:** 80/20 stratified split + 5-fold stratified CV
- **Explainability:** SHAP TreeExplainer (top-3 churn drivers per account)
- **Risk tiers:** Low (0-30%), Medium (30-50%), High (50-70%), Critical (70-100%)
