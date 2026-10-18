#!/bin/sh
# Apply migrations, then start the API.
set -e

alembic upgrade head

if [ "${UVICORN_RELOAD:-false}" = "true" ]; then
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
