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

for path in sorted(Path("/app/backend").rglob("*.py")):
    data = path.read_bytes()

    if b"\x00" in data:
        print(f"NULL BYTE FOUND: {path}")
        print(f"Null byte count: {data.count(b'\\x00')}")
        print(f"First null byte at position: {data.index(b'\\x00')}")
        failed = True
        continue

    try:
        py_compile.compile(str(path), doraise=True)
        print(f"OK: {path}")
    except Exception:
        print(f"FAILED: {path}")
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
