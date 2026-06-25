# Multi-stage build: Node for React, Python for backend
FROM node:18-alpine AS react-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# Python backend with React serving
FROM python:3.11-bullseye

ARG GIT_SHA=local
ENV KUMO_BUILD_SHA=${GIT_SHA}

WORKDIR /app

RUN python -m pip install --upgrade pip setuptools wheel

RUN pip install --no-cache-dir \
    snowflake-connector-python==3.17.4 \
    Flask==3.0.0 \
    Flask-CORS==4.0.0 \
    python-dotenv==1.0.0

# Copy backend files
COPY backend/ /app/backend/

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Copy built React app from previous stage
COPY --from=react-builder /app/frontend/build /app/backend/static

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
