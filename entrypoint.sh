#!/bin/bash
set -e

export PYTHONPATH=/app/backend:/app:${PYTHONPATH}

export FLASK_APP=backend.app
export FLASK_ENV=production
export PYTHONUNBUFFERED=1

if [ "${KUMO_TASK_ENGINE_RUNTIME_ENABLED:-false}" = "true" ]; then
    exec python -m task_engine.runtime
fi

exec python -m gunicorn --bind 0.0.0.0:5000 --workers 1 \
    --worker-class gthread --threads 64 --timeout 120 \
    --graceful-timeout 20 backend.app:app
