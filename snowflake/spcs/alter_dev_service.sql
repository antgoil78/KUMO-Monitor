-- Executed for the DEV SPCS update on 20 October 2026 at the user's request.
-- GitHub Actions run: https://github.com/antgoil78/KUMO-Monitor/actions/runs/39435105259
-- Image tag and digest select the same published build.
-- All other settings were preserved from the current DEV service specification.
USE ROLE KUMO_ADMIN;

ALTER SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE FROM SPECIFICATION $$
---
spec:
  containers:
  - name: "monitor"
    image: "vymkbmw-ov24823.registry.snowflakecomputing.com/kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-216-1-0ce9611cf217"
    sha256: "@sha256:59145b48dc4dfebcf8c2042f556d0f88199fdbcb200bb7ce3b7f8feb6a42912c"
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
