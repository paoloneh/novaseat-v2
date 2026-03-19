#!/usr/bin/env python3
"""
Remove the last dummy account inserted by insert_dummy_account.py.

Reads the email from scripts/.last_dummy_account, deletes the account and
all related rows (interventions, outreach_messages, tasks, cases cascade via FK),
then removes the tracker file.

Usage:
    python scripts/remove_dummy_account.py

    # Preview without deleting
    python scripts/remove_dummy_account.py --dry-run
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2


TRACKER = Path(__file__).parent / ".last_dummy_account"


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
    p = argparse.ArgumentParser(description="Remove the last dummy account from the NovaSeat DB")
    p.add_argument("--env-file", default=".env", help="Path to .env file")
    p.add_argument("--dry-run", action="store_true", help="Show what would be deleted without touching the DB")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv_file(args.env_file)

    if not TRACKER.exists():
        print("No dummy account to remove — tracker file not found at", TRACKER)
        sys.exit(1)

    email = TRACKER.read_text().strip()
    if not email:
        print("Tracker file is empty — nothing to remove")
        TRACKER.unlink()
        sys.exit(1)

    print(f"Looking up account with email: {email}")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Find the account
            cur.execute(
                "SELECT id, name, annual_revenue, plan_type FROM accounts WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
            if not row:
                print(f"No account found with email '{email}' — already deleted?")
                TRACKER.unlink()
                sys.exit(0)

            account_id, name, arr, plan = row
            print(f"Found account: {account_id}")
            print(f"  Name: {name}")
            print(f"  ARR:  €{arr:,.2f}")
            print(f"  Plan: {plan}")

            if args.dry_run:
                # Show related rows that would be cascade-deleted
                for table in ("outreach_messages", "tasks", "cases", "interventions", "churn_scores_history"):
                    cur.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = %s", (account_id,))
                    count = cur.fetchone()[0]
                    if count:
                        print(f"  Would delete {count} row(s) from {table}")
                print("\nDRY RUN — no changes made")
                return

            # Delete the account — FKs with ON DELETE CASCADE handle related rows
            cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))

        conn.commit()
        TRACKER.unlink()
        print(f"Deleted account {account_id} and all related data")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
