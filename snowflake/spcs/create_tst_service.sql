-- First TST creation executed on 21 October 2026 at the user's request.
-- GitHub Actions image: build-218-1-df9704efa34c (readiness-log routing correction).
-- Build: https://github.com/antgoil78/KUMO-Monitor/actions/runs/39609959753
-- Use the same published app image for DEV and TST.
-- Uses the existing shared infrastructure and KUMO_ADMIN role grants.
-- Confirm KUMO_SHARED_MONITOR_POOL is IDLE/ACTIVE before creating the service.
-- If the service already exists, use alter_tst_service.sql to update it.
USE ROLE KUMO_ADMIN;

CREATE SERVICE IF NOT EXISTS KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE
  IN COMPUTE POOL KUMO_SHARED_MONITOR_POOL
  FROM SPECIFICATION $$
spec:
  containers:
    - name: monitor
      image: /kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-218-1-df9704efa34c
      env:
        KUMO_ENVIRONMENT_TABLE: "KUMO_TST.MONITOR_META.APPLICATION_PARAMETERS"
        KUMO_ENV: "TST"
        KUMO_DEPLOYMENT_ENV: "TEST"
        SNOWFLAKE_WAREHOUSE: "KUMO_ELT_GEN_1"
        KUMO_TASK_WAREHOUSE: "KUMO_ELT_GEN_1"
        KUMO_LIM_ROLE: "KUMO_ADMIN"
        KUMO_AUTO_RESUME_TASKS: "false"
        SNOWFLAKE_STATEMENT_TIMEOUT_SECONDS: "7200"
        SNOWFLAKE_NETWORK_TIMEOUT_SECONDS: "7200"
      readinessProbe:
        port: 5000
        path: /api/health
      resources:
        limits:
          memory: "6Gi"
          cpu: "1"
        requests:
          memory: "0.5Gi"
          cpu: "0.5"
  endpoints:
    - name: kumo-monitor-tst-ui
      port: 5000
      public: true
capabilities:
  securityContext:
    executeAsCaller: true
$$
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1
  QUERY_WAREHOUSE = KUMO_ELT_GEN_1;

SELECT SYSTEM$GET_SERVICE_STATUS('KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE');
SHOW SERVICE CONTAINERS IN SERVICE KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE;
SHOW ENDPOINTS IN SERVICE KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE;
