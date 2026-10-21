-- Prepared deployment SQL; not executed automatically.
-- GitHub Actions image: build-218-1-df9704efa34c (readiness-log routing correction).
-- Build: https://github.com/antgoil78/KUMO-Monitor/actions/runs/39609959753
-- Use the same published app image for DEV and TST.
-- Updates the TST service created by create_tst_service.sql.
-- The specification intentionally selects the image by tag without a digest;
-- there is no previous sha256 pin to override a newly selected image tag.
USE ROLE KUMO_ADMIN;

ALTER SERVICE KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE FROM SPECIFICATION $$
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
$$;

SELECT SYSTEM$GET_SERVICE_STATUS('KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE');
SHOW SERVICE CONTAINERS IN SERVICE KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE;
SHOW ENDPOINTS IN SERVICE KUMO_TST.MONITOR_APP.KUMO_MONITOR_SERVICE;
