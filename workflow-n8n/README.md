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

Both workflows require credentials to be configured in n8n:

| Credential | Where | Notes |
|---|---|---|
| **NovaSeat PostgreSQL** | All Postgres nodes | `postgresql://novaseat:novaseat_secret@localhost:5432/novaseat` |
| **NovaSeat SMTP** | All Email Send nodes | Configure with your mail provider (Gmail, SendGrid, Resend, etc.) |

### Placeholders to replace

| Placeholder | File | Description |
|---|---|---|
| `{{CSM_NAME}}` | WF2 | Replaced dynamically if CSM data is in DB; fallback needs manual config |
| `{{CALENDAR_LINK}}` | WF2 | URL to the CSM's booking page (Cal.com, Calendly, Google Calendar) |
| `{{HEAD_OF_CS_EMAIL}}` | WF4 | Email address of the Head of Customer Success |
