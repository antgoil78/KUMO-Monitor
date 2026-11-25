-- DEV PostgreSQL service; credential provisioned separately. Never recreate persistent storage.
USE ROLE KUMO_ADMIN;
CREATE SERVICE KUMO_DEV.MONITOR_APP.KUMO_MONITOR_POSTGRES_SERVICE IN COMPUTE POOL KUMO_SHARED_MONITOR_POOL FROM SPECIFICATION $$
spec:
  containers:
    - name: postgres
      image: /kumo_shared/monitor_app/monitor_repository/kumo-monitor-postgres:pg17-1-1-7034518aaa10
      env:
        POSTGRES_DB: kumo_engine_dev
        PGDATA: /var/lib/postgresql/data/pgdata
        POSTGRES_INITDB_ARGS: "--auth-host=scram-sha-256"
      secrets:
        - snowflakeSecret: KUMO_DEV.MONITOR_APP.KUMO_MONITOR_POSTGRES_CREDENTIALS
          secretKeyRef: username
          envVarName: POSTGRES_USER
        - snowflakeSecret: KUMO_DEV.MONITOR_APP.KUMO_MONITOR_POSTGRES_CREDENTIALS
          secretKeyRef: password
          envVarName: POSTGRES_PASSWORD
      volumeMounts:
        - name: postgres-data
          mountPath: /var/lib/postgresql/data
      resources:
        requests:
          cpu: "0.1"
          memory: 0.5Gi
        limits:
          cpu: "1"
          memory: 2Gi
  endpoints:
    - name: postgres
      port: 5432
      protocol: TCP
      public: false
  volumes:
    - name: postgres-data
      source: block
      size: 10Gi
$$ MIN_INSTANCES=1 MAX_INSTANCES=1;
