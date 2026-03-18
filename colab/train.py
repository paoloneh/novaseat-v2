#!/usr/bin/env python3
"""
NovaSeat Churn Prediction Model — Training Pipeline

Loads the IBM Telco Customer Churn dataset, maps it to the NovaSeat DB schema,
trains an XGBoost classifier, evaluates it, and saves the model artifacts.

Usage:
    python model/train.py                          # default CSV path
    python model/train.py --csv path/to/file.csv   # custom path
    python model/train.py --output model/artifacts  # custom output dir

Output artifacts (saved to --output dir):
    model.joblib          — trained XGBoost model
    scaler.joblib         — fitted StandardScaler
    feature_columns.json  — ordered list of feature names
    training_report.json  — metrics, thresholds, metadata
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
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RISK_TIERS = {
    "Low": (0.0, 0.3),
    "Medium": (0.3, 0.5),
    "High": (0.5, 0.7),
    "Critical": (0.7, 1.0),
}

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

SEED = 42

# ---------------------------------------------------------------------------
# Step 1: Load and map dataset to NovaSeat schema
# ---------------------------------------------------------------------------


def load_and_map(csv_path: str) -> pd.DataFrame:
    """Load Telco CSV, map to NovaSeat DB fields, generate synthetic features."""
    logger.info("Loading dataset from %s", csv_path)
    raw = pd.read_csv(csv_path)
    raw["TotalCharges"] = pd.to_numeric(raw["TotalCharges"], errors="coerce").fillna(0.0)
    logger.info("Loaded %d rows", len(raw))

    rng = np.random.RandomState(SEED)

    df = pd.DataFrame()
    df["customer_id"] = raw["customerID"]

    # --- DB schema fields ---
    df["annual_revenue"] = (raw["MonthlyCharges"] * 12).round(2)
    df["monthly_charges"] = raw["MonthlyCharges"].round(2)
    df["tenure_months"] = raw["tenure"].astype(int)
    df["plan_type"] = raw["Contract"].map(CONTRACT_TO_PLAN)
    df["has_dedicated_csm"] = (raw["TechSupport"] == "Yes").astype(int)
    df["platform_tier"] = raw["InternetService"].map(INTERNET_TO_TIER)
    df["payment_auto"] = raw["PaymentMethod"].apply(
        lambda v: 1 if v in AUTO_PAYMENT_METHODS else 0
    )

    # --- Synthetic behavioral features (correlated with churn) ---
    churned = (raw["Churn"] == "Yes").values

    # days_since_last_login: churners 15-90, active 0-30
    df["days_since_last_login"] = np.where(
        churned,
        rng.randint(15, 91, size=len(raw)),
        rng.randint(0, 31, size=len(raw)),
    )

    # events_per_month_trend: Declining=-1, Stable=0, Increasing=1
    trend_churned = rng.choice([-1, 0, 1], size=len(raw), p=[0.70, 0.20, 0.10])
    trend_active = rng.choice([-1, 0, 1], size=len(raw), p=[0.10, 0.45, 0.45])
    df["events_per_month_trend"] = np.where(churned, trend_churned, trend_active)

    # support_ticket_velocity: churners 1.5-4.0, active 0.5-1.5
    df["support_ticket_velocity"] = np.where(
        churned,
        rng.uniform(1.5, 4.0, size=len(raw)).round(2),
        rng.uniform(0.5, 1.5, size=len(raw)).round(2),
    )

    # --- Additional features from raw data ---
    df["senior_citizen"] = raw["SeniorCitizen"].astype(int)
    df["has_partner"] = (raw["Partner"] == "Yes").astype(int)
    df["has_dependents"] = (raw["Dependents"] == "Yes").astype(int)
    df["paperless_billing"] = (raw["PaperlessBilling"] == "Yes").astype(int)
    df["online_security"] = (raw["OnlineSecurity"] == "Yes").astype(int)
    df["online_backup"] = (raw["OnlineBackup"] == "Yes").astype(int)
    df["streaming_tv"] = (raw["StreamingTV"] == "Yes").astype(int)

    # Target
    df["churned"] = churned.astype(int)

    logger.info("Mapped %d records to NovaSeat schema", len(df))
    return df


# ---------------------------------------------------------------------------
# Step 2: Feature engineering
# ---------------------------------------------------------------------------

# Features used by the model — these match the DB columns that n8n/Colab
# will read when scoring live accounts.
FEATURE_COLUMNS = [
    "tenure_months",
    "monthly_charges",
    "annual_revenue",
    "days_since_last_login",
    "events_per_month_trend",
    "support_ticket_velocity",
    "has_dedicated_csm",
    "payment_auto",
    "senior_citizen",
    "has_partner",
    "has_dependents",
    "paperless_billing",
    "online_security",
    "online_backup",
    "streaming_tv",
    # one-hot: plan_type
    "plan_Pro",
    "plan_Enterprise",
    # one-hot: platform_tier
    "tier_Premium",
    "tier_Free",
]


def engineer_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Build the feature matrix X and target y from mapped data."""
    feat = df.copy()

    # One-hot encode plan_type (Starter as baseline)
    feat["plan_Pro"] = (feat["plan_type"] == "Pro").astype(int)
    feat["plan_Enterprise"] = (feat["plan_type"] == "Enterprise").astype(int)

    # One-hot encode platform_tier (Basic as baseline)
    feat["tier_Premium"] = (feat["platform_tier"] == "Premium").astype(int)
    feat["tier_Free"] = (feat["platform_tier"] == "Free").astype(int)

    X = feat[FEATURE_COLUMNS].copy()
    y = feat["churned"]

    logger.info("Feature matrix: %d samples x %d features", X.shape[0], X.shape[1])
    logger.info("Class balance: %d churned (%.1f%%), %d active (%.1f%%)",
                y.sum(), y.mean() * 100,
                len(y) - y.sum(), (1 - y.mean()) * 100)

    return X, y


# ---------------------------------------------------------------------------
# Step 3: Train and evaluate
# ---------------------------------------------------------------------------


def train_model(
    X: pd.DataFrame, y: pd.Series
) -> tuple[XGBClassifier, StandardScaler, dict]:
    """Train XGBoost with cross-validation, return model, scaler, and metrics."""

    # Scale numeric features
    scaler = StandardScaler()
    X_scaled = pd.DataFrame(scaler.fit_transform(X), columns=X.columns, index=X.index)

    # Train/test split
    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y, test_size=0.2, random_state=SEED, stratify=y
    )
    logger.info("Train: %d, Test: %d", len(X_train), len(X_test))

    # Handle class imbalance via scale_pos_weight
    n_neg = (y_train == 0).sum()
    n_pos = (y_train == 1).sum()
    scale_pos_weight = n_neg / n_pos

    model = XGBClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.1,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        random_state=SEED,
        use_label_encoder=False,
    )

    # Cross-validation
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="roc_auc")
    logger.info("CV AUC-ROC: %.4f (+/- %.4f)", cv_scores.mean(), cv_scores.std())

    # Final training
    model.fit(X_train, y_train)

    # Evaluate on test set
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]

    auc = roc_auc_score(y_test, y_prob)
    accuracy = accuracy_score(y_test, y_pred)
    precision = precision_score(y_test, y_pred)
    recall = recall_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred)

    logger.info("Test AUC: %.4f | Accuracy: %.4f | Precision: %.4f | Recall: %.4f | F1: %.4f",
                auc, accuracy, precision, recall, f1)

    report = classification_report(y_test, y_pred, target_names=["Active", "Churned"])
    logger.info("Classification Report:\n%s", report)

    # Feature importance
    importance = dict(zip(X.columns, model.feature_importances_))
    importance_sorted = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))
    logger.info("Top 5 features: %s",
                {k: round(v, 4) for k, v in list(importance_sorted.items())[:5]})

    metrics = {
        "auc_roc": round(auc, 4),
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "cv_auc_mean": round(cv_scores.mean(), 4),
        "cv_auc_std": round(cv_scores.std(), 4),
        "scale_pos_weight": round(scale_pos_weight, 2),
        "train_size": len(X_train),
        "test_size": len(X_test),
        "feature_importance": {k: round(v, 4) for k, v in importance_sorted.items()},
        "risk_tiers": RISK_TIERS,
    }

    return model, scaler, metrics


# ---------------------------------------------------------------------------
# Step 4: SHAP explainability
# ---------------------------------------------------------------------------


def compute_shap_top_drivers(model, X_scaled: pd.DataFrame, top_n: int = 3) -> list[list[dict]]:
    """Compute per-account top-N churn drivers using SHAP TreeExplainer."""
    import shap

    logger.info("Computing SHAP values for %d accounts (top %d drivers each)...", len(X_scaled), top_n)
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X_scaled)

    all_drivers = []
    for i in range(len(X_scaled)):
        row_shap = shap_values[i]
        # Get indices of top-N absolute SHAP values
        top_idx = np.argsort(np.abs(row_shap))[-top_n:][::-1]
        drivers = [
            {"driver": X_scaled.columns[idx], "impact": round(float(row_shap[idx]), 4)}
            for idx in top_idx
        ]
        all_drivers.append(drivers)

    logger.info("SHAP drivers computed for all accounts")
    return all_drivers


# ---------------------------------------------------------------------------
# Step 5: Save artifacts
# ---------------------------------------------------------------------------


def save_artifacts(
    output_dir: str,
    model: XGBClassifier,
    scaler: StandardScaler,
    metrics: dict,
) -> None:
    """Save model, scaler, feature list, and training report."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    joblib.dump(model, out / "model.joblib")
    joblib.dump(scaler, out / "scaler.joblib")

    with open(out / "feature_columns.json", "w") as f:
        json.dump(FEATURE_COLUMNS, f, indent=2)

    report = {
        **metrics,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "model_type": "XGBClassifier",
        "n_features": len(FEATURE_COLUMNS),
        "feature_columns": FEATURE_COLUMNS,
    }
    report = _to_builtin_json(report)

    with open(out / "training_report.json", "w") as f:
        json.dump(report, f, indent=2)

    logger.info("Artifacts saved to %s/", out)
    for p in sorted(out.iterdir()):
        logger.info("  %s (%s)", p.name, _human_size(p.stat().st_size))


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _to_builtin_json(value):
    """Recursively convert NumPy values into JSON-serializable Python types."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {k: _to_builtin_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_builtin_json(v) for v in value]
    if isinstance(value, tuple):
        return [_to_builtin_json(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Step 6: Generate scored output for DB seeding
# ---------------------------------------------------------------------------


def generate_scored_csv(
    df: pd.DataFrame,
    model: XGBClassifier,
    scaler: StandardScaler,
    output_dir: str,
) -> None:
    """Score all accounts, assign risk tiers and SHAP drivers, export CSV
    ready for DB import."""
    X, _ = engineer_features(df)
    X_scaled = pd.DataFrame(scaler.transform(X), columns=X.columns, index=X.index)

    # Predict
    probabilities = model.predict_proba(X_scaled)[:, 1]

    # Risk tiers
    def assign_tier(prob: float) -> str:
        for tier, (lo, hi) in RISK_TIERS.items():
            if lo <= prob < hi:
                return tier
        return "Critical"

    tiers = [assign_tier(p) for p in probabilities]

    # SHAP drivers
    drivers = compute_shap_top_drivers(model, X_scaled, top_n=3)

    # Build output matching DB accounts table
    out = pd.DataFrame()
    out["name"] = df["customer_id"].apply(lambda cid: f"NovaSeat-{cid}")
    out["email"] = df["customer_id"].apply(lambda cid: f"{cid.lower()}@example.com")
    out["annual_revenue"] = df["annual_revenue"]
    out["plan_type"] = df["plan_type"]
    out["tenure_months"] = df["tenure_months"]
    out["monthly_charges"] = df["monthly_charges"]
    out["has_dedicated_csm"] = df["has_dedicated_csm"].astype(bool)
    out["csm_name"] = None
    out["csm_email"] = None
    out["days_since_last_login"] = df["days_since_last_login"]
    out["events_per_month_trend"] = df["events_per_month_trend"].map(
        {-1: "Declining", 0: "Stable", 1: "Increasing"}
    )
    out["support_ticket_velocity"] = df["support_ticket_velocity"]
    out["churn_probability"] = probabilities.round(4)
    out["risk_tier"] = tiers
    out["churn_drivers"] = [json.dumps(d) for d in drivers]
    out["intervention_status"] = "None"

    out_path = Path(output_dir) / "accounts_seed.csv"
    out.to_csv(out_path, index=False)
    logger.info("Scored CSV exported: %s (%d records)", out_path, len(out))

    # Summary
    tier_counts = pd.Series(tiers).value_counts()
    logger.info("Risk distribution:")
    for tier in ["Low", "Medium", "High", "Critical"]:
        count = tier_counts.get(tier, 0)
        logger.info("  %s: %d (%.1f%%)", tier, count, count / len(tiers) * 100)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train NovaSeat churn prediction model on Telco dataset"
    )
    parser.add_argument(
        "--csv",
        default="data/WA_Fn-UseC_-Telco-Customer-Churn.csv",
        help="Path to Telco churn CSV (default: data/WA_Fn-UseC_-Telco-Customer-Churn.csv)",
    )
    parser.add_argument(
        "--output",
        default="model/artifacts",
        help="Directory for model artifacts (default: model/artifacts)",
    )
    parser.add_argument(
        "--skip-shap",
        action="store_true",
        help="Skip SHAP computation (faster, no churn_drivers in CSV)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # 1. Load and map
    df = load_and_map(args.csv)

    # 2. Feature engineering
    X, y = engineer_features(df)

    # 3. Train and evaluate
    model, scaler, metrics = train_model(X, y)

    # 4. Save artifacts
    save_artifacts(args.output, model, scaler, metrics)

    # 5. Generate scored CSV for DB seeding
    generate_scored_csv(df, model, scaler, args.output)

    logger.info("Training pipeline complete.")


if __name__ == "__main__":
    main()
