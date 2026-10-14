-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.SP_RUN_WORKFLOW(VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.SP_RUN_WORKFLOW("P_WORKFLOW_ID" VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS '
DECLARE
  V_PAYLOAD VARIANT;
  V_RUN_ID STRING;
  V_SQL STRING;

  V_CHAIN_ID STRING;
  V_CHAIN_DEPTH NUMBER;

  V_ON_SUCCESS_RAW VARIANT;
  V_ON_FAIL_RAW VARIANT;

  V_PENDING_CNT NUMBER;
  V_IS_DBT BOOLEAN DEFAULT FALSE;

  V_TASK_NAME STRING;
  V_TASK_DDL STRING;
  V_RUN_ID_ESC STRING;

  V_NEXT_SAFE VARIANT;
  V_CHILD_WF_ID STRING;
  V_EXIST_CNT NUMBER;

  V_MAX_DEPTH NUMBER DEFAULT 20;
  V_CLAIM_TS TIMESTAMP_NTZ;

  RS_CHILDREN RESULTSET;

  E_DISPATCH_NOT_OK EXCEPTION (-20001, ''SP_WORKFLOW_DISPATCH returned ok=false'');
BEGIN
  V_CLAIM_TS := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;

  /*
    Important:
    The master dispatcher may already have claimed the run and moved it from
    QUEUED -> DISPATCHING.

    Therefore, do not create a new run just because no row is currently QUEUED.
  */
  SELECT COUNT(*)
    INTO :V_PENDING_CNT
  FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
  WHERE TRIM(WORKFLOW_ID) = TRIM(:P_WORKFLOW_ID)
    AND UPPER(TRIM(STATUS)) IN (''QUEUED'', ''DISPATCHING'');

  /*
    If there is no queued/dispatching run, this is a direct/manual invocation.
    Create a new run request, then immediately claim one QUEUED row as DISPATCHING.
  */
  IF (V_PENDING_CNT = 0) THEN
    CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_REQUEST_RUN(
      :P_WORKFLOW_ID,
      ''SCHEDULE'',
      NULL,
      NULL,
      NULL
    );
  END IF;

  /*
    Claim exactly one QUEUED row for this workflow when this proc is called directly.

    When called from the master dispatcher, the row should already be DISPATCHING,
    so this update will normally affect zero rows.
  */
  UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
     SET STATUS     = ''DISPATCHING'',
         CLAIMED_AT = COALESCE(CLAIMED_AT, :V_CLAIM_TS),
         CLAIMED_BY = COALESCE(CLAIMED_BY, ''SP_RUN_WORKFLOW'')
   WHERE RUN_ID IN (
     SELECT RUN_ID
     FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
     WHERE TRIM(WORKFLOW_ID) = TRIM(:P_WORKFLOW_ID)
       AND UPPER(TRIM(STATUS)) = ''QUEUED''
     ORDER BY REQUESTED_AT, RUN_ID
     LIMIT 1
   );

  /*
    This procedure now expects SP_WORKFLOW_DISPATCH to be compatible with
    DISPATCHING rows, not only QUEUED rows.
  */
  CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(:P_WORKFLOW_ID);

  SELECT $1::VARIANT
    INTO :V_PAYLOAD
  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

  IF (COALESCE(V_PAYLOAD:ok::BOOLEAN, FALSE)) THEN
    V_RUN_ID       := V_PAYLOAD:run_id::STRING;
    V_SQL          := V_PAYLOAD:exec_sql::STRING;
    V_CHAIN_ID     := V_PAYLOAD:chain_id::STRING;
    V_CHAIN_DEPTH  := V_PAYLOAD:chain_depth::NUMBER;

    IF (V_CHAIN_ID IS NULL OR TRIM(V_CHAIN_ID) = '''') THEN
      V_CHAIN_ID := V_RUN_ID;
    END IF;

    IF (V_CHAIN_DEPTH IS NULL) THEN
      V_CHAIN_DEPTH := 0;
    END IF;

    V_ON_SUCCESS_RAW := V_PAYLOAD:on_success;
    V_ON_FAIL_RAW    := V_PAYLOAD:on_fail;

    INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
      (RUN_ID, LOG_LEVEL, MESSAGE)
    VALUES
      (:V_RUN_ID, ''INFO'', ''SP_RUN: dispatch ok'');

    /*
      The workflow has now been selected for execution.
      Move it out of DISPATCHING so it cannot be picked up again.
    */
    UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
       SET STATUS = ''DISPATCHED''
     WHERE RUN_ID = :V_RUN_ID
       AND UPPER(TRIM(STATUS)) IN (''QUEUED'', ''CLAIMED'', ''STARTING'', ''DISPATCHING'');

    V_IS_DBT := (UPPER(TRIM(:V_SQL)) LIKE ''EXECUTE DBT PROJECT%'');

    V_TASK_NAME :=
      ''KUMO_ADMIN.WORKFLOW_MANAGER.TMP_WF_'' || REPLACE(:V_RUN_ID, ''-'', ''_'');

    V_RUN_ID_ESC := REPLACE(:V_RUN_ID, '''''''', '''''''''''');

    IF (V_IS_DBT) THEN
      V_TASK_DDL :=
        ''CREATE OR REPLACE TASK '' || :V_TASK_NAME ||
        '' WAREHOUSE = KUMO_ELT_GEN_1'' ||
        '' SCHEDULE = ''''USING CRON 0 0 29 2 * UTC'''''' ||
        '' USER_TASK_TIMEOUT_MS = 21600000'' ||
        '' AS DECLARE V_ERR STRING; BEGIN'' ||
        ''   USE DATABASE KUMO_TST; USE SCHEMA RAW_LIM;'' ||
        ''   '' || :V_SQL || '';'' ||
        ''   CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_DBT_COMPLETE('''''' || :V_RUN_ID_ESC || '''''', TRUE);'' ||
        '' EXCEPTION WHEN OTHER THEN'' ||
        ''   V_ERR := SQLERRM;'' ||
        ''   CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_DBT_COMPLETE('''''' || :V_RUN_ID_ESC || '''''', FALSE, :V_ERR);'' ||
        '' END'';
    ELSE
      V_TASK_DDL :=
        ''CREATE OR REPLACE TASK '' || :V_TASK_NAME ||
        '' WAREHOUSE = KUMO_ELT_GEN_1'' ||
        '' SCHEDULE = ''''USING CRON 0 0 29 2 * UTC'''''' ||
        '' USER_TASK_TIMEOUT_MS = 21600000'' ||
        '' AS DECLARE V_ERR STRING; BEGIN'' ||
        ''   USE DATABASE KUMO_TST; USE SCHEMA RAW_LIM;'' ||
        ''   '' || :V_SQL || '';'' ||
        ''   CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_SQL_COMPLETE('''''' || :V_RUN_ID_ESC || '''''', TRUE);'' ||
        '' EXCEPTION WHEN OTHER THEN'' ||
        ''   V_ERR := SQLERRM;'' ||
        ''   CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_SQL_COMPLETE('''''' || :V_RUN_ID_ESC || '''''', FALSE, :V_ERR);'' ||
        '' END'';
    END IF;

    /*
      Create temporary execution task.
    */
    BEGIN
      EXECUTE IMMEDIATE :V_TASK_DDL;

      BEGIN
        MERGE INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_TMP_TASKS t
        USING (
          SELECT
            :V_RUN_ID AS RUN_ID,
            :P_WORKFLOW_ID AS WORKFLOW_ID,
            :V_TASK_NAME AS TASK_FQN,
            SPLIT_PART(:V_TASK_NAME, ''.'', 3) AS TASK_NAME
        ) s
        ON t.TASK_FQN = s.TASK_FQN

        WHEN MATCHED THEN UPDATE SET
            RUN_ID        = s.RUN_ID,
            WORKFLOW_ID   = s.WORKFLOW_ID,
            TASK_NAME     = s.TASK_NAME,
            STATUS        = ''CREATED'',
            CLEANED_AT    = NULL,
            CLEANUP_ERROR = NULL

        WHEN NOT MATCHED THEN INSERT (
            RUN_ID,
            WORKFLOW_ID,
            TASK_FQN,
            TASK_NAME,
            STATUS
        )
        VALUES (
            s.RUN_ID,
            s.WORKFLOW_ID,
            s.TASK_FQN,
            s.TASK_NAME,
            ''CREATED''
        );

      EXCEPTION
        WHEN OTHER THEN
          INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
            (RUN_ID, LOG_LEVEL, MESSAGE)
          VALUES
            (
              :V_RUN_ID,
              ''WARN'',
              ''SP_RUN: temp task registry insert failed. task='' || :V_TASK_NAME ||
              '', err='' || COALESCE(SQLERRM, ''?'')
            );
      END;

    EXCEPTION
      WHEN OTHER THEN
        INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
          (RUN_ID, LOG_LEVEL, MESSAGE)
        VALUES
          (:V_RUN_ID, ''ERROR'', ''SP_RUN: CREATE TASK failed. err='' || COALESCE(SQLERRM, ''?''));

        UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
           SET STATUS = ''FAILED'',
               END_TIME = SYSDATE(),
               ERROR_MESSAGE = ''Task creation failed: '' || COALESCE(SQLERRM, ''?''),
               UPDATED_AT = SYSDATE()
         WHERE RUN_ID = :V_RUN_ID;

        UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
           SET STATUS = ''FAILED''
         WHERE RUN_ID = :V_RUN_ID;

        BEGIN
          CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_SEND_NOTIFICATION(
            :P_WORKFLOW_ID,
            :V_RUN_ID,
            FALSE,
            ''Task creation failed: '' || COALESCE(SQLERRM, ''?'')
          );
        EXCEPTION
          WHEN OTHER THEN NULL;
        END;

        RETURN ''FAIL_CREATE_TASK'';
    END;

    /*
      Resume and fire temporary task.
    */
    BEGIN
      EXECUTE IMMEDIATE ''ALTER TASK '' || :V_TASK_NAME || '' RESUME'';
      EXECUTE IMMEDIATE ''EXECUTE TASK '' || :V_TASK_NAME;

      /*
        The async task has now been fired.
        Completion should later be handled by SP_DBT_COMPLETE or SP_SQL_COMPLETE.
      */
      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
         SET STATUS = ''RUNNING''
       WHERE RUN_ID = :V_RUN_ID
         AND UPPER(TRIM(STATUS)) IN (''DISPATCHED'', ''DISPATCHING'', ''QUEUED'');

    EXCEPTION
      WHEN OTHER THEN
        INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
          (RUN_ID, LOG_LEVEL, MESSAGE)
        VALUES
          (:V_RUN_ID, ''ERROR'', ''SP_RUN: EXECUTE TASK failed. err='' || COALESCE(SQLERRM, ''?''));

        UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
           SET STATUS = ''FAILED'',
               END_TIME = SYSDATE(),
               ERROR_MESSAGE = ''Task execution failed: '' || COALESCE(SQLERRM, ''?''),
               UPDATED_AT = SYSDATE()
         WHERE RUN_ID = :V_RUN_ID;

        UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
           SET STATUS = ''FAILED''
         WHERE RUN_ID = :V_RUN_ID;

        BEGIN
          EXECUTE IMMEDIATE ''DROP TASK IF EXISTS '' || :V_TASK_NAME;
        EXCEPTION
          WHEN OTHER THEN NULL;
        END;

        BEGIN
          CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_SEND_NOTIFICATION(
            :P_WORKFLOW_ID,
            :V_RUN_ID,
            FALSE,
            ''Task execution failed: '' || COALESCE(SQLERRM, ''?'')
          );
        EXCEPTION
          WHEN OTHER THEN NULL;
        END;

        RETURN ''FAIL_EXECUTE_TASK'';
    END;

    INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
      (RUN_ID, LOG_LEVEL, MESSAGE)
    VALUES
      (:V_RUN_ID, ''INFO'', ''SP_RUN: fired via '' || :V_TASK_NAME);

    RETURN ''OK_ASYNC'';

  ELSE
    /*
      Do not silently return OK here.
      If the master dispatcher already moved a row to DISPATCHING, silently returning
      would let the master think dispatch succeeded.
    */
    RAISE E_DISPATCH_NOT_OK;
  END IF;

EXCEPTION
  WHEN OTHER THEN
    IF (V_RUN_ID IS NOT NULL) THEN
      INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
        (RUN_ID, LOG_LEVEL, MESSAGE)
      VALUES
        (:V_RUN_ID, ''ERROR'', ''SP_RUN: ex='' || COALESCE(SQLERRM, ''?''));

      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
         SET STATUS = ''FAILED'',
             END_TIME = SYSDATE(),
             ERROR_MESSAGE = COALESCE(SQLERRM, ''FAIL''),
             UPDATED_AT = SYSDATE()
       WHERE RUN_ID = :V_RUN_ID;

      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
         SET STATUS = ''FAILED''
       WHERE RUN_ID = :V_RUN_ID;
    END IF;

    RAISE;
END;
';
