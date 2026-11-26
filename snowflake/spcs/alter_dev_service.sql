-- DEV PostgreSQL orchestration promotion; preserves the existing endpoint and resources.
USE ROLE KUMO_ADMIN;
ALTER SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE FROM SPECIFICATION $$
{
  "spec": {
    "containers": [
      {
        "name": "monitor",
        "image": "/kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-232-1-31bd91225e71",
        "sha256": "@sha256:14691f81c06d3ceb97c4590375d5f6adde54d32a77378d566198a81299eb1459",
        "env": {
          "KUMO_ENVIRONMENT_TABLE": "KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS",
          "KUMO_ENV": "DEV",
          "KUMO_DEPLOYMENT_ENV": "DEV",
          "SNOWFLAKE_WAREHOUSE": "KUMO_ELT_GEN_1",
          "KUMO_TASK_WAREHOUSE": "KUMO_ELT_GEN_1",
          "KUMO_LIM_ROLE": "KUMO_ADMIN",
          "KUMO_AUTO_RESUME_TASKS": "false",
          "SNOWFLAKE_STATEMENT_TIMEOUT_SECONDS": "7200",
          "SNOWFLAKE_NETWORK_TIMEOUT_SECONDS": "7200",
          "KUMO_ORCHESTRATION_RUNS_ENABLED": "true",
          "KUMO_TASK_PROTOTYPE_ENABLED": "true",
          "KUMO_POSTGRES_ENGINE_PRIMARY": "true",
          "KUMO_TASK_ENGINE_RUNTIME_ENABLED": "true",
          "KUMO_PG_BUSINESS_JOBS_ENABLED": "true",
          "KUMO_TASK_ENGINE_WORKER_ENABLED": "true",
          "KUMO_POSTGRES_HOST": "kumo-monitor-postgres-service.gvdf.svc.spcs.internal",
          "KUMO_POSTGRES_DATABASE": "kumo_engine_dev",
          "KUMO_PG_TASK_ROLE": "KUMO_ADMIN",
          "KUMO_PG_TASK_WAREHOUSE": "KUMO_ELT_GEN_1"
        },
        "readinessProbe": {
          "port": 5000,
          "path": "/api/orchestration/task-engine/ready"
        },
        "resources": {
          "limits": {
            "memory": "6Gi",
            "cpu": "1"
          },
          "requests": {
            "memory": "0.5Gi",
            "cpu": "0.25"
          }
        },
        "secrets": [
          {
            "snowflakeSecret": "KUMO_DEV.MONITOR_APP.KUMO_MONITOR_POSTGRES_CREDENTIALS",
            "secretKeyRef": "username",
            "envVarName": "KUMO_POSTGRES_USER"
          },
          {
            "snowflakeSecret": "KUMO_DEV.MONITOR_APP.KUMO_MONITOR_POSTGRES_CREDENTIALS",
            "secretKeyRef": "password",
            "envVarName": "KUMO_POSTGRES_PASSWORD"
          }
        ]
      }
    ],
    "endpoints": [
      {
        "name": "kumo-monitor-dev-ui",
        "port": 5000,
        "public": true
      }
    ]
  },
  "capabilities": {
    "securityContext": {
      "executeAsCaller": true
    }
  }
}
$$;
