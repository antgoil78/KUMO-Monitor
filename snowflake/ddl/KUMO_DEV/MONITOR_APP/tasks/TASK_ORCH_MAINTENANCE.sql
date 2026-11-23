-- Compatibility task retained for explicit operator use only. Never RESUME.
-- Automatic dispatch/recovery now follows ORCH_DISPATCH_EVENTS_STREAM.
CREATE OR REPLACE TASK KUMO_DEV.MONITOR_APP.TASK_ORCH_MAINTENANCE
    WAREHOUSE = KUMO_ELT_GEN_1
    USER_TASK_TIMEOUT_MS = 55000
    TASK_AUTO_RETRY_ATTEMPTS = 0
    COMMENT = 'KUMO orchestration: manual recovery only; no schedule'
AS CALL KUMO_DEV.MONITOR_APP.SP_ORCH_RECONCILE(NULL);
