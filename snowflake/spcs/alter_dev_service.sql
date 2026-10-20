-- DEV app release applied on 20 October 2026.
-- Image tag and digest select the same published build; other service settings are preserved.
USE ROLE KUMO_ADMIN;

ALTER SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE FROM SPECIFICATION $$
---
spec:
  containers:
  - name: "monitor"
    image: "vymkbmw-ov24823.registry.snowflakecomputing.com/kumo_shared/monitor_app/monitor_repository/kumo-monitor:dev-readiness-20261020-134830-1bb36d43"
    sha256: "@sha256:f65286b5397d8b1f1bf553fc476eafc90a1ed39379ac47e1f985a3ffc7e50d98"
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

SELECT SYSTEM$GET_SERVICE_STATUS('KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE');
SHOW ENDPOINTS IN SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE;
