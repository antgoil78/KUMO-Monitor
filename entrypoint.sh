#!/bin/bash
set -e

export PREFECT_TELEMETRY_ENABLED=false
export PREFECT_API_URL="http://127.0.0.1:4200/api"
export FLASK_APP=backend/app.py
export FLASK_ENV=production

echo "Starting Flask backend server..."
python -m flask run --host=0.0.0.0 --port=5000 &
BACKEND_PID=$!

echo "Flask backend started on port 5000"
echo "React frontend will be served from backend on http://localhost:5000"

wait $BACKEND_PID
