#!/usr/bin/env python3
"""Sync workflow JSON files from a local folder into an n8n instance.

This script performs an upsert by workflow name:
- If a workflow name does not exist in n8n, it is created.
- If a workflow name already exists, it is updated.

Authentication uses n8n API key via the X-N8N-API-KEY header.

Credential strategy:
- postgres nodes:
  1) Use N8N_POSTGRES_CREDENTIAL_ID when provided.
  2) Else resolve by credential name in n8n.
  3) Else inject inline postgres connection from .env (POSTGRES_* / DB_* / N8N_POSTGRES_*).
- emailSend nodes:
  1) Use N8N_SMTP_CREDENTIAL_ID when provided.
  2) Else resolve by credential name in n8n.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_dotenv_values(dotenv_path: Path) -> Dict[str, str]:
    """Parse a .env file into a key/value map.

    The parser intentionally supports the common subset used in local env files:
    - blank lines and comment lines are ignored
    - optional "export " prefix is supported
    - values can be unquoted or wrapped in single/double quotes
    """

    if not dotenv_path.exists() or not dotenv_path.is_file():
        return {}

    values: Dict[str, str] = {}

    with dotenv_path.open("r", encoding="utf-8") as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[len("export ") :].strip()

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if not key:
                continue

            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]

            values[key] = value

    return values


def resolve_setting(dotenv_values: Dict[str, str], key: str, fallback: Optional[str] = None) -> Optional[str]:
    """Resolve a setting preferring .env over process environment variables."""

    if key in dotenv_values and dotenv_values[key] != "":
        return dotenv_values[key]

    env_value = os.environ.get(key)
    if env_value is not None and env_value != "":
        return env_value

    return fallback


def first_non_empty(dotenv_values: Dict[str, str], keys: List[str], fallback: Optional[str] = None) -> Optional[str]:
    """Resolve first non-empty value among multiple keys."""

    for key in keys:
        value = resolve_setting(dotenv_values, key)
        if value is not None and value != "":
            return value
    return fallback


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    dotenv_values = load_dotenv_values(repo_root / ".env")

    parser = argparse.ArgumentParser(
        description="Upsert n8n workflows from JSON files in a directory.",
    )
    parser.add_argument(
        "--workflows-dir",
        default=str(repo_root / "workflow-n8n"),
        help="Directory containing workflow JSON files (default: workflow-n8n).",
    )
    parser.add_argument(
        "--base-url",
        default=resolve_setting(dotenv_values, "N8N_BASE_URL", "http://localhost:5678"),
        help="n8n base URL (default: .env N8N_BASE_URL, then env var, then http://localhost:5678).",
    )
    parser.add_argument(
        "--api-key",
        default=resolve_setting(dotenv_values, "N8N_API_KEY"),
        help="n8n API key (default: .env N8N_API_KEY, then env var N8N_API_KEY).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without creating/updating workflows.",
    )
    return parser.parse_args()


class N8nClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        if not api_key:
            raise ValueError("Missing API key. Set N8N_API_KEY or pass --api-key.")

        parsed = urllib.parse.urlsplit(base_url)
        normalized_path = parsed.path.rstrip("/")
        if normalized_path.endswith("/api/v1"):
            api_base = base_url.rstrip("/")
        else:
            base_no_trailing = base_url.rstrip("/")
            api_base = f"{base_no_trailing}/api/v1"

        self.api_base = api_base
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, endpoint: str, payload: Dict[str, Any] | None = None) -> Any:
        url = f"{self.api_base}{endpoint}"
        body = None
        headers = {
            "Accept": "application/json",
            "X-N8N-API-KEY": self.api_key,
        }

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, method=method, data=body, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HTTP {exc.code} calling {method} {url}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach n8n at {url}: {exc}") from exc

    def list_workflows(self) -> List[Dict[str, Any]]:
        response = self._request("GET", "/workflows")
        if isinstance(response, list):
            return response
        if isinstance(response, dict) and isinstance(response.get("data"), list):
            return response["data"]
        raise RuntimeError("Unexpected response shape from GET /workflows")

    def create_workflow(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self._request("POST", "/workflows", payload)
        if not isinstance(response, dict):
            raise RuntimeError("Unexpected response shape from POST /workflows")
        return response

    def update_workflow(self, workflow_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self._request("PUT", f"/workflows/{workflow_id}", payload)
        if not isinstance(response, dict):
            raise RuntimeError(f"Unexpected response shape from PUT /workflows/{workflow_id}")
        return response

    def list_credentials(self) -> List[Dict[str, Any]]:
        response = self._request("GET", "/credentials")
        if isinstance(response, list):
            return response
        if isinstance(response, dict) and isinstance(response.get("data"), list):
            return response["data"]
        raise RuntimeError("Unexpected response shape from GET /credentials")


def load_workflow_files(workflows_dir: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    if not workflows_dir.exists() or not workflows_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {workflows_dir}")

    files = sorted(workflows_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"No JSON files found in: {workflows_dir}")

    loaded: List[Tuple[Path, Dict[str, Any]]] = []
    for path in files:
        with path.open("r", encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        if not isinstance(data, dict):
            raise ValueError(f"Workflow file is not a JSON object: {path}")
        if not data.get("name"):
            raise ValueError(f"Workflow file has no 'name': {path}")
        loaded.append((path, data))

    return loaded


def build_credential_config(dotenv_values: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """Build credential references for supported node types from .env/env vars."""

    return {
        "postgres": {
            "id": resolve_setting(dotenv_values, "N8N_POSTGRES_CREDENTIAL_ID", ""),
            "name": resolve_setting(
                dotenv_values,
                "N8N_POSTGRES_CREDENTIAL_NAME",
                "NovaSeat PostgreSQL",
            )
            or "NovaSeat PostgreSQL",
        },
        "smtp": {
            "id": resolve_setting(dotenv_values, "N8N_SMTP_CREDENTIAL_ID", ""),
            "name": resolve_setting(dotenv_values, "N8N_SMTP_CREDENTIAL_NAME", "NovaSeat SMTP")
            or "NovaSeat SMTP",
        },
        "googleApi": {
            "id": resolve_setting(dotenv_values, "N8N_GOOGLE_CREDENTIAL_ID", ""),
            "name": resolve_setting(
                dotenv_values,
                "N8N_GOOGLE_CREDENTIAL_NAME",
                "NovaSeat Google Service Account",
            )
            or "NovaSeat Google Service Account",
        },
    }


def enrich_credential_ids_from_n8n(
    client: N8nClient,
    credential_config: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, str]]:
    """Fill missing credential IDs by looking up credentials in n8n by name/type."""

    resolved = copy.deepcopy(credential_config)
    credentials = client.list_credentials()

    lookup_rules = {
        "postgres": {
            "id_key": "id",
            "name_key": "name",
            "type_hint": "postgres",
        },
        "smtp": {
            "id_key": "id",
            "name_key": "name",
            "type_hint": "smtp",
        },
        "googleApi": {
            "id_key": "id",
            "name_key": "name",
            "type_hint": "googleapi",
        },
    }

    for kind, rule in lookup_rules.items():
        conf = resolved.get(kind, {})
        if conf.get(rule["id_key"]):
            continue

        target_name = str(conf.get(rule["name_key"], "")).strip()
        if not target_name:
            continue

        matches: List[Dict[str, Any]] = []
        for cred in credentials:
            name = str(cred.get("name", "")).strip()
            cred_type = str(cred.get("type", "")).lower()
            if name == target_name and rule["type_hint"] in cred_type:
                matches.append(cred)

        if matches:
            resolved[kind]["id"] = str(matches[0].get("id", ""))

    return resolved


def build_postgres_inline_credentials(dotenv_values: Dict[str, str]) -> Dict[str, Any] | None:
    """Build inline postgres credentials from .env when no n8n credential ID is provided."""

    host = first_non_empty(
        dotenv_values,
        ["N8N_POSTGRES_HOST", "POSTGRES_HOST", "DB_HOST", "POSTGRES_CONTAINER_NAME"],
        "localhost",
    )
    port_raw = first_non_empty(dotenv_values, ["N8N_POSTGRES_PORT", "POSTGRES_PORT", "DB_PORT"], "5432")
    database = first_non_empty(dotenv_values, ["N8N_POSTGRES_DB", "POSTGRES_DB", "DB_NAME"])
    user = first_non_empty(dotenv_values, ["N8N_POSTGRES_USER", "POSTGRES_USER", "DB_USER"])
    password = first_non_empty(dotenv_values, ["N8N_POSTGRES_PASSWORD", "POSTGRES_PASSWORD", "DB_PASSWORD"])
    ssl_raw = first_non_empty(dotenv_values, ["N8N_POSTGRES_SSL", "POSTGRES_SSL", "DB_SSL"], "false")

    if not database or not user or not password:
        return None

    try:
        port = int(str(port_raw))
    except (TypeError, ValueError):
        port = 5432

    ssl = str(ssl_raw).strip().lower() in {"1", "true", "yes", "on"}

    return {
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "password": password,
        "ssl": ssl,
    }


def inject_credentials(
    workflow: Dict[str, Any],
    credential_config: Dict[str, Dict[str, str]],
    dotenv_values: Dict[str, str],
) -> Dict[str, Any]:
    """Inject credentials by node type so workflow JSON can stay secrets-free."""

    updated_workflow = copy.deepcopy(workflow)
    nodes = updated_workflow.get("nodes", [])
    postgres_inline = build_postgres_inline_credentials(dotenv_values)
    warned_missing_smtp = False
    warned_missing_google = False

    for node in nodes:
        node_type = node.get("type")

        if node_type == "n8n-nodes-base.postgres":
            postgres = credential_config.get("postgres", {})
            if postgres.get("id"):
                node["credentials"] = {
                    "postgres": {
                        "id": postgres["id"],
                        "name": postgres["name"],
                    }
                }
            elif postgres_inline is not None:
                # Fallback for local/dev setups: inject raw postgres connection fields from .env.
                node["credentials"] = {
                    "postgres": {
                        "name": postgres.get("name", "NovaSeat PostgreSQL"),
                        **postgres_inline,
                    }
                }
            else:
                raise ValueError(
                    "Cannot resolve postgres credentials for postgres nodes. "
                    "Provide N8N_POSTGRES_CREDENTIAL_ID, or create a postgres credential in n8n, "
                    "or set POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD (and optional host/port) in .env."
                )

        if node_type == "n8n-nodes-base.emailSend":
            smtp = credential_config.get("smtp", {})
            if not smtp.get("id"):
                if not warned_missing_smtp:
                    print(
                        "WARNING: SMTP credential not resolved for emailSend nodes. "
                        "Sync will continue, but email nodes will remain without credentials. "
                        "Set N8N_SMTP_CREDENTIAL_ID or create an SMTP credential named "
                        f"'{smtp.get('name', 'NovaSeat SMTP')}' in n8n to enable email sending.",
                        file=sys.stderr,
                    )
                    warned_missing_smtp = True
                continue
            node["credentials"] = {
                "smtp": {
                    "id": smtp["id"],
                    "name": smtp["name"],
                }
            }

        # httpRequest nodes using Google Service Account (predefinedCredentialType)
        if (
            node_type == "n8n-nodes-base.httpRequest"
            and node.get("parameters", {}).get("nodeCredentialType") == "googleApi"
        ):
            google = credential_config.get("googleApi", {})
            if not google.get("id"):
                if not warned_missing_google:
                    print(
                        "WARNING: Google API credential not resolved for httpRequest nodes. "
                        "Sync will continue, but Google API nodes will remain without credentials. "
                        "Set N8N_GOOGLE_CREDENTIAL_ID or create a Google Service Account credential named "
                        f"'{google.get('name', 'NovaSeat Google Service Account')}' in n8n.",
                        file=sys.stderr,
                    )
                    warned_missing_google = True
                continue
            node["credentials"] = {
                "googleApi": {
                    "id": google["id"],
                    "name": google["name"],
                }
            }

    return updated_workflow


def build_payload(
    workflow: Dict[str, Any],
    credential_config: Dict[str, Dict[str, str]],
    dotenv_values: Dict[str, str],
) -> Dict[str, Any]:
    # Keep payload strict for n8n API schema validation.
    workflow_with_credentials = inject_credentials(workflow, credential_config, dotenv_values)

    payload: Dict[str, Any] = {
        "name": workflow_with_credentials["name"],
        "nodes": workflow_with_credentials.get("nodes", []),
        "connections": workflow_with_credentials.get("connections", {}),
        "settings": workflow_with_credentials.get("settings", {}),
    }
    return payload


def main() -> int:
    args = parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    dotenv_values = load_dotenv_values(repo_root / ".env")

    workflows_dir = Path(args.workflows_dir)

    try:
        files = load_workflow_files(workflows_dir)
        client = N8nClient(base_url=args.base_url, api_key=args.api_key, timeout=args.timeout)
        credential_config = build_credential_config(dotenv_values)
        credential_config = enrich_credential_ids_from_n8n(client, credential_config)

        existing = client.list_workflows()
        existing_by_name = {str(w.get("name")): w for w in existing if w.get("name")}

        created = 0
        updated = 0

        for path, workflow in files:
            name = workflow["name"]
            payload = build_payload(workflow, credential_config, dotenv_values)

            if name in existing_by_name:
                workflow_id = str(existing_by_name[name].get("id"))
                if not workflow_id or workflow_id == "None":
                    raise RuntimeError(f"Existing workflow '{name}' has no id")

                if args.dry_run:
                    print(f"[DRY-RUN] UPDATE {name} ({workflow_id}) from {path}")
                    continue

                client.update_workflow(workflow_id, payload)
                print(f"UPDATED  {name} ({workflow_id}) from {path}")
                updated += 1
            else:
                if args.dry_run:
                    print(f"[DRY-RUN] CREATE {name} from {path}")
                    continue

                created_wf = client.create_workflow(payload)
                created_id = created_wf.get("id", "unknown-id")
                print(f"CREATED  {name} ({created_id}) from {path}")
                created += 1

        if args.dry_run:
            print("\nDry run complete.")
        else:
            print(f"\nSync complete. Created: {created}, Updated: {updated}")

        return 0

    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
