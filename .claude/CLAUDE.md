# NovaSeat v2 - Project Context & Analysis

## What is NovaSeat?

NovaSeat is a B2B SaaS platform for corporate event and conference management. It targets mid-to-large companies (200-5,000 employees) helping HR teams, Marketing ops, Executive Assistants, and Event Managers plan, manage, and analyze corporate events (town halls, sales kickoffs, client summits, product launches).

- **Industry**: B2B SaaS — Corporate Event & Conference Management
- **Market**: Europe and North America
- **Pricing**: Annual subscription, €8,000/year (Starter) to €120,000/year (Enterprise)
- **Churn rates**: ~18% SMB annually, ~7% Enterprise

## This Repository: Churn Prevention Agent

This repo implements an **AI-powered churn prevention system** that:
1. Predicts which clients are likely to leave the platform
2. Automatically intervenes with personalized engagement strategies

### Architecture Overview

The system has three major components:

#### 1. Google Colab — Prediction Engine (`colab/`)
ML model trained on customer behavior data (based on IBM Telco Churn Dataset adapted to B2B SaaS context). Two notebooks:
- **train_colab.ipynb**: Trains the churn prediction model
- **score_colab.ipynb**: Scores current customers and writes risk tiers to the database

#### 2. PostgreSQL Database (`db/`, `docker-compose.yml`)
Dockerized PostgreSQL instance storing customer data, churn scores, risk tiers, and intervention tracking.

#### 3. n8n — Workflow Orchestration (external)
Four automated workflows form the operational backbone:

| Workflow | Purpose | Trigger |
|----------|---------|---------|
| **Nightly Model Trigger** | Executes churn scoring notebook, validates output, alerts on failure | Cron at 02:00 AM |
| **Churn Alert Monitor** | Detects newly high-risk accounts, generates personalized outreach, escalates when needed | Daily morning poll or manual trigger |
| **Escalation & Follow-up** | Monitors responses, handles 48h no-reply, logs wins/losses | Event-driven (email monitoring) |
| **Weekly Reporting** | Aggregates outcomes, sends digest to Head of Customer Success | Every Monday morning or manual trigger |

### Key Metrics Tracked
- Events created per month
- Attendee engagement scores
- Active integrations (Mail, Zoom, Catering vendors)
- Support tickets opened
- Last login date per user
- NPS score (quarterly)
- Feature adoption rate (AI agenda builder, live polling, post-event analytics)

### Churn Alert Decision Logic

```
IF risk_tier = "Critical" AND ARR > €50K
  → Immediate CSM escalation + parallel email outreach

IF risk_tier = "Critical" AND ARR ≤ €50K
  → Autonomous email outreach + offer success call

IF risk_tier = "High"
  → Personalized email with product tip based on top churn driver

IF risk_tier = "Medium"
  → Add to nurture sequence, no immediate action
```

### Outreach Strategy
- Personalized opening referencing the specific issue detected
- Triage branching: frustrated / confused / budget-constrained / busy
- Resolution paths: product walkthrough, auto-ticket, CSM escalation, calendar booking
- Real-time sentiment analysis with CSM alerts
- Seamless human handoff with full conversation transcript

## Development Notes

- Database runs via Docker Compose (`docker-compose.yml`)
- Colab notebooks are designed to run from GitHub

### n8n Workflow Management

#### Workflow files (`workflow-n8n/`)

n8n workflows are stored as JSON files in the `workflow-n8n/` directory. Each file represents a single workflow exported from n8n. File naming convention uses a numbered prefix matching the workflow number:

```
workflow-n8n/
├── workflow2-churn-alert-monitor.json
├── workflow4-weekly-reporting.json
└── ...
```

- Files must be valid JSON objects with a `"name"` key at the top level (the workflow name used for upsert matching).
- Workflow JSON should **not** contain credential secrets — the sync script injects credentials at deploy time.

#### Syncing workflows to n8n (`scripts/sync_n8n_workflows.py`)

The sync script performs an **upsert by workflow name**: if a workflow with the same name exists in n8n it is updated, otherwise it is created.

**Prerequisites**: set the following in `.env` (or as environment variables):

| Variable | Required | Description |
|----------|----------|-------------|
| `N8N_API_KEY` | Yes | n8n instance API key |
| `N8N_BASE_URL` | No | n8n base URL (default: `http://localhost:5678`) |
| `N8N_POSTGRES_CREDENTIAL_ID` | No | Existing n8n credential ID for Postgres nodes |
| `N8N_POSTGRES_CREDENTIAL_NAME` | No | Credential name to look up in n8n (default: `NovaSeat PostgreSQL`) |
| `N8N_SMTP_CREDENTIAL_ID` | No | Existing n8n credential ID for email nodes |
| `N8N_SMTP_CREDENTIAL_NAME` | No | SMTP credential name to look up (default: `NovaSeat SMTP`) |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Fallback | Used for inline Postgres credentials when no credential ID is set |

**Usage**:

```bash
# Sync all workflows from workflow-n8n/ to the local n8n instance
python scripts/sync_n8n_workflows.py

# Dry run — show what would be created/updated without making changes
python scripts/sync_n8n_workflows.py --dry-run

# Point to a different n8n instance
python scripts/sync_n8n_workflows.py --base-url https://n8n.example.com --api-key <key>

# Sync from a custom directory
python scripts/sync_n8n_workflows.py --workflows-dir /path/to/workflows
```

**Credential injection**: the script automatically injects credentials into Postgres and email nodes at sync time. Resolution order:
1. Explicit credential ID from env (`N8N_POSTGRES_CREDENTIAL_ID` / `N8N_SMTP_CREDENTIAL_ID`)
2. Lookup by credential name in n8n
3. (Postgres only) Inline connection from `POSTGRES_*` env vars
