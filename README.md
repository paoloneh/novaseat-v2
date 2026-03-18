# NovaSeat v2 — Churn Prevention Agent

AI-powered churn prevention for NovaSeat's B2B event platform.

## Environment Setup

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Adjust values in `.env` if needed (ports, credentials, container names).

## Start Local Stack

```bash
docker compose up -d
```

## Stop Local Stack

```bash
docker compose down
```

## Services

- PostgreSQL: `postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@localhost:$POSTGRES_PORT/$POSTGRES_DB`
- pgAdmin: `http://localhost:$PGADMIN_PORT`

## Helpful Commands

```bash
docker compose ps
docker compose logs -f postgres
docker compose logs -f pgadmin
```

## Project Structure

- `db/` — PostgreSQL schema and docs
- `colab/` — model training and scoring notebooks/scripts
- `workflow-n8n/` — n8n workflow exports and docs
- `scripts/sync_n8n_workflows.py` — upsert local workflow JSON files to a running n8n instance via API key
