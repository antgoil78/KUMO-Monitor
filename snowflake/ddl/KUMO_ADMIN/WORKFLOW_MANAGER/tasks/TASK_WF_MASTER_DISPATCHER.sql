-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.TASK_WF_MASTER_DISPATCHER
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

create or replace task KUMO_ADMIN.WORKFLOW_MANAGER.TASK_WF_MASTER_DISPATCHER
	warehouse=KUMO_ELT_GEN_1
	when SYSTEM$STREAM_HAS_DATA('KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_EXECUTION_QUEUE_STREAM')
	as DECLARE
  V_CLAIM_TS    TIMESTAMP_NTZ;
  V_CLAIM_USER  STRING;
  V_WORKFLOW_ID STRING;
  V_RUN_ID      STRING;
  V_ERROR       STRING;
BEGIN
  V_CLAIM_TS   := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
  V_CLAIM_USER := CURRENT_USER();

  /*
    Consume the stream into a temp table.

    Keep all stream rows for diagnostics/filtering, but only use QUEUED insert
    rows as dispatch candidates.

    This CTAS advances the stream offset.
  */
  CREATE OR REPLACE TEMPORARY TABLE TMP_STREAM_CONSUMED AS
  SELECT
      RUN_ID,
      WORKFLOW_ID,
      STATUS,
      METADATA$ACTION   AS STREAM_ACTION,
      METADATA$ISUPDATE AS STREAM_ISUPDATE
  FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_EXECUTION_QUEUE_STREAM;

  /*
    Candidate rows are rows whose new state is QUEUED.

    This includes:
    - fresh inserts into the queue
    - updates/requeues where the new version is QUEUED
  */
  CREATE OR REPLACE TEMPORARY TABLE TMP_QUEUE_CANDIDATES AS
  SELECT DISTINCT RUN_ID
  FROM TMP_STREAM_CONSUMED
  WHERE STREAM_ACTION = 'INSERT'
    AND UPPER(TRIM(STATUS)) = 'QUEUED'
    AND RUN_ID IS NOT NULL;

  /*
    Claim only candidate queue rows.
  */
  UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE Q
     SET STATUS     = 'DISPATCHING',
         CLAIMED_AT = :V_CLAIM_TS,
         CLAIMED_BY = :V_CLAIM_USER
   WHERE UPPER(TRIM(Q.STATUS)) = 'QUEUED'
     AND EXISTS (
       SELECT 1
       FROM TMP_QUEUE_CANDIDATES S
       WHERE S.RUN_ID = Q.RUN_ID
     );

  /*
    Process only rows claimed by this task invocation.
  */
  LET RS RESULTSET := (
    SELECT WORKFLOW_ID, RUN_ID
    FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
    WHERE UPPER(TRIM(STATUS)) = 'DISPATCHING'
      AND CLAIMED_AT = :V_CLAIM_TS
      AND CLAIMED_BY = :V_CLAIM_USER
    ORDER BY REQUESTED_AT, RUN_ID
    LIMIT 10
  );

  FOR REC IN RS DO
    V_WORKFLOW_ID := REC.WORKFLOW_ID;
    V_RUN_ID      := REC.RUN_ID;

    INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
      (RUN_ID, LOG_LEVEL, MESSAGE)
    VALUES
      (:V_RUN_ID, 'INFO', 'MASTER: calling SP_RUN_WORKFLOW for ' || :V_WORKFLOW_ID);

    BEGIN
      CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_RUN_WORKFLOW(:V_WORKFLOW_ID, :V_RUN_ID);

      /*
        Do not set STATUS = DISPATCHED here.

        SP_RUN_WORKFLOW should move:
          DISPATCHING -> DISPATCHED -> RUNNING

        The master only claims and invokes.
      */
      INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
        (RUN_ID, LOG_LEVEL, MESSAGE)
      VALUES
        (:V_RUN_ID, 'INFO', 'MASTER: SP_RUN_WORKFLOW completed for ' || :V_WORKFLOW_ID);

    EXCEPTION
      WHEN OTHER THEN
        V_ERROR := SQLERRM;

        /*
          Only requeue if the row is still owned by this task invocation and has not
          already moved forward inside SP_RUN_WORKFLOW.
        */
        UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
           SET STATUS = 'QUEUED'
         WHERE RUN_ID = :V_RUN_ID
           AND UPPER(TRIM(STATUS)) = 'DISPATCHING'
           AND CLAIMED_AT = :V_CLAIM_TS
           AND CLAIMED_BY = :V_CLAIM_USER;

        INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
          (RUN_ID, LOG_LEVEL, MESSAGE)
        VALUES
          (
            :V_RUN_ID,
            'ERROR',
            'MASTER: failed ' || :V_WORKFLOW_ID || ' err=' || COALESCE(:V_ERROR, '?')
          );
    END;
  END FOR;
END;
