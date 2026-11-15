#!/bin/sh
set -e

alembic upgrade head

if [ "${APP_SEED_DEMO:-false}" = "true" ]; then
    python scripts/seed.py
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
