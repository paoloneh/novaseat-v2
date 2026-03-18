-- =============================================================================
-- NovaSeat Churn Prevention Platform — Database Schema
-- PostgreSQL 16+
-- =============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- ENUM TYPES
-- =============================================================================

CREATE TYPE risk_tier AS ENUM ('Low', 'Medium', 'High', 'Critical');
CREATE TYPE plan_type AS ENUM ('Starter', 'Pro', 'Enterprise');
CREATE TYPE platform_tier AS ENUM ('Free', 'Basic', 'Premium');
CREATE TYPE event_trend AS ENUM ('Declining', 'Stable', 'Increasing');
CREATE TYPE intervention_status AS ENUM ('None', 'Pending', 'Active', 'Completed', 'Failed');
CREATE TYPE outreach_channel AS ENUM ('Email', 'Phone', 'Calendar', 'InApp');
CREATE TYPE message_status AS ENUM ('Queued', 'Sent', 'Delivered', 'Opened', 'Replied', 'Bounced', 'Failed');
CREATE TYPE task_priority AS ENUM ('Low', 'Medium', 'High', 'Urgent');
CREATE TYPE task_status AS ENUM ('Open', 'InProgress', 'Completed', 'Cancelled');
CREATE TYPE case_priority AS ENUM ('Normal', 'High', 'Critical');
CREATE TYPE case_status AS ENUM ('Open', 'InProgress', 'Escalated', 'Resolved', 'Closed');
CREATE TYPE ticket_status AS ENUM ('Open', 'Pending', 'Resolved', 'Closed');
CREATE TYPE scoring_run_status AS ENUM ('Running', 'Completed', 'Failed');
CREATE TYPE sentiment AS ENUM ('Positive', 'Neutral', 'Negative');

-- =============================================================================
-- 1. CSM MANAGERS
-- =============================================================================

CREATE TABLE csm_managers (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        VARCHAR(200) NOT NULL,
    email       VARCHAR(320) NOT NULL UNIQUE,
    calendar_id VARCHAR(500),               -- external calendar ID for booking
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_csm_active ON csm_managers (is_active) WHERE is_active = TRUE;

-- =============================================================================
-- 2. ACCOUNTS  (core table — maps to ML model input/output)
-- =============================================================================

CREATE TABLE accounts (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                     VARCHAR(300) NOT NULL,
    email                    VARCHAR(320) NOT NULL,
    company_name             VARCHAR(300),

    -- Contract / revenue
    annual_revenue           NUMERIC(12,2) NOT NULL DEFAULT 0,
    monthly_charges          NUMERIC(10,2) NOT NULL DEFAULT 0,
    plan_type                plan_type NOT NULL DEFAULT 'Starter',
    tenure_months            INT NOT NULL DEFAULT 0,

    -- Platform
    platform_tier            platform_tier NOT NULL DEFAULT 'Basic',
    payment_auto             BOOLEAN NOT NULL DEFAULT FALSE,
    paperless_billing        BOOLEAN NOT NULL DEFAULT FALSE,

    -- CSM assignment
    has_dedicated_csm        BOOLEAN NOT NULL DEFAULT FALSE,
    csm_id                   UUID REFERENCES csm_managers(id) ON DELETE SET NULL,

    -- Behavioral metrics (updated by scoring pipeline)
    days_since_last_login    INT DEFAULT 0,
    last_login_at            TIMESTAMPTZ,
    events_created_this_month INT DEFAULT 0,
    events_per_month_trend   event_trend DEFAULT 'Stable',
    support_ticket_velocity  NUMERIC(5,2) DEFAULT 0,
    attendee_engagement_score NUMERIC(5,2) DEFAULT 0,

    -- Demographic (from Telco mapping)
    senior_citizen           BOOLEAN NOT NULL DEFAULT FALSE,
    has_partner              BOOLEAN NOT NULL DEFAULT FALSE,
    has_dependents           BOOLEAN NOT NULL DEFAULT FALSE,
    online_security          BOOLEAN NOT NULL DEFAULT FALSE,
    online_backup            BOOLEAN NOT NULL DEFAULT FALSE,
    streaming_tv             BOOLEAN NOT NULL DEFAULT FALSE,

    -- Churn prediction output (written by scoring pipeline)
    churn_probability        NUMERIC(6,4),
    risk_tier                risk_tier DEFAULT 'Low',
    previous_risk_tier       risk_tier,
    churn_drivers            JSONB,          -- top-3 SHAP drivers [{driver, impact}]
    last_scored_at           TIMESTAMPTZ,

    -- Intervention state (managed by n8n Workflow 2)
    intervention_status      intervention_status NOT NULL DEFAULT 'None',

    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_accounts_risk          ON accounts (risk_tier);
CREATE INDEX idx_accounts_intervention  ON accounts (intervention_status);
CREATE INDEX idx_accounts_csm           ON accounts (csm_id);
CREATE INDEX idx_accounts_churn_prob    ON accounts (churn_probability DESC);
CREATE INDEX idx_accounts_arr           ON accounts (annual_revenue DESC);
CREATE INDEX idx_accounts_risk_change   ON accounts (risk_tier, previous_risk_tier)
    WHERE risk_tier != previous_risk_tier;

-- =============================================================================
-- 3. SCORING RUNS  (Workflow 1 — Nightly Model Trigger)
-- =============================================================================

CREATE TABLE scoring_runs (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    status           scoring_run_status NOT NULL DEFAULT 'Running',
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    accounts_scored  INT DEFAULT 0,
    model_version    VARCHAR(100),
    error_message    TEXT,
    metrics          JSONB,                 -- AUC, accuracy, tier distribution
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_scoring_runs_status ON scoring_runs (status, started_at DESC);

-- =============================================================================
-- 4. CHURN SCORES HISTORY  (track score changes over time)
-- =============================================================================

CREATE TABLE churn_scores_history (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id          UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    scoring_run_id      UUID REFERENCES scoring_runs(id) ON DELETE SET NULL,
    churn_probability   NUMERIC(6,4) NOT NULL,
    risk_tier           risk_tier NOT NULL,
    churn_drivers       JSONB,
    scored_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_churn_history_account ON churn_scores_history (account_id, scored_at DESC);
CREATE INDEX idx_churn_history_run     ON churn_scores_history (scoring_run_id);

-- =============================================================================
-- 5. INTERVENTIONS  (Workflow 2 — Churn Alert Monitor)
-- =============================================================================

CREATE TABLE interventions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id          UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    csm_id              UUID REFERENCES csm_managers(id) ON DELETE SET NULL,
    trigger_risk_tier   risk_tier NOT NULL,
    trigger_arr         NUMERIC(12,2),
    strategy            VARCHAR(100) NOT NULL,  -- 'csm_escalation', 'auto_outreach', 'product_tip', 'nurture'
    status              intervention_status NOT NULL DEFAULT 'Pending',
    outcome             VARCHAR(100),           -- 'churn_prevented', 'no_response', 'escalated', 'churned'
    notes               TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_interventions_account ON interventions (account_id);
CREATE INDEX idx_interventions_status  ON interventions (status) WHERE status IN ('Pending', 'Active');
CREATE INDEX idx_interventions_csm     ON interventions (csm_id);

-- =============================================================================
-- 6. OUTREACH MESSAGES  (Workflow 2, 3 — emails sent to customers)
-- =============================================================================

CREATE TABLE outreach_messages (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    intervention_id     UUID NOT NULL REFERENCES interventions(id) ON DELETE CASCADE,
    account_id          UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    channel             outreach_channel NOT NULL DEFAULT 'Email',
    subject             VARCHAR(500),
    body_html           TEXT,
    recipient_email     VARCHAR(320) NOT NULL,
    status              message_status NOT NULL DEFAULT 'Queued',
    sentiment_detected  sentiment,
    is_followup         BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE for 48h second-touch
    external_message_id VARCHAR(500),                    -- mail provider message ID
    sent_at             TIMESTAMPTZ,
    opened_at           TIMESTAMPTZ,
    replied_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_outreach_intervention ON outreach_messages (intervention_id);
CREATE INDEX idx_outreach_account      ON outreach_messages (account_id);
CREATE INDEX idx_outreach_status       ON outreach_messages (status);
CREATE INDEX idx_outreach_followup     ON outreach_messages (is_followup, status)
    WHERE is_followup = TRUE AND status = 'Sent';

-- =============================================================================
-- 7. TASKS  (Workflow 2, 3 — follow-up tasks for CSMs)
-- =============================================================================

CREATE TABLE tasks (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    intervention_id     UUID REFERENCES interventions(id) ON DELETE SET NULL,
    account_id          UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    assigned_to         UUID REFERENCES csm_managers(id) ON DELETE SET NULL,
    title               VARCHAR(500) NOT NULL,
    description         TEXT,
    priority            task_priority NOT NULL DEFAULT 'Medium',
    status              task_status NOT NULL DEFAULT 'Open',
    due_date            TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tasks_assigned   ON tasks (assigned_to, status);
CREATE INDEX idx_tasks_account    ON tasks (account_id);
CREATE INDEX idx_tasks_priority   ON tasks (priority, status) WHERE status IN ('Open', 'InProgress');
CREATE INDEX idx_tasks_due        ON tasks (due_date) WHERE status IN ('Open', 'InProgress');

-- =============================================================================
-- 8. CASES  (Workflow 2, 3 — high-priority escalations for ARR > €50K)
-- =============================================================================

CREATE TABLE cases (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    intervention_id     UUID REFERENCES interventions(id) ON DELETE SET NULL,
    account_id          UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    assigned_to         UUID REFERENCES csm_managers(id) ON DELETE SET NULL,
    title               VARCHAR(500) NOT NULL,
    description         TEXT,
    priority            case_priority NOT NULL DEFAULT 'High',
    status              case_status NOT NULL DEFAULT 'Open',
    resolution          TEXT,
    escalated_at        TIMESTAMPTZ,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cases_account  ON cases (account_id);
CREATE INDEX idx_cases_assigned ON cases (assigned_to, status);
CREATE INDEX idx_cases_priority ON cases (priority, status) WHERE status IN ('Open', 'InProgress', 'Escalated');

-- =============================================================================
-- 9. NPS SCORES  (collected quarterly)
-- =============================================================================

CREATE TABLE nps_scores (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id  UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    score       INT NOT NULL CHECK (score BETWEEN 0 AND 10),
    feedback    TEXT,
    quarter     VARCHAR(7) NOT NULL,        -- e.g. '2026-Q1'
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_nps_account  ON nps_scores (account_id, collected_at DESC);
CREATE INDEX idx_nps_quarter  ON nps_scores (quarter);

-- =============================================================================
-- 10. INTEGRATIONS  (Mail, Zoom, Catering vendors per account)
-- =============================================================================

CREATE TABLE integrations (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    integration_name VARCHAR(100) NOT NULL,   -- 'Mail', 'Zoom', 'CateringVendor', etc.
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    connected_at     TIMESTAMPTZ,
    disconnected_at  TIMESTAMPTZ,
    config           JSONB,                   -- integration-specific settings
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, integration_name)
);

CREATE INDEX idx_integrations_account ON integrations (account_id);
CREATE INDEX idx_integrations_active  ON integrations (account_id, is_active) WHERE is_active = TRUE;

-- =============================================================================
-- 11. FEATURE ADOPTION  (AI agenda builder, live polling, post-event analytics)
-- =============================================================================

CREATE TABLE feature_adoption (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    feature_name    VARCHAR(200) NOT NULL,   -- 'ai_agenda_builder', 'live_polling', 'post_event_analytics'
    is_adopted      BOOLEAN NOT NULL DEFAULT FALSE,
    first_used_at   TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    usage_count     INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, feature_name)
);

CREATE INDEX idx_feature_account ON feature_adoption (account_id);
CREATE INDEX idx_feature_name    ON feature_adoption (feature_name, is_adopted);

-- =============================================================================
-- 12. SUPPORT TICKETS
-- =============================================================================

CREATE TABLE support_tickets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    subject         VARCHAR(500) NOT NULL,
    description     TEXT,
    status          ticket_status NOT NULL DEFAULT 'Open',
    assigned_to     UUID REFERENCES csm_managers(id) ON DELETE SET NULL,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tickets_account ON support_tickets (account_id);
CREATE INDEX idx_tickets_status  ON support_tickets (status) WHERE status IN ('Open', 'Pending');

-- =============================================================================
-- 13. WEEKLY REPORTS  (Workflow 4 — Weekly Reporting)
-- =============================================================================

CREATE TABLE weekly_reports (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    week_start              DATE NOT NULL,
    week_end                DATE NOT NULL,
    accounts_intercepted    INT NOT NULL DEFAULT 0,
    conversations_started   INT NOT NULL DEFAULT 0,
    churn_prevented         INT NOT NULL DEFAULT 0,    -- estimated
    csm_escalations         INT NOT NULL DEFAULT 0,
    messages_sent           INT NOT NULL DEFAULT 0,
    messages_replied        INT NOT NULL DEFAULT 0,
    avg_response_time_hours NUMERIC(6,2),
    report_data             JSONB,                      -- full breakdown for the digest
    sent_to                 VARCHAR(320),               -- Head of Customer Success email
    sent_at                 TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_weekly_reports_week ON weekly_reports (week_start);

-- =============================================================================
-- TRIGGER: auto-update updated_at
-- =============================================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply to all tables with updated_at
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT table_name FROM information_schema.columns
        WHERE column_name = 'updated_at'
          AND table_schema = 'public'
    LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION update_updated_at()',
            tbl, tbl
        );
    END LOOP;
END;
$$;

-- =============================================================================
-- TRIGGER: store previous_risk_tier before account update
-- =============================================================================

CREATE OR REPLACE FUNCTION track_risk_tier_change()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.risk_tier IS DISTINCT FROM NEW.risk_tier THEN
        NEW.previous_risk_tier = OLD.risk_tier;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_accounts_risk_change
    BEFORE UPDATE ON accounts
    FOR EACH ROW
    EXECUTE FUNCTION track_risk_tier_change();

-- =============================================================================
-- VIEW: Workflow 2 — accounts newly flagged as High/Critical, not in active intervention
-- =============================================================================

CREATE VIEW v_churn_alerts AS
SELECT
    a.id,
    a.name,
    a.email,
    a.annual_revenue,
    a.plan_type,
    a.risk_tier,
    a.previous_risk_tier,
    a.churn_probability,
    a.churn_drivers,
    a.days_since_last_login,
    a.events_per_month_trend,
    a.support_ticket_velocity,
    a.csm_id,
    csm.name  AS csm_name,
    csm.email AS csm_email,
    a.last_scored_at
FROM accounts a
LEFT JOIN csm_managers csm ON csm.id = a.csm_id
WHERE a.risk_tier IN ('High', 'Critical')
  AND a.intervention_status = 'None'
  AND a.risk_tier IS DISTINCT FROM a.previous_risk_tier;

-- =============================================================================
-- VIEW: Workflow 3 — messages awaiting follow-up (sent > 48h, no reply)
-- =============================================================================

CREATE VIEW v_pending_followups AS
SELECT
    om.id           AS message_id,
    om.intervention_id,
    om.account_id,
    a.name          AS account_name,
    a.email         AS account_email,
    a.risk_tier,
    om.channel,
    om.sent_at,
    EXTRACT(EPOCH FROM (NOW() - om.sent_at)) / 3600 AS hours_since_sent
FROM outreach_messages om
JOIN accounts a ON a.id = om.account_id
WHERE om.status = 'Sent'
  AND om.is_followup = FALSE
  AND om.replied_at IS NULL
  AND om.sent_at < NOW() - INTERVAL '48 hours';

-- =============================================================================
-- VIEW: Workflow 4 — weekly aggregation helper
-- =============================================================================

CREATE VIEW v_weekly_stats AS
SELECT
    date_trunc('week', i.started_at)::DATE                              AS week_start,
    COUNT(DISTINCT i.id)                                                 AS accounts_intercepted,
    COUNT(DISTINCT om.id) FILTER (WHERE om.status != 'Queued')          AS conversations_started,
    COUNT(DISTINCT i.id) FILTER (WHERE i.outcome = 'churn_prevented')   AS churn_prevented,
    COUNT(DISTINCT c.id)                                                 AS csm_escalations,
    COUNT(DISTINCT om.id) FILTER (WHERE om.status IN ('Sent','Delivered','Opened','Replied')) AS messages_sent,
    COUNT(DISTINCT om.id) FILTER (WHERE om.status = 'Replied')          AS messages_replied
FROM interventions i
LEFT JOIN outreach_messages om ON om.intervention_id = i.id
LEFT JOIN cases c ON c.intervention_id = i.id
GROUP BY 1;
