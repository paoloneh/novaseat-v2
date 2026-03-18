# NovaSeat — n8n Workflows

This folder contains the n8n workflow JSON files for the NovaSeat Churn Prevention Platform. Each file can be imported directly into n8n via **Settings → Import Workflow**.

---

## Workflow 2 — Churn Alert Monitor

**File:** `workflow2-churn-alert-monitor.json`

**Trigger:** Cron daily at 08:00 + manual trigger

**Flow:**

1. Query `v_churn_alerts` for accounts newly flagged as High/Critical
2. Code node applies the decision logic based on `risk_tier` + ARR:
   - **Critical + ARR > €50K** → `csm_escalation` — creates a Case, notifies the CSM via email, sends personalized outreach to the customer
   - **Critical + ARR ≤ €50K** → `auto_outreach` — sends autonomous email with success call offer
   - **High** → `product_tip` — sends personalized email with a feature recommendation based on the top SHAP churn driver
3. For each account:
   - INSERT into `interventions`
   - INSERT into `outreach_messages`
   - INSERT into `tasks` (assigned to the account's CSM)
   - UPDATE `accounts.intervention_status` → `Active`
   - (CSM escalation only) INSERT into `cases` + email notification to CSM

**Email templates** are built dynamically in the Code node, with personalized content based on the account name, top churn driver, and strategy. Product tips are mapped from SHAP driver names to human-readable feature suggestions.

---

## Workflow 4 — Weekly Reporting

**File:** `workflow4-weekly-reporting.json`

**Trigger:** Cron every Monday at 08:00 + manual trigger

**Flow:**

1. Fetch last week's stats from `v_weekly_stats`
2. Fetch current risk tier distribution from `accounts`
3. Merge and build an HTML digest with:
   - KPI cards: accounts intercepted, conversations started, churn prevented, CSM escalations
   - Messaging stats: messages sent, replies received, reply rate
   - Risk distribution table: Critical / High / Medium / Low breakdown
4. UPSERT into `weekly_reports` (safe to re-run for the same week)
5. Send the digest email to the Head of Customer Success

---

## Setup after import

Both workflows require credentials to exist in n8n:

| Credential | Where | Notes |
|---|---|---|
| **NovaSeat PostgreSQL** | All Postgres nodes | Configure with your database connection details |
| **NovaSeat SMTP** | All Email Send nodes | Configure with your mail provider (Gmail, SendGrid, Resend, etc.) |

Important: the workflow JSON files in this folder do not store credentials directly.
Credentials are injected by the sync script at upload time.

---

## Credential Injection via sync_n8n_workflows.py

The script `scripts/sync_n8n_workflows.py` reads `.env` (or process environment variables), then automatically assigns credential references to nodes based on node type:

- `n8n-nodes-base.postgres` nodes get `postgres` credentials
- `n8n-nodes-base.emailSend` nodes get `smtp` credentials

This keeps workflow JSON files portable and secrets-free in git.

### Required environment variables

For API access:

- `N8N_BASE_URL` (default: `http://localhost:5678`)
- `N8N_API_KEY`

For credential injection:

- `N8N_POSTGRES_CREDENTIAL_ID` (optional but recommended)
- `N8N_SMTP_CREDENTIAL_ID` (optional but recommended)

Optional display names:

- `N8N_POSTGRES_CREDENTIAL_NAME` (default: `NovaSeat PostgreSQL`)
- `N8N_SMTP_CREDENTIAL_NAME` (default: `NovaSeat SMTP`)

If credential IDs are not provided, the script tries to resolve them from n8n using the credential names above.
Sync fails only if it cannot resolve an ID for a node type that exists in the workflows.

### Example .env

```env
N8N_BASE_URL=http://localhost:5678
N8N_API_KEY=YOUR_N8N_API_KEY

N8N_POSTGRES_CREDENTIAL_ID=YOUR_POSTGRES_CRED_ID
N8N_POSTGRES_CREDENTIAL_NAME=NovaSeat PostgreSQL

N8N_SMTP_CREDENTIAL_ID=YOUR_SMTP_CRED_ID
N8N_SMTP_CREDENTIAL_NAME=NovaSeat SMTP
```

### How to find credential IDs in n8n

Open the credential in n8n and copy its ID from the URL.
For example, a URL ending with `/credentials/12` means the credential ID is `12`.

### Placeholders to replace

| Placeholder | File | Description |
|---|---|---|
| `{{CSM_NAME}}` | WF2 | Replaced dynamically if CSM data is in DB; fallback needs manual config |
| `{{CALENDAR_LINK}}` | WF2 | URL to the CSM's booking page (Cal.com, Calendly, Google Calendar) |
| `{{HEAD_OF_CS_EMAIL}}` | WF4 | Email address of the Head of Customer Success |

---

## Automatic Sync to Local n8n

If you are running n8n locally, you can upsert all workflow JSON files in this folder into your instance automatically.

### 1) Generate an API key in n8n

- Open n8n
- Go to **Settings -> n8n API**
- Create an API key

### 2) Configure environment variables

Preferred: create a `.env` file in the repository root (the script loads it automatically).

Alternative: export variables in your shell.

```bash
export N8N_BASE_URL="http://localhost:5678"
export N8N_API_KEY="YOUR_API_KEY"
export N8N_POSTGRES_CREDENTIAL_ID="YOUR_POSTGRES_CRED_ID"
export N8N_SMTP_CREDENTIAL_ID="YOUR_SMTP_CRED_ID"
```

### 3) Run the sync script

From the repository root:

```bash
python3 scripts/sync_n8n_workflows.py
```

What it does:
- Creates workflows that do not exist yet (matched by workflow `name`)
- Updates workflows that already exist (same `name`)
- Injects `postgres` and `smtp` credential references from `.env`/environment variables before sending to n8n

Optional dry-run:

```bash
python3 scripts/sync_n8n_workflows.py --dry-run
```

Optional custom folder:

```bash
python3 scripts/sync_n8n_workflows.py --workflows-dir workflow-n8n
```