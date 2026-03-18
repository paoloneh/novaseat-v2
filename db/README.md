# NovaSeat — Database Documentation

This document describes the PostgreSQL database schema that powers the NovaSeat Churn Prevention Platform. The schema is designed to support four n8n automation workflows, a nightly ML scoring pipeline (Google Colab), and the key business metrics NovaSeat tracks.

---

## Quick Start

```bash
docker compose up -d
```

| Service    | URL / Connection                                                   |
|------------|--------------------------------------------------------------------|
| PostgreSQL | `postgresql://novaseat:novaseat_secret@localhost:5432/novaseat`     |
| pgAdmin    | `http://localhost:5050` — login `admin@novaseat.dev` / `admin`     |

The schema is automatically applied on first container start via `init.sql`.

---

## Architecture Overview

```
┌──────────────┐      ┌────────────────┐      ┌─────────────────────┐
│  Google Colab │─────▶│   PostgreSQL   │◀─────│   n8n Workflows     │
│  (ML Scoring) │      │   (this DB)    │      │   (Automation)      │
└──────────────┘      └────────────────┘      └─────────────────────┘
       │                      │                        │
  Reads accounts        Stores all state         Reads alerts,
  Writes scores         Triggers & views         writes interventions,
  back to DB            pre-built for n8n        tasks, cases, reports
```

---

## Tables

### 1. `csm_managers`

**Purpose:** Registry of Customer Success Managers who handle at-risk accounts.

**Why it exists:** Every intervention, task, and escalation case needs to be assigned to a real person. Workflow 2 assigns outreach to the account's CSM, Workflow 3 creates follow-up tasks for them, and the "Book a call" flow needs access to the CSM's calendar. This table is the single source of truth for CSM identity and availability.

| Column        | Description                                         |
|---------------|-----------------------------------------------------|
| `id`          | UUID primary key                                    |
| `name`        | Full name of the CSM                                |
| `email`       | Email address (used for notifications and handoffs) |
| `calendar_id` | External calendar ID for the "Success Call Offer" booking flow |
| `is_active`   | Whether the CSM is currently active                 |

---

### 2. `accounts`

**Purpose:** Core table representing NovaSeat customer accounts. This is the central entity that every other table references.

**Why it exists:** This table serves a dual role. First, it stores the account profile and behavioral metrics that the ML model reads as input features during nightly scoring (`tenure_months`, `days_since_last_login`, `events_per_month_trend`, `support_ticket_velocity`, etc.). Second, it stores the model's output (`churn_probability`, `risk_tier`, `churn_drivers`) so that n8n workflows can query it to trigger interventions. The `previous_risk_tier` column (auto-populated by a trigger) is critical for Workflow 2, which needs to detect *transitions* into High/Critical risk rather than re-alerting on every poll.

| Column Group             | Key Columns                                                         |
|--------------------------|---------------------------------------------------------------------|
| Identity                 | `name`, `email`, `company_name`                                     |
| Contract & Revenue       | `annual_revenue`, `monthly_charges`, `plan_type`, `tenure_months`   |
| Platform                 | `platform_tier`, `payment_auto`, `paperless_billing`                |
| CSM Assignment           | `has_dedicated_csm`, `csm_id` (FK to `csm_managers`)               |
| Behavioral Metrics       | `days_since_last_login`, `last_login_at`, `events_created_this_month`, `events_per_month_trend`, `support_ticket_velocity`, `attendee_engagement_score` |
| Demographic (Telco map)  | `senior_citizen`, `has_partner`, `has_dependents`, `online_security`, `online_backup`, `streaming_tv` |
| Churn Prediction Output  | `churn_probability`, `risk_tier`, `previous_risk_tier`, `churn_drivers` (JSONB), `last_scored_at` |
| Intervention State       | `intervention_status` — prevents duplicate outreach (Workflow 2 filter) |

---

### 3. `scoring_runs`

**Purpose:** Audit log for each execution of the nightly churn scoring pipeline.

**Why it exists:** Workflow 1 (Nightly Model Trigger) runs the Colab notebook at 02:00 AM. This table tracks whether each run succeeded or failed, how many accounts were scored, and captures model metrics. If a run fails, n8n reads the `error_message` to include in the failure notification email sent to the data team. It also provides traceability — you can tie any account's churn score back to the specific model run that produced it.

| Column            | Description                                              |
|-------------------|----------------------------------------------------------|
| `status`          | `Running`, `Completed`, or `Failed`                      |
| `accounts_scored` | Number of accounts processed in this run                 |
| `model_version`   | Version string of the model used                         |
| `error_message`   | Error details if the run failed (used in alert emails)   |
| `metrics`         | JSONB with AUC, accuracy, tier distribution              |

---

### 4. `churn_scores_history`

**Purpose:** Historical record of every churn score assigned to every account.

**Why it exists:** The `accounts` table only stores the *latest* score. This history table preserves every score over time, enabling trend analysis (is an account's risk increasing or decreasing?), model performance tracking, and the weekly reports in Workflow 4 that calculate "churn prevented" by comparing before/after scores around interventions.

| Column              | Description                                        |
|---------------------|----------------------------------------------------|
| `account_id`        | FK to `accounts`                                   |
| `scoring_run_id`    | FK to `scoring_runs` — links score to its run      |
| `churn_probability` | The probability at that point in time              |
| `risk_tier`         | The assigned tier at that point in time            |
| `churn_drivers`     | SHAP drivers snapshot at scoring time              |

---

### 5. `interventions`

**Purpose:** Tracks each outreach campaign launched for an at-risk account.

**Why it exists:** When Workflow 2 (Churn Alert Monitor) detects a newly flagged account, it creates an intervention record. This is the parent entity that groups together all the outreach messages, tasks, and cases related to a single "save this account" effort. The `strategy` column records which decision branch was taken (CSM escalation, autonomous email, product tip, or nurture sequence), based on the risk tier and ARR logic from the prompt. The `outcome` column tracks the final result, feeding into the weekly "churn prevented" metric.

| Column              | Description                                                      |
|---------------------|------------------------------------------------------------------|
| `account_id`        | FK to `accounts` — which account is at risk                      |
| `csm_id`            | FK to `csm_managers` — assigned CSM for this intervention        |
| `trigger_risk_tier` | The risk tier that triggered this intervention                   |
| `trigger_arr`       | The account's ARR when triggered (for the €50K escalation rule)  |
| `strategy`          | `csm_escalation`, `auto_outreach`, `product_tip`, or `nurture`   |
| `status`            | `None` → `Pending` → `Active` → `Completed` / `Failed`          |
| `outcome`           | `churn_prevented`, `no_response`, `escalated`, `churned`         |

---

### 6. `outreach_messages`

**Purpose:** Individual emails and messages sent to customers as part of an intervention.

**Why it exists:** Each intervention may generate multiple messages — an initial personalized email (Workflow 2), a 48-hour follow-up via a different channel if no reply (Workflow 3), or additional touches. This table tracks the full lifecycle of each message (queued → sent → delivered → opened → replied) so that Workflow 3 can detect non-responses and Workflow 4 can count conversations started. The `sentiment_detected` column supports the real-time sentiment analysis requirement, and `is_followup` distinguishes first-touch from second-touch messages.

| Column                | Description                                              |
|-----------------------|----------------------------------------------------------|
| `intervention_id`     | FK to `interventions` — parent campaign                  |
| `channel`             | `Email`, `Phone`, `Calendar`, or `InApp`                 |
| `subject` / `body_html` | The actual message content (HTML template output)     |
| `status`              | Full lifecycle: `Queued` → `Sent` → `Delivered` → `Opened` → `Replied` |
| `sentiment_detected`  | AI-detected sentiment from customer replies              |
| `is_followup`         | `TRUE` for the 48-hour second-touch (Workflow 3)         |
| `external_message_id` | Mail provider's ID for tracking delivery/opens           |

---

### 7. `tasks`

**Purpose:** Action items assigned to CSMs for manual follow-up.

**Why it exists:** Workflow 2 creates a follow-up task when it triggers an intervention ("Call Sarah at Acme Corp about declining event usage"). Workflow 3 creates a high-priority task when a conversation ends without resolution. These tasks ensure nothing falls through the cracks when automated outreach isn't enough and a human needs to take over.

| Column            | Description                                          |
|-------------------|------------------------------------------------------|
| `intervention_id` | FK to `interventions` — optional link to campaign    |
| `account_id`      | FK to `accounts` — which customer this is about      |
| `assigned_to`     | FK to `csm_managers` — who needs to act              |
| `priority`        | `Low`, `Medium`, `High`, or `Urgent`                 |
| `status`          | `Open` → `InProgress` → `Completed` / `Cancelled`   |
| `due_date`        | Deadline for the task                                |

---

### 8. `cases`

**Purpose:** High-priority escalation records for accounts with ARR above €50,000.

**Why it exists:** The prompt specifies that when `risk_tier = Critical AND ARR > €50K`, the system must immediately escalate to a CSM. Cases are different from tasks — they represent formal escalations with their own lifecycle (`Open` → `Escalated` → `Resolved`), priority tracking, and resolution notes. Workflow 3 also creates cases when automated conversations fail to resolve the issue. The weekly report (Workflow 4) counts CSM escalations from this table.

| Column            | Description                                          |
|-------------------|------------------------------------------------------|
| `intervention_id` | FK to `interventions` — the triggering campaign      |
| `account_id`      | FK to `accounts`                                     |
| `assigned_to`     | FK to `csm_managers`                                 |
| `priority`        | `Normal`, `High`, or `Critical`                      |
| `status`          | `Open` → `InProgress` → `Escalated` → `Resolved` → `Closed` |
| `resolution`      | Free-text description of how it was resolved         |

---

### 9. `nps_scores`

**Purpose:** Stores quarterly Net Promoter Score surveys per account.

**Why it exists:** NPS is one of the key metrics NovaSeat tracks. Storing individual scores per quarter enables trend analysis (is satisfaction dropping before churn?) and enriches the account profile that Workflow 2 pulls when generating personalized outreach. A declining NPS is a strong churn signal that complements the ML model's prediction.

| Column        | Description                                  |
|---------------|----------------------------------------------|
| `account_id`  | FK to `accounts`                             |
| `score`       | 0–10 NPS score                               |
| `feedback`    | Optional text feedback from the customer     |
| `quarter`     | Period identifier, e.g. `2026-Q1`            |

---

### 10. `integrations`

**Purpose:** Tracks which third-party integrations (Mail, Zoom, Catering vendors) each account has active.

**Why it exists:** "Integrations active" is a key metric NovaSeat tracks and a strong engagement signal. An account that disconnects integrations may be disengaging from the platform. This data feeds into the account profile that the ML model and n8n workflows use. The `config` JSONB column stores integration-specific settings without requiring schema changes for each new integration type.

| Column             | Description                                       |
|--------------------|---------------------------------------------------|
| `account_id`       | FK to `accounts`                                  |
| `integration_name` | e.g. `Mail`, `Zoom`, `CateringVendor`             |
| `is_active`        | Whether currently connected                       |
| `connected_at`     | When the integration was set up                   |
| `disconnected_at`  | When it was disconnected (if applicable)          |
| `config`           | JSONB for integration-specific settings            |

---

### 11. `feature_adoption`

**Purpose:** Tracks which platform features each account has adopted and how often they use them.

**Why it exists:** "Feature adoption rate" is a key metric, and low adoption after onboarding is cited as the #1 historical churn reason. This table tracks specific features like "AI agenda builder", "live polling", and "post-event analytics". When Workflow 2 generates a personalized product tip for High-risk accounts, it queries this table to recommend features the account hasn't tried yet. The `usage_count` and recency of `last_used_at` help distinguish accounts that tried a feature once from those actively using it.

| Column          | Description                                        |
|-----------------|----------------------------------------------------|
| `account_id`    | FK to `accounts`                                   |
| `feature_name`  | e.g. `ai_agenda_builder`, `live_polling`           |
| `is_adopted`    | Whether the account has meaningfully adopted it    |
| `first_used_at` | First usage timestamp                              |
| `last_used_at`  | Most recent usage                                  |
| `usage_count`   | Total number of times the feature was used         |

---

### 12. `support_tickets`

**Purpose:** History of support tickets opened by each account.

**Why it exists:** "Support tickets opened" is a tracked metric, and `support_ticket_velocity` (tickets per unit time) is a direct feature in the ML model. This table provides the raw data from which that velocity is calculated. It also supports the chatbot flow where "I have a problem" automatically opens a ticket. High ticket volume with unresolved issues is a strong churn predictor.

| Column        | Description                                      |
|---------------|--------------------------------------------------|
| `account_id`  | FK to `accounts`                                 |
| `subject`     | Ticket title                                     |
| `description` | Full ticket description                          |
| `status`      | `Open` → `Pending` → `Resolved` → `Closed`      |
| `assigned_to` | FK to `csm_managers` — who is handling this      |

---

### 13. `weekly_reports`

**Purpose:** Stores the aggregated weekly digest that Workflow 4 sends every Monday to the Head of Customer Success.

**Why it exists:** Workflow 4 computes weekly KPIs (accounts intercepted, conversations started, churn prevented, CSM escalations) and formats them into a report. Storing these snapshots provides a historical record of platform effectiveness over time and avoids recomputing past weeks. The `report_data` JSONB column holds the full breakdown used to render the email digest.

| Column                    | Description                                      |
|---------------------------|--------------------------------------------------|
| `week_start` / `week_end` | The reporting period                            |
| `accounts_intercepted`    | Unique accounts that received an intervention    |
| `conversations_started`   | Messages that were actually sent                 |
| `churn_prevented`         | Estimated saves (outcome = `churn_prevented`)    |
| `csm_escalations`         | Number of cases created                          |
| `messages_sent`           | Total outreach messages delivered                |
| `messages_replied`        | Messages that got a customer reply               |
| `report_data`             | JSONB with the full digest breakdown             |

---

## Views

### `v_churn_alerts`

**Used by:** Workflow 2 (Churn Alert Monitor)

Returns accounts where `risk_tier` just changed to `High` or `Critical` and no intervention is currently active. This is the primary query n8n polls each morning to decide which accounts need outreach.

### `v_pending_followups`

**Used by:** Workflow 3 (Escalation & Follow-up)

Returns outreach messages that were sent more than 48 hours ago and have not received a reply. Workflow 3 uses this to trigger second-touch messages via a different channel.

### `v_weekly_stats`

**Used by:** Workflow 4 (Weekly Reporting)

Aggregates intervention outcomes by week, joining interventions, outreach messages, and cases. Provides the raw numbers that get formatted into the Monday morning digest.

---

## Triggers

### `update_updated_at`

Applied to all tables that have an `updated_at` column. Automatically sets `updated_at = NOW()` on every `UPDATE`, so application code doesn't need to manage it.

### `track_risk_tier_change`

Applied to the `accounts` table. Before any update, if `risk_tier` has changed, the old value is copied to `previous_risk_tier`. This is essential for the `v_churn_alerts` view, which filters on `risk_tier IS DISTINCT FROM previous_risk_tier` to detect new transitions rather than re-alerting on already-flagged accounts.

---

## Entity Relationship Summary

```
csm_managers ─────────┬──────────────┬──────────────┬───────────────┐
                       │              │              │               │
                  accounts ───── interventions ── outreach_messages │
                   │  │  │           │    │                         │
                   │  │  │           │    ├──── tasks ──────────────┘
                   │  │  │           │    └──── cases
                   │  │  │           │
                   │  │  │     scoring_runs
                   │  │  │           │
                   │  │  └── churn_scores_history
                   │  │
                   │  ├── nps_scores
                   │  ├── integrations
                   │  ├── feature_adoption
                   │  └── support_tickets
                   │
              weekly_reports (standalone, aggregated)
```

---

## Workflow → Table Mapping

| Workflow | Reads From | Writes To |
|----------|-----------|-----------|
| **WF1** Nightly Model Trigger | `accounts` (features) | `accounts` (scores), `scoring_runs`, `churn_scores_history` |
| **WF2** Churn Alert Monitor | `v_churn_alerts`, `accounts`, `feature_adoption` | `interventions`, `outreach_messages`, `tasks`, `cases`, `accounts` (intervention_status) |
| **WF3** Escalation & Follow-up | `v_pending_followups`, `outreach_messages` | `outreach_messages`, `tasks`, `cases`, `accounts` (risk_tier update on win) |
| **WF4** Weekly Reporting | `v_weekly_stats`, `interventions`, `outreach_messages`, `cases` | `weekly_reports` |
| **Colab** Scoring Pipeline | `accounts` (via API) | `accounts` (scores), `churn_scores_history` |
