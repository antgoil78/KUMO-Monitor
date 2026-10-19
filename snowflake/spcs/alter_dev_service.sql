-- Prepared update SQL; not executed automatically.
-- Run after create_dev_service.sql has created the DEV service in the shared pool.
-- Replace the image tag with the published version you want to deploy.
-- Updating the specification restarts DEV containers when their image/config changes.

USE ROLE KUMO_ADMIN;

ALTER SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE
  FROM SPECIFICATION $$
spec:
  containers:
    - name: monitor
      image: /kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-215-1-022ffedd2bf2
      env:
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
$$;

ALTER SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE
  SET QUERY_WAREHOUSE = KUMO_ELT_GEN_1;

SELECT SYSTEM$GET_SERVICE_STATUS('KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE');
SHOW ENDPOINTS IN SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE;
