# Multi-stage build: Node for React, Python for backend
FROM node:18-alpine AS react-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build

# Python backend with React serving
FROM python:3.11-bullseye

WORKDIR /app

RUN python -m pip install --upgrade pip setuptools wheel

RUN pip install --no-cache-dir \
    snowflake-connector-python==3.17.4 \
    Flask==3.0.0 \
    Flask-CORS==4.0.0 \
    python-dotenv==1.0.0

# --- Accept build-time args ---
ARG SNOWFLAKE_USER
ARG SNOWFLAKE_PASSWORD

# --- Set them as environment variables in the container ---
ENV SNOWFLAKE_USER=$SNOWFLAKE_USER
ENV SNOWFLAKE_PASSWORD=$SNOWFLAKE_PASSWORD

# Copy backend files
COPY backend/ /app/backend/
RUN python - <<'PY'
from pathlib import Path
import py_compile
import sys
import traceback

failed = False
NULL = bytes([0])

for path in sorted(Path("/app/backend").rglob("*.py")):
    data = path.read_bytes()

    if NULL in data:
        count = data.count(NULL)
        first_pos = data.index(NULL)

        print("NULL BYTE FOUND: {}".format(path))
        print("Null byte count: {}".format(count))
        print("First null byte at position: {}".format(first_pos))

        failed = True
        continue

    try:
        py_compile.compile(str(path), doraise=True)
        print("OK: {}".format(path))
    except Exception:
        print("FAILED: {}".format(path))
        traceback.print_exc()
        failed = True

if failed:
    sys.exit(1)
PY

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Copy built React app from previous stage
COPY --from=react-builder /app/frontend/build /app/backend/static

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
