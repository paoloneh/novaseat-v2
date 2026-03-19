#!/usr/bin/env python3
"""
NovaSeat DB Seeder — Pull scored dataset from Google Drive, generate missing
fields (CSM managers, company names, engagement metrics, etc.), and populate
the PostgreSQL database.

Prerequisites:
    pip install psycopg2-binary google-api-python-client google-auth pandas numpy faker

Usage:
    # Pull accounts_seed.csv from Google Drive and seed the DB
    python scripts/seed_db.py

    # Use a local CSV instead of downloading from Drive
    python scripts/seed_db.py --local-csv path/to/accounts_seed.csv

    # Also provide the original Telco CSV for full field coverage
    python scripts/seed_db.py --telco-csv colab/WA_Fn-UseC_-Telco-Customer-Churn.csv

    # Dry run — show what would be inserted without touching the DB
    python scripts/seed_db.py --dry-run

    # Clear existing data before seeding
    python scripts/seed_db.py --clear

Environment variables (from .env):
    POSTGRES_HOST       (default: localhost)
    POSTGRES_PORT       (default: 5432)
    POSTGRES_DB         (default: novaseat)
    POSTGRES_USER       (default: novaseat)
    POSTGRES_PASSWORD   (required)
    GOOGLE_DRIVE_FILE_ID           — file ID of accounts_seed.csv on Drive
    GOOGLE_SERVICE_ACCOUNT_FILE    — path to service account JSON key
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

# Optional: Google Drive download
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    import io

    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

# Optional: realistic fake data
try:
    from faker import Faker

    fake = Faker()
    Faker.seed(42)
    HAS_FAKER = True
except ImportError:
    HAS_FAKER = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SEED = 42
rng = np.random.RandomState(SEED)

# ---------------------------------------------------------------------------
# Telco-to-NovaSeat mapping constants (same as train.py)
# ---------------------------------------------------------------------------

CONTRACT_TO_PLAN = {
    "Month-to-month": "Starter",
    "One year": "Pro",
    "Two year": "Enterprise",
}

INTERNET_TO_TIER = {
    "DSL": "Basic",
    "Fiber optic": "Premium",
    "No": "Free",
}

AUTO_PAYMENT_METHODS = {"Bank transfer (automatic)", "Credit card (automatic)"}

# ---------------------------------------------------------------------------
# CSM manager pool
# ---------------------------------------------------------------------------

CSM_MANAGERS = [
    {"name": "Elena Rossi", "email": "elena.rossi@novaseat.dev", "calendar_id": "elena.rossi@novaseat.dev"},
    {"name": "Marco Bianchi", "email": "marco.bianchi@novaseat.dev", "calendar_id": "marco.bianchi@novaseat.dev"},
    {"name": "Sofia Mueller", "email": "sofia.mueller@novaseat.dev", "calendar_id": "sofia.mueller@novaseat.dev"},
    {"name": "James Carter", "email": "james.carter@novaseat.dev", "calendar_id": "james.carter@novaseat.dev"},
    {"name": "Lea Dupont", "email": "lea.dupont@novaseat.dev", "calendar_id": "lea.dupont@novaseat.dev"},
]


# ---------------------------------------------------------------------------
# Google Drive download
# ---------------------------------------------------------------------------


def download_from_drive(file_id: str, sa_file: str | None = None) -> str:
    """Download a file from Google Drive by file ID, return local path."""
    if not HAS_GOOGLE:
        logger.error(
            "google-api-python-client / google-auth not installed. "
            "Install with: pip install google-api-python-client google-auth"
        )
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]

    if sa_file:
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
    else:
        from google.auth import default

        creds, _ = default(scopes=scopes)

    service = build("drive", "v3", credentials=creds)

    # Get file metadata for the name
    meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    logger.info("Downloading '%s' from Google Drive...", meta["name"])

    request = service.files().get_media(fileId=file_id)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    downloader = MediaIoBaseDownload(io.FileIO(tmp.name, "wb"), request)

    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            logger.info("  Download progress: %d%%", int(status.progress() * 100))

    logger.info("Downloaded to %s", tmp.name)
    return tmp.name


# ---------------------------------------------------------------------------
# Load and enrich data
# ---------------------------------------------------------------------------


def load_seed_csv(csv_path: str) -> pd.DataFrame:
    """Load the accounts_seed.csv produced by train_colab.ipynb."""
    logger.info("Loading seed CSV: %s", csv_path)
    df = pd.read_csv(csv_path)
    logger.info("Loaded %d accounts", len(df))
    return df


def load_telco_csv(csv_path: str) -> pd.DataFrame:
    """Load the original Telco CSV to recover fields not in accounts_seed.csv."""
    logger.info("Loading Telco CSV: %s", csv_path)
    raw = pd.read_csv(csv_path)
    raw["TotalCharges"] = pd.to_numeric(raw["TotalCharges"], errors="coerce").fillna(0.0)
    logger.info("Loaded %d rows from Telco dataset", len(raw))
    return raw


def generate_company_name(index: int) -> str:
    """Generate a realistic company name."""
    if HAS_FAKER:
        return fake.company()
    # Fallback: deterministic names
    prefixes = ["Acme", "Nova", "Global", "Peak", "Summit", "Vertex", "Prime", "Core", "Arc", "Nexus"]
    suffixes = ["Corp", "Inc", "Ltd", "Group", "Solutions", "Tech", "Systems", "Partners", "Digital", "Labs"]
    return f"{prefixes[index % len(prefixes)]} {suffixes[(index * 7) % len(suffixes)]}"


def enrich_accounts(seed_df: pd.DataFrame, telco_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Add missing DB fields to the seed data."""
    df = seed_df.copy()
    n = len(df)
    now = datetime.now(timezone.utc)

    # --- company_name ---
    df["company_name"] = [generate_company_name(i) for i in range(n)]

    # --- platform_tier (derive from plan if telco not available) ---
    if "platform_tier" not in df.columns:
        if telco_df is not None and len(telco_df) == n:
            df["platform_tier"] = telco_df["InternetService"].map(INTERNET_TO_TIER)
        else:
            # Heuristic: Enterprise → Premium, Pro → Basic, Starter → mix
            tier_map = {"Enterprise": "Premium", "Pro": "Basic", "Starter": "Free"}
            df["platform_tier"] = df["plan_type"].map(tier_map)
            # Override some Starter accounts to Basic
            starter_mask = df["plan_type"] == "Starter"
            starter_idx = df[starter_mask].index
            upgrade_count = len(starter_idx) // 3
            if upgrade_count > 0:
                upgrade_idx = rng.choice(starter_idx, size=upgrade_count, replace=False)
                df.loc[upgrade_idx, "platform_tier"] = "Basic"

    # --- payment_auto ---
    if "payment_auto" not in df.columns:
        if telco_df is not None and len(telco_df) == n:
            df["payment_auto"] = telco_df["PaymentMethod"].apply(
                lambda v: v in AUTO_PAYMENT_METHODS
            )
        else:
            # ~40% auto-pay
            df["payment_auto"] = rng.random(n) < 0.4

    # --- paperless_billing ---
    if "paperless_billing" not in df.columns:
        if telco_df is not None and len(telco_df) == n:
            df["paperless_billing"] = telco_df["PaperlessBilling"] == "Yes"
        else:
            df["paperless_billing"] = rng.random(n) < 0.6

    # --- Demographic booleans ---
    for col, telco_col in [
        ("senior_citizen", "SeniorCitizen"),
        ("has_partner", "Partner"),
        ("has_dependents", "Dependents"),
        ("online_security", "OnlineSecurity"),
        ("online_backup", "OnlineBackup"),
        ("streaming_tv", "StreamingTV"),
    ]:
        if col not in df.columns:
            if telco_df is not None and len(telco_df) == n:
                if telco_col == "SeniorCitizen":
                    df[col] = telco_df[telco_col].astype(bool)
                else:
                    df[col] = telco_df[telco_col] == "Yes"
            else:
                df[col] = rng.random(n) < 0.3

    # --- last_login_at (derived from days_since_last_login) ---
    df["last_login_at"] = df["days_since_last_login"].apply(
        lambda d: (now - timedelta(days=int(d))).isoformat()
    )

    # --- events_created_this_month ---
    # Correlated with trend: Declining → 0-3, Stable → 3-8, Increasing → 6-15
    trend_col = df["events_per_month_trend"]
    events = np.zeros(n, dtype=int)
    for i in range(n):
        trend = trend_col.iloc[i]
        if trend in ("Declining", -1):
            events[i] = rng.randint(0, 4)
        elif trend in ("Stable", 0):
            events[i] = rng.randint(3, 9)
        else:
            events[i] = rng.randint(6, 16)
    df["events_created_this_month"] = events

    # --- attendee_engagement_score (0-100) ---
    # Higher for active accounts (low days_since_last_login)
    base_score = 100 - df["days_since_last_login"].clip(0, 90) * (80 / 90)
    noise = rng.normal(0, 10, n)
    df["attendee_engagement_score"] = np.clip(base_score + noise, 0, 100).round(2)

    # --- last_scored_at ---
    df["last_scored_at"] = now.isoformat()

    # --- CSM assignment ---
    # Accounts with has_dedicated_csm=True get a CSM assigned
    csm_col = df["has_dedicated_csm"].astype(bool) if "has_dedicated_csm" in df.columns else pd.Series([False] * n)
    csm_indices = rng.randint(0, len(CSM_MANAGERS), size=n)
    df["csm_index"] = np.where(csm_col, csm_indices, -1)

    logger.info("Enriched %d accounts with missing fields", n)
    return df


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


def get_db_connection(args: argparse.Namespace):
    """Create a PostgreSQL connection from env vars."""
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "novaseat"),
        user=os.getenv("POSTGRES_USER", "novaseat"),
        password=os.getenv("POSTGRES_PASSWORD", "novaseat_secret"),
    )
    conn.autocommit = False
    return conn


def clear_tables(conn) -> None:
    """Delete all data from seeded tables (in FK-safe order)."""
    tables = [
        "outreach_messages",
        "tasks",
        "cases",
        "interventions",
        "churn_scores_history",
        "scoring_runs",
        "accounts",
        "csm_managers",
        "weekly_reports",
    ]
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"DELETE FROM {table}")
            logger.info("  Cleared %s (%d rows)", table, cur.rowcount)
    conn.commit()


def insert_csm_managers(conn) -> dict[int, str]:
    """Insert CSM managers and return {index: uuid} mapping."""
    csm_ids = {}
    with conn.cursor() as cur:
        for i, csm in enumerate(CSM_MANAGERS):
            cur.execute(
                """
                INSERT INTO csm_managers (name, email, calendar_id, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (csm["name"], csm["email"], csm["calendar_id"]),
            )
            csm_ids[i] = str(cur.fetchone()[0])
    conn.commit()
    logger.info("Inserted/updated %d CSM managers", len(csm_ids))
    return csm_ids


def insert_accounts(conn, df: pd.DataFrame, csm_ids: dict[int, str]) -> None:
    """Bulk-insert accounts into the database."""
    columns = [
        "name", "email", "company_name",
        "annual_revenue", "monthly_charges", "plan_type", "tenure_months",
        "platform_tier", "payment_auto", "paperless_billing",
        "has_dedicated_csm", "csm_id",
        "days_since_last_login", "last_login_at",
        "events_created_this_month", "events_per_month_trend",
        "support_ticket_velocity", "attendee_engagement_score",
        "senior_citizen", "has_partner", "has_dependents",
        "online_security", "online_backup", "streaming_tv",
        "churn_probability", "risk_tier", "churn_drivers",
        "last_scored_at", "intervention_status",
    ]

    rows = []
    for _, row in df.iterrows():
        csm_idx = int(row["csm_index"])
        csm_id = csm_ids.get(csm_idx) if csm_idx >= 0 else None

        # Normalize events_per_month_trend to DB enum value
        trend = row["events_per_month_trend"]
        if isinstance(trend, (int, float)):
            trend = {-1: "Declining", 0: "Stable", 1: "Increasing"}.get(int(trend), "Stable")

        # Parse churn_drivers — ensure it's valid JSON
        drivers = row.get("churn_drivers")
        if isinstance(drivers, str):
            try:
                json.loads(drivers)  # validate
            except (json.JSONDecodeError, TypeError):
                drivers = None
        elif drivers is not None and not isinstance(drivers, str):
            drivers = json.dumps(drivers) if not pd.isna(drivers) else None

        rows.append((
            row["name"],
            row["email"],
            row["company_name"],
            float(row["annual_revenue"]),
            float(row["monthly_charges"]),
            row["plan_type"],
            int(row["tenure_months"]),
            row["platform_tier"],
            bool(row.get("payment_auto", False)),
            bool(row.get("paperless_billing", False)),
            bool(row["has_dedicated_csm"]),
            csm_id,
            int(row["days_since_last_login"]),
            row["last_login_at"],
            int(row["events_created_this_month"]),
            trend,
            float(row["support_ticket_velocity"]),
            float(row["attendee_engagement_score"]),
            bool(row.get("senior_citizen", False)),
            bool(row.get("has_partner", False)),
            bool(row.get("has_dependents", False)),
            bool(row.get("online_security", False)),
            bool(row.get("online_backup", False)),
            bool(row.get("streaming_tv", False)),
            float(row["churn_probability"]),
            row["risk_tier"],
            drivers,
            row["last_scored_at"],
            row.get("intervention_status", "None"),
        ))

    placeholders = ", ".join(["%s"] * len(columns))
    col_names = ", ".join(columns)
    sql = f"INSERT INTO accounts ({col_names}) VALUES ({placeholders})"

    with conn.cursor() as cur:
        for batch_start in range(0, len(rows), 500):
            batch = rows[batch_start : batch_start + 500]
            cur.executemany(sql, batch)
            logger.info(
                "  Inserted accounts %d-%d / %d",
                batch_start + 1,
                min(batch_start + 500, len(rows)),
                len(rows),
            )
    conn.commit()
    logger.info("Inserted %d accounts", len(rows))


def insert_initial_scoring_run(conn, n_accounts: int) -> None:
    """Record the seed as an initial scoring run."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scoring_runs (status, accounts_scored, model_version, completed_at)
            VALUES ('Completed', %s, 'seed-v1.0', NOW())
            """,
            (n_accounts,),
        )
    conn.commit()
    logger.info("Recorded initial scoring run (%d accounts)", n_accounts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_dotenv_file(path: str = ".env") -> None:
    """Minimal .env loader (avoids python-dotenv dependency)."""
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull scored dataset from Google Drive and seed the NovaSeat database"
    )
    parser.add_argument(
        "--local-csv",
        help="Path to a local accounts_seed.csv (skip Google Drive download)",
    )
    parser.add_argument(
        "--telco-csv",
        default="colab/WA_Fn-UseC_-Telco-Customer-Churn.csv",
        help="Path to the original Telco CSV for full field coverage (default: colab/WA_Fn-UseC_-Telco-Customer-Churn.csv)",
    )
    parser.add_argument(
        "--drive-file-id",
        default=None,
        help="Google Drive file ID for accounts_seed.csv (overrides GOOGLE_DRIVE_FILE_ID env var)",
    )
    parser.add_argument(
        "--sa-file",
        default=None,
        help="Path to Google service account JSON key (overrides GOOGLE_SERVICE_ACCOUNT_FILE env var)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete all existing data before seeding",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be inserted without touching the database",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: .env)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv_file(args.env_file)

    # ---- Step 1: Get accounts_seed.csv ----
    if args.local_csv:
        csv_path = args.local_csv
    else:
        file_id = args.drive_file_id or os.getenv("GOOGLE_DRIVE_FILE_ID")
        if not file_id:
            logger.error(
                "No CSV source specified. Use --local-csv or set GOOGLE_DRIVE_FILE_ID / --drive-file-id"
            )
            sys.exit(1)
        sa_file = args.sa_file or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        csv_path = download_from_drive(file_id, sa_file)

    seed_df = load_seed_csv(csv_path)

    # ---- Step 2: Load Telco CSV (optional, for full field recovery) ----
    telco_df = None
    telco_path = Path(args.telco_csv)
    if telco_path.exists():
        telco_df = load_telco_csv(str(telco_path))
        if len(telco_df) != len(seed_df):
            logger.warning(
                "Telco CSV has %d rows but seed CSV has %d — ignoring Telco data",
                len(telco_df),
                len(seed_df),
            )
            telco_df = None
    else:
        logger.info("Telco CSV not found at %s — generating all missing fields synthetically", telco_path)

    # ---- Step 3: Enrich with missing fields ----
    enriched_df = enrich_accounts(seed_df, telco_df)

    # ---- Summary ----
    logger.info("--- Seed Summary ---")
    logger.info("Total accounts: %d", len(enriched_df))
    logger.info(
        "Risk distribution: %s",
        enriched_df["risk_tier"].value_counts().to_dict(),
    )
    logger.info(
        "Plan distribution: %s",
        enriched_df["plan_type"].value_counts().to_dict(),
    )
    logger.info(
        "CSM assigned: %d / %d",
        (enriched_df["csm_index"] >= 0).sum(),
        len(enriched_df),
    )

    if args.dry_run:
        logger.info("DRY RUN — no database changes made")
        print("\nSample rows:")
        print(enriched_df.head(3).to_string())
        return

    # ---- Step 4: Insert into database ----
    conn = get_db_connection(args)
    try:
        if args.clear:
            logger.info("Clearing existing data...")
            clear_tables(conn)

        csm_ids = insert_csm_managers(conn)
        insert_accounts(conn, enriched_df, csm_ids)
        insert_initial_scoring_run(conn, len(enriched_df))

        logger.info("Database seeding complete!")
    except Exception:
        conn.rollback()
        logger.exception("Database error — rolled back")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
