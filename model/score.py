#!/usr/bin/env python3
"""
NovaSeat Churn Scoring — Production Scorer

Loads the trained model artifacts, reads accounts from the DB (via API or CSV),
scores them, and writes back churn_probability, risk_tier, and churn_drivers.

This script is what n8n Workflow 1 triggers nightly via Cloud Function.

Usage:
    # Score from DB API (production)
    python model/score.py --db-url http://localhost:3000/api/accounts

    # Score from CSV (testing / local)
    python model/score.py --csv data/accounts_export.csv --output data/scored.csv

    # Dry run (print results, don't write back)
    python model/score.py --csv data/accounts_export.csv --dry-run
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

RISK_TIERS = {
    "Low": (0.0, 0.3),
    "Medium": (0.3, 0.5),
    "High": (0.5, 0.7),
    "Critical": (0.7, 1.0),
}

# ---------------------------------------------------------------------------
# Load artifacts
# ---------------------------------------------------------------------------


def load_artifacts(artifacts_dir: str):
    """Load model, scaler, and feature columns from the artifacts directory."""
    d = Path(artifacts_dir)
    model = joblib.load(d / "model.joblib")
    scaler = joblib.load(d / "scaler.joblib")
    with open(d / "feature_columns.json") as f:
        feature_columns = json.load(f)
    logger.info("Loaded model artifacts from %s/", d)
    return model, scaler, feature_columns


# ---------------------------------------------------------------------------
# Load accounts
# ---------------------------------------------------------------------------

# Maps DB column names to the feature names expected by the model.
# Only columns that need renaming are listed here; the rest match directly.
DB_TO_FEATURE = {
    "events_per_month_trend": "events_per_month_trend",  # needs encoding
}

TREND_MAP = {"Declining": -1, "Stable": 0, "Increasing": 1}

PLAN_TYPES = {"Starter", "Pro", "Enterprise"}
TIERS = {"Basic", "Premium", "Free"}


def load_accounts_csv(csv_path: str) -> pd.DataFrame:
    """Load accounts from a CSV export."""
    logger.info("Loading accounts from CSV: %s", csv_path)
    df = pd.read_csv(csv_path)
    logger.info("Loaded %d accounts", len(df))
    return df


def load_accounts_api(api_url: str) -> pd.DataFrame:
    """Load accounts from the webapp REST API."""
    import requests

    logger.info("Fetching accounts from API: %s", api_url)
    resp = requests.get(api_url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Handle both {"accounts": [...]} and plain [...]
    if isinstance(data, dict):
        records = data.get("accounts") or data.get("data") or data.get("results", [])
    else:
        records = data

    df = pd.DataFrame(records)
    logger.info("Fetched %d accounts from API", len(df))
    return df


def prepare_features(df: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    """Transform raw DB columns into the model's expected feature matrix."""
    feat = df.copy()

    # Encode trend if it's a string
    if feat["events_per_month_trend"].dtype == object:
        feat["events_per_month_trend"] = feat["events_per_month_trend"].map(TREND_MAP).fillna(0)

    # Encode boolean columns
    for col in ["has_dedicated_csm"]:
        if feat[col].dtype == object or feat[col].dtype == bool:
            feat[col] = feat[col].astype(int)

    # One-hot encode plan_type (Starter as baseline)
    if "plan_type" in feat.columns:
        feat["plan_Pro"] = (feat["plan_type"] == "Pro").astype(int)
        feat["plan_Enterprise"] = (feat["plan_type"] == "Enterprise").astype(int)

    # One-hot encode platform_tier (Basic as baseline)
    if "platform_tier" in feat.columns:
        feat["tier_Premium"] = (feat["platform_tier"] == "Premium").astype(int)
        feat["tier_Free"] = (feat["platform_tier"] == "Free").astype(int)
    else:
        # If platform_tier not in DB, default to 0
        feat["tier_Premium"] = 0
        feat["tier_Free"] = 0

    # Ensure payment_auto exists
    if "payment_auto" not in feat.columns:
        feat["payment_auto"] = 0

    # Ensure all extra features exist with defaults
    for col in ["senior_citizen", "has_partner", "has_dependents",
                "paperless_billing", "online_security", "online_backup", "streaming_tv"]:
        if col not in feat.columns:
            feat[col] = 0

    # Select and order features
    missing = [c for c in feature_columns if c not in feat.columns]
    if missing:
        logger.error("Missing feature columns in data: %s", missing)
        sys.exit(1)

    X = feat[feature_columns].fillna(0)
    return X


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def assign_tier(prob: float) -> str:
    for tier, (lo, hi) in RISK_TIERS.items():
        if lo <= prob < hi:
            return tier
    return "Critical"


def score_accounts(
    df: pd.DataFrame,
    model,
    scaler,
    feature_columns: list[str],
    compute_drivers: bool = True,
) -> pd.DataFrame:
    """Score all accounts and return DataFrame with predictions."""
    X = prepare_features(df, feature_columns)
    X_scaled = pd.DataFrame(scaler.transform(X), columns=X.columns, index=X.index)

    # Predict probabilities
    probabilities = model.predict_proba(X_scaled)[:, 1]
    tiers = [assign_tier(p) for p in probabilities]

    # SHAP drivers
    drivers_json = [None] * len(df)
    if compute_drivers:
        try:
            import shap
            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(X_scaled)
            for i in range(len(X_scaled)):
                row_shap = shap_values[i]
                top_idx = np.argsort(np.abs(row_shap))[-3:][::-1]
                drivers = [
                    {"driver": X_scaled.columns[idx], "impact": round(float(row_shap[idx]), 4)}
                    for idx in top_idx
                ]
                drivers_json[i] = json.dumps(drivers)
        except ImportError:
            logger.warning("shap not installed, skipping driver computation")

    # Build result
    result = df.copy()
    result["churn_probability"] = probabilities.round(4)
    result["risk_tier"] = tiers
    result["churn_drivers"] = drivers_json
    result["last_scored_at"] = datetime.now(timezone.utc).isoformat()

    logger.info("Scored %d accounts", len(result))
    tier_counts = pd.Series(tiers).value_counts()
    for tier in ["Low", "Medium", "High", "Critical"]:
        count = tier_counts.get(tier, 0)
        logger.info("  %s: %d (%.1f%%)", tier, count, count / len(tiers) * 100)

    return result


# ---------------------------------------------------------------------------
# Write back
# ---------------------------------------------------------------------------


def writeback_api(api_url: str, scored_df: pd.DataFrame) -> dict:
    """Write scored results back to DB via batch API."""
    import requests

    records = []
    for _, row in scored_df.iterrows():
        records.append({
            "id": row.get("id"),
            "churn_probability": row["churn_probability"],
            "risk_tier": row["risk_tier"],
            "churn_drivers": row["churn_drivers"],
            "last_scored_at": row["last_scored_at"],
        })

    logger.info("Writing %d scored records to API: %s", len(records), api_url)
    resp = requests.patch(
        api_url,
        json={"accounts": records},
        timeout=60,
    )
    resp.raise_for_status()

    result = {
        "status": "success",
        "records_scored": len(records),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("Writeback complete: %d records", len(records))
    return result


def writeback_csv(output_path: str, scored_df: pd.DataFrame) -> dict:
    """Write scored results to CSV."""
    scored_df.to_csv(output_path, index=False)
    result = {
        "status": "success",
        "records_scored": len(scored_df),
        "output_file": output_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("Scored CSV written to %s (%d records)", output_path, len(scored_df))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score NovaSeat accounts for churn risk")
    parser.add_argument("--artifacts", default="model/artifacts",
                        help="Path to model artifacts directory")

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="Path to accounts CSV (local/testing)")
    source.add_argument("--db-url", help="DB API URL to fetch accounts")

    parser.add_argument("--output", help="Output CSV path (if using --csv mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing back")
    parser.add_argument("--no-shap", action="store_true",
                        help="Skip SHAP driver computation (faster)")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load model
    model, scaler, feature_columns = load_artifacts(args.artifacts)

    # Load accounts
    if args.csv:
        df = load_accounts_csv(args.csv)
    else:
        df = load_accounts_api(args.db_url)

    # Score
    scored = score_accounts(df, model, scaler, feature_columns,
                           compute_drivers=not args.no_shap)

    # Output
    if args.dry_run:
        print("\n--- Scored Accounts (first 20) ---")
        cols = ["name", "churn_probability", "risk_tier", "churn_drivers"]
        available = [c for c in cols if c in scored.columns]
        print(scored[available].head(20).to_string(index=False))
        print(f"\nTotal: {len(scored)} accounts scored")
    elif args.csv:
        out_path = args.output or args.csv.replace(".csv", "_scored.csv")
        result = writeback_csv(out_path, scored)
        print(json.dumps(result, indent=2))
    else:
        result = writeback_api(args.db_url, scored)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
