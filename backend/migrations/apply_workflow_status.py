"""Track DBT workflow execution in KUMO_TST.META.WORKFLOW_STATUS.

The dispatcher records PROCESSING only after a DBT run has passed validation and
is marked RUNNING. There is one row per workflow; each run replaces RUN_ID and
SP_DBT_COMPLETE updates that row to SUCCESS or ERROR and stamps LOADED_DTTM.
"""

from snowflake.connector import DictCursor

import snowflake_client as sf


STATUS_TABLE = "KUMO_TST.META.WORKFLOW_STATUS"
DISPATCHER = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(VARCHAR)"
DBT_COMPLETE = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_DBT_COMPLETE(VARCHAR, BOOLEAN, VARCHAR)"


def _procedure_ddl(cur, procedure):
    cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (procedure,))
    return cur.fetchone()["DDL"]


def _qualify_create(ddl, schema):
    return ddl.replace(
        "CREATE OR REPLACE PROCEDURE ",
        f"CREATE OR REPLACE PROCEDURE {schema}.",
        1,
    )


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute(
                f"""
                CREATE HYBRID TABLE IF NOT EXISTS {STATUS_TABLE} (
                  WORKFLOW_ID VARCHAR(256) NOT NULL,
                  STATUS VARCHAR(256),
                  LOADED_DTTM TIMESTAMP_NTZ,
                  RUN_ID VARCHAR(36) NOT NULL,
                  PRIMARY KEY (WORKFLOW_ID, RUN_ID)
                )
                """
            )

            dispatcher_ddl = _procedure_ddl(cur, DISPATCHER)
            processing_marker = "WORKFLOW_STATUS: PROCESSING"
            if processing_marker not in dispatcher_ddl:
                history_update = """  snowflake.execute({
    sqlText: `
      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
         SET STATUS = ''RUNNING'',
             START_TIME = SYSDATE(),
             DBT_ARGS = ?,
             UPDATED_AT = SYSDATE()
       WHERE RUN_ID = ?
    `,
    binds: [args, runId]
  });"""
                processing_insert = history_update + """

  /* WORKFLOW_STATUS: PROCESSING */
  snowflake.execute({
    sqlText: `
      MERGE INTO KUMO_TST.META.WORKFLOW_STATUS t
      USING (SELECT ? AS WORKFLOW_ID, ? AS RUN_ID) s
         ON t.WORKFLOW_ID = s.WORKFLOW_ID
      WHEN MATCHED THEN UPDATE SET
        STATUS = ''PROCESSING'',
        LOADED_DTTM = NULL,
        RUN_ID = s.RUN_ID
      WHEN NOT MATCHED THEN INSERT
        (WORKFLOW_ID, STATUS, LOADED_DTTM, RUN_ID)
      VALUES
        (s.WORKFLOW_ID, ''PROCESSING'', NULL, s.RUN_ID)
    `,
    binds: [String(P_WORKFLOW_ID), runId]
  });"""
                if history_update not in dispatcher_ddl:
                    raise RuntimeError("Dispatcher DBT RUNNING marker did not match the expected version")
                dispatcher_ddl = _qualify_create(
                    dispatcher_ddl.replace(history_update, processing_insert, 1),
                    "KUMO_ADMIN.WORKFLOW_MANAGER",
                )
                cur.execute(dispatcher_ddl)

            complete_ddl = _procedure_ddl(cur, DBT_COMPLETE)
            completion_marker = "WORKFLOW_STATUS: terminal DBT status v2"
            if completion_marker not in complete_ddl:
                legacy_update = """

  -- WORKFLOW_STATUS: terminal DBT status
  UPDATE KUMO_TST.META.WORKFLOW_STATUS
     SET STATUS = IFF(:P_SUCCESS, ''SUCCESS'', ''FAILED''),
         LOADED_DTTM = SYSDATE()
   WHERE WORKFLOW_ID = (
           SELECT WORKFLOW_ID
             FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
            WHERE RUN_ID = :P_RUN_ID
            LIMIT 1
         );"""
                complete_ddl = complete_ddl.replace(legacy_update, "", 1)
                log_statement = """  INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS (RUN_ID, LOG_LEVEL, MESSAGE)
  VALUES (:P_RUN_ID, IFF(:P_SUCCESS,''INFO'',''ERROR''), ''DBT_COMPLETE: '' || IFF(:P_SUCCESS,''SUCCESS'',''FAILED: '' || COALESCE(:P_ERROR_MSG,''?'')));"""
                completion_update = log_statement + """

  -- WORKFLOW_STATUS: terminal DBT status v2
  UPDATE KUMO_TST.META.WORKFLOW_STATUS
     SET STATUS = IFF(:P_SUCCESS, ''SUCCESS'', ''ERROR''),
         LOADED_DTTM = SYSDATE()
   WHERE WORKFLOW_ID = (
           SELECT WORKFLOW_ID
             FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
            WHERE RUN_ID = :P_RUN_ID
            LIMIT 1
         );"""
                if log_statement not in complete_ddl:
                    raise RuntimeError("DBT completion log marker did not match the expected version")
                complete_ddl = _qualify_create(
                    complete_ddl.replace(log_statement, completion_update, 1),
                    "KUMO_ADMIN.WORKFLOW_MANAGER",
                )
                cur.execute(complete_ddl)

            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
