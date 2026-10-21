-- Prepared deployment SQL; not executed automatically.
-- Creates only the DEV service. The existing ADMIN service is not referenced.
-- Verified published image: GitHub Actions run 39263506767, 19 October 2026.
-- Choose a different published tag here when deploying another version.

-- First run prepare_dev_service.sql and wait for the shared pool to be IDLE/ACTIVE.
-- KUMO_ADMIN owns the DEV schemas and shared image repository.
USE ROLE KUMO_ADMIN;

CREATE SERVICE IF NOT EXISTS KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE
  IN COMPUTE POOL KUMO_SHARED_MONITOR_POOL
  FROM SPECIFICATION $$
spec:
  containers:
    - name: monitor
      image: /kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-215-1-022ffedd2bf2
      env:
        KUMO_ENVIRONMENT_TABLE: "KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS"
        KUMO_ENV: "DEV"
        KUMO_DEPLOYMENT_ENV: "DEV"
        SNOWFLAKE_WAREHOUSE: "KUMO_ELT_GEN_1"
        KUMO_TASK_WAREHOUSE: "KUMO_ELT_GEN_1"
        KUMO_LIM_ROLE: "KUMO_ADMIN"
        KUMO_AUTO_RESUME_TASKS: "false"
        SNOWFLAKE_STATEMENT_TIMEOUT_SECONDS: "7200"
        SNOWFLAKE_NETWORK_TIMEOUT_SECONDS: "7200"
      readinessProbe:
        port: 5000
        path: /api/health
  endpoints:
    - name: kumo-monitor-dev-ui
      port: 5000
      public: true
capabilities:
  securityContext:
    executeAsCaller: true
$$
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1
  QUERY_WAREHOUSE = KUMO_ELT_GEN_1;

SELECT SYSTEM$GET_SERVICE_STATUS('KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE');
SHOW ENDPOINTS IN SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE;
