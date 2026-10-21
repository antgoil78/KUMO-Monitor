-- Executed for the DEV SPCS update on 21 October 2026 at the user's request.
-- GitHub Actions run: https://github.com/antgoil78/KUMO-Monitor/actions/runs/39585655144
-- Tag and digest select the same published build.
-- Preserves live settings and sets the environment parameter-table bootstrap.
USE ROLE KUMO_ADMIN;

ALTER SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE FROM SPECIFICATION $$
---
spec:
  containers:
  - name: "monitor"
    image: "vymkbmw-ov24823.registry.snowflakecomputing.com/kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-217-1-0004ec2c3422"
    sha256: "@sha256:9caad95504cedcb918eef1b332f9ae36d78914390769e0ed85f045f0dc787042"
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
