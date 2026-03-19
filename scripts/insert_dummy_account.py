#!/usr/bin/env python3
"""
Insert a single dummy account into the NovaSeat database.

All fields are configurable via CLI flags. Defaults create a high-spending,
disengaged account (likely to churn) with NO scoring output — simulating
a new account that Workflow 1 has not yet scored.

Usage:
    # Insert with all defaults
    python scripts/insert_dummy_account.py

    # Customize fields
    python scripts/insert_dummy_account.py --name "Jane Doe" --email jane@example.com --annual-revenue 80000

    # Dry run
    python scripts/insert_dummy_account.py --dry-run
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2


def load_dotenv_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "novaseat"),
        user=os.getenv("POSTGRES_USER", "novaseat"),
        password=os.getenv("POSTGRES_PASSWORD", "novaseat_secret"),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Insert a dummy account into the NovaSeat DB")

    # Identity
    p.add_argument("--name", default="Paolo Magnani", help="Account holder name")
    p.add_argument("--email", default="p.magnani2004@gmail.com", help="Account email")
    p.add_argument("--company-name", default="Magnani Events S.r.l.", help="Company name")

    # Contract / revenue  (high spender defaults)
    p.add_argument("--annual-revenue", type=float, default=110000.00, help="Annual revenue in EUR")
    p.add_argument("--monthly-charges", type=float, default=9166.67, help="Monthly charges in EUR")
    p.add_argument("--plan-type", choices=["Starter", "Pro", "Enterprise"], default="Enterprise")
    p.add_argument("--tenure-months", type=int, default=26, help="Months as customer")

    # Platform
    p.add_argument("--platform-tier", choices=["Free", "Basic", "Premium"], default="Premium")
    p.add_argument("--payment-auto", type=bool, default=True, help="Auto-pay enabled")
    p.add_argument("--paperless-billing", type=bool, default=True)

    # CSM
    p.add_argument("--has-dedicated-csm", type=bool, default=True)
    p.add_argument("--csm-email", default=None,
                   help="CSM email to assign (looked up in csm_managers). Default: first active CSM.")

    # Behavioral metrics  (disengaged defaults → high churn likelihood)
    p.add_argument("--days-since-last-login", type=int, default=45, help="Days since last login")
    p.add_argument("--events-created-this-month", type=int, default=0)
    p.add_argument("--events-per-month-trend", choices=["Declining", "Stable", "Increasing"], default="Declining")
    p.add_argument("--support-ticket-velocity", type=float, default=8.5, help="Support tickets rate")
    p.add_argument("--attendee-engagement-score", type=float, default=12.0, help="Engagement score 0-100")

    # Demographics
    p.add_argument("--senior-citizen", type=bool, default=False)
    p.add_argument("--has-partner", type=bool, default=False)
    p.add_argument("--has-dependents", type=bool, default=False)
    p.add_argument("--online-security", type=bool, default=False)
    p.add_argument("--online-backup", type=bool, default=False)
    p.add_argument("--streaming-tv", type=bool, default=False)

    # Misc
    p.add_argument("--env-file", default=".env", help="Path to .env file")
    p.add_argument("--dry-run", action="store_true", help="Print the INSERT without executing")

    return p.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv_file(args.env_file)

    # Build the row — scoring fields are explicitly NULL
    row = {
        "name": args.name,
        "email": args.email,
        "company_name": args.company_name,
        "annual_revenue": args.annual_revenue,
        "monthly_charges": args.monthly_charges,
        "plan_type": args.plan_type,
        "tenure_months": args.tenure_months,
        "platform_tier": args.platform_tier,
        "payment_auto": args.payment_auto,
        "paperless_billing": args.paperless_billing,
        "has_dedicated_csm": args.has_dedicated_csm,
        "days_since_last_login": args.days_since_last_login,
        "last_login_at": None,  # computed in SQL via make_interval
        "events_created_this_month": args.events_created_this_month,
        "events_per_month_trend": args.events_per_month_trend,
        "support_ticket_velocity": args.support_ticket_velocity,
        "attendee_engagement_score": args.attendee_engagement_score,
        "senior_citizen": args.senior_citizen,
        "has_partner": args.has_partner,
        "has_dependents": args.has_dependents,
        "online_security": args.online_security,
        "online_backup": args.online_backup,
        "streaming_tv": args.streaming_tv,
        # Scoring output — NULL (not yet scored by WF1)
        "churn_probability": None,
        "risk_tier": None,
        "previous_risk_tier": None,
        "churn_drivers": None,
        "last_scored_at": None,
        # Intervention state
        "intervention_status": "None",
    }

    if args.dry_run:
        print("DRY RUN — would insert:\n")
        for k, v in row.items():
            print(f"  {k}: {v}")
        return

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Resolve CSM
            csm_id = None
            if args.has_dedicated_csm:
                if args.csm_email:
                    cur.execute("SELECT id FROM csm_managers WHERE email = %s AND is_active = TRUE",
                                (args.csm_email,))
                else:
                    cur.execute("SELECT id FROM csm_managers WHERE is_active = TRUE LIMIT 1")
                csm_row = cur.fetchone()
                if csm_row:
                    csm_id = csm_row[0]
                else:
                    print("WARNING: No active CSM found — inserting without CSM assignment")

            cur.execute(
                """
                INSERT INTO accounts (
                    name, email, company_name,
                    annual_revenue, monthly_charges, plan_type, tenure_months,
                    platform_tier, payment_auto, paperless_billing,
                    has_dedicated_csm, csm_id,
                    days_since_last_login, last_login_at,
                    events_created_this_month, events_per_month_trend,
                    support_ticket_velocity, attendee_engagement_score,
                    senior_citizen, has_partner, has_dependents,
                    online_security, online_backup, streaming_tv,
                    churn_probability, risk_tier, previous_risk_tier,
                    churn_drivers, last_scored_at,
                    intervention_status
                ) VALUES (
                    %(name)s, %(email)s, %(company_name)s,
                    %(annual_revenue)s, %(monthly_charges)s, %(plan_type)s, %(tenure_months)s,
                    %(platform_tier)s, %(payment_auto)s, %(paperless_billing)s,
                    %(has_dedicated_csm)s, %(csm_id)s,
                    %(days_since_last_login)s,
                    NOW() - make_interval(days => %(days_since_last_login)s),
                    %(events_created_this_month)s, %(events_per_month_trend)s,
                    %(support_ticket_velocity)s, %(attendee_engagement_score)s,
                    %(senior_citizen)s, %(has_partner)s, %(has_dependents)s,
                    %(online_security)s, %(online_backup)s, %(streaming_tv)s,
                    NULL, NULL, NULL,
                    NULL, NULL,
                    %(intervention_status)s
                )
                RETURNING id
                """,
                {
                    **row,
                    "csm_id": csm_id,
                },
            )
            account_id = cur.fetchone()[0]

        conn.commit()

        # Save email to tracking file so remove_dummy_account.py knows what to delete
        tracker = Path(__file__).parent / ".last_dummy_account"
        tracker.write_text(args.email)

        print(f"Inserted account: {account_id}")
        print(f"  Name:    {args.name}")
        print(f"  Email:   {args.email}")
        print(f"  ARR:     €{args.annual_revenue:,.2f}")
        print(f"  Plan:    {args.plan_type}")
        print(f"  CSM:     {csm_id or 'None'}")
        print(f"  Scoring: NOT YET SCORED (churn_probability, risk_tier, churn_drivers = NULL)")
        print(f"  Tracked: {tracker}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
