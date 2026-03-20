#!/usr/bin/env python3
"""Trigger the churn scoring notebook on Vertex AI Colab Enterprise and poll until completion."""

import json
import os
import sys
import time
from datetime import datetime, timezone

import google.auth
import google.auth.transport.requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration from .env
# ---------------------------------------------------------------------------

ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(ENV_PATH)

PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT_ID"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
RUNTIME_TEMPLATE = os.environ["GOOGLE_CLOUD_NOTEBOOK_RUNTIME_TEMPLATE"]
NOTEBOOK_GCS_URI = os.environ["COLAB_NOTEBOOK_GCS_URI"]
OUTPUT_GCS_PREFIX = os.environ.get("COLAB_OUTPUT_GCS_PREFIX", "")

BASE_URL = f"https://{LOCATION}-aiplatform.googleapis.com/v1"
PARENT = f"projects/{PROJECT_ID}/locations/{LOCATION}"

POLL_INTERVAL = 10  # seconds


def get_access_token():
    """Get an access token using Application Default Credentials (gcloud auth)."""
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def trigger_execution(token: str) -> dict:
    """Create a notebook execution job and return the API response."""
    import urllib.request

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    body = {
        "displayName": f"novaseat-churn-scoring-{now}",
        "gcsNotebookSource": {"uri": NOTEBOOK_GCS_URI},
        "notebookRuntimeTemplateResourceName": (
            f"projects/{PROJECT_ID}/locations/{LOCATION}"
            f"/notebookRuntimeTemplates/{RUNTIME_TEMPLATE}"
        ),
        "gcsOutputUri": f"{OUTPUT_GCS_PREFIX}/{today}" if OUTPUT_GCS_PREFIX else "",
        "serviceAccount": f"novaseat-colab-runner@{PROJECT_ID}.iam.gserviceaccount.com",
    }

    url = f"{BASE_URL}/{PARENT}/notebookExecutionJobs"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"HTTP {e.code}: {error_body}")
        raise


def poll_status(token: str, job_name: str) -> dict:
    """GET the execution job status."""
    import urllib.request

    url = f"{BASE_URL}/{job_name}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def main():
    print(f"Project:  {PROJECT_ID}")
    print(f"Location: {LOCATION}")
    print(f"Notebook: {NOTEBOOK_GCS_URI}")
    print(f"Runtime:  {RUNTIME_TEMPLATE}")
    print()

    # Authenticate
    print("Authenticating via Application Default Credentials...")
    token = get_access_token()
    print("OK\n")

    # Trigger
    print("Triggering notebook execution job...")
    response = trigger_execution(token)
    raw_name = response.get("name", "")

    # The API may return an operation wrapper (jobs/.../operations/...).
    # Strip the /operations/... suffix to get the actual job resource name.
    if "/operations/" in raw_name:
        job_name = raw_name.split("/operations/")[0]
    else:
        job_name = raw_name

    print(f"Job created: {job_name}")
    initial_state = response.get("metadata", {}).get("genericMetadata", {}).get("state", response.get("jobState", "PENDING"))
    print(f"State:       {initial_state}")
    print()

    # Poll
    terminal_states = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
    iteration = 0

    while True:
        iteration += 1
        print(f"[{iteration}] Waiting {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)

        # Refresh token if needed (long-running jobs)
        if iteration % 60 == 0:
            token = get_access_token()

        status = poll_status(token, job_name)
        state = status.get("jobState", "UNKNOWN")
        print(f"[{iteration}] State: {state}")

        if state in terminal_states:
            print()
            if state == "JOB_STATE_SUCCEEDED":
                print("Notebook execution SUCCEEDED")
                output_uri = status.get("gcsOutputUri", "")
                if output_uri:
                    print(f"Output: {output_uri}")
            else:
                print(f"Notebook execution FAILED — state: {state}")
                error = status.get("status", {})
                if error:
                    print(f"Error:  {error.get('message', 'unknown')}")
            return state == "JOB_STATE_SUCCEEDED"

    return False


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
