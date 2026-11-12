-- Source: KUMO_DEV.MONITOR_APP.TASK_ORCH_MAINTENANCE
-- Static engine housekeeping; generated workflow run/scheduler tasks are excluded.
CREATE OR REPLACE TASK KUMO_DEV.MONITOR_APP.TASK_ORCH_MAINTENANCE
    WAREHOUSE = KUMO_ELT_GEN_1
    SCHEDULE = '1 MINUTE'
    USER_TASK_TIMEOUT_MS = 55000
    TASK_AUTO_RETRY_ATTEMPTS = 0
    COMMENT = 'KUMO parallel orchestration v2: recover interrupted runs and remove completed temporary workflow tasks'
AS CALL KUMO_DEV.MONITOR_APP.SP_ORCH_RECONCILE(NULL);
