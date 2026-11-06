-- Executed for the DEV SPCS update on 23 October 2026 at the user's request.
-- GitHub Actions run: https://github.com/antgoil78/KUMO-Monitor/actions/runs/39938368016
-- Tag and digest select the same published build.
-- Preserves live service settings; changes only image tag and digest.
USE ROLE KUMO_ADMIN;

ALTER SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE FROM SPECIFICATION $$
---
spec:
  containers:
  - name: "monitor"
    image: "vymkbmw-ov24823.registry.snowflakecomputing.com/kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-219-1-6248c7d58e11"
    sha256: "@sha256:77eb973a98d9b2fc44d06fe07a560ac24fcbf835795e4e865b2b7d5570fdf3c0"
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
      path: "/api/health"
    resources:
      limits:
        memory: "6Gi"
        cpu: "1"
      requests:
        memory: "0.5Gi"
        cpu: "0.5"
  endpoints:
  - name: "kumo-monitor-dev-ui"
    port: 5000
    public: true
capabilities:
  securityContext:
    executeAsCaller: true
$$;

SHOW SERVICE CONTAINERS IN SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE;
SHOW ENDPOINTS IN SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE;
