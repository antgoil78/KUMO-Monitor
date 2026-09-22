"""Register every standard workflow request in the shared active-run lock table."""

from snowflake.connector import DictCursor

import snowflake_client as sf


PROCEDURE = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_REQUEST_RUN(VARCHAR,VARCHAR,VARCHAR,NUMBER,VARCHAR)"
QUALIFIED_NAME = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_REQUEST_RUN"
MARKER = "KUMO_EXTERNAL_RUN_LOCK_TRACKING_V1"


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (PROCEDURE,))
            ddl = cur.fetchone()["DDL"]
            if MARKER in ddl:
                return "already applied"

            needle = "  RETURN V_RUN_ID;"
            if needle not in ddl:
                raise RuntimeError("SP_WORKFLOW_REQUEST_RUN return marker was not found")

            tracking_sql = """  --------------------------------------------------------------------
  -- KUMO_EXTERNAL_RUN_LOCK_TRACKING_V1
  -- Register runs from dependency rules, schedules, and external SQL.
  --------------------------------------------------------------------
  MERGE INTO KUMO_ADMIN.WORKFLOW_MANAGER.APP_WORKFLOW_RUN_LOCKS t
  USING (
    SELECT
      UUID_STRING() AS LOCK_ID,
      :P_WORKFLOW_ID AS WORKFLOW_ID,
      COALESCE(w.WORKFLOW_NAME, :P_WORKFLOW_ID) AS WORKFLOW_NAME,
      :V_RUN_ID AS RUN_ID,
      CURRENT_USER() AS REQUESTED_BY
    FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOWS w
    WHERE w.WORKFLOW_ID = :P_WORKFLOW_ID
  ) s
  ON t.WORKFLOW_ID = s.WORKFLOW_ID
     AND t.RELEASED_AT IS NULL
     AND t.LOCK_EXPIRES_AT >= CURRENT_TIMESTAMP()
  WHEN MATCHED THEN UPDATE SET
    RUN_ID = s.RUN_ID,
    STATUS = ''QUEUED'',
    WORKFLOW_NAME = s.WORKFLOW_NAME,
    REQUESTED_BY = s.REQUESTED_BY,
    REQUESTED_BY_USER = s.REQUESTED_BY,
    LAST_SEEN_AT = CURRENT_TIMESTAMP(),
    LOCK_EXPIRES_AT = DATEADD(''minute'', 360, CURRENT_TIMESTAMP()),
    MESSAGE = ''Queued through SP_WORKFLOW_REQUEST_RUN'',
    UPDATED_AT = CURRENT_TIMESTAMP()
  WHEN NOT MATCHED THEN INSERT (
    LOCK_ID, WORKFLOW_ID, WORKFLOW_NAME, RUN_ID, STATUS,
    REQUESTED_BY, REQUESTED_BY_USER, REQUESTED_BY_ROLE, SESSION_MODE,
    REQUESTED_AT, LAST_SEEN_AT, LOCK_EXPIRES_AT, MESSAGE, UPDATED_AT
  ) VALUES (
    s.LOCK_ID, s.WORKFLOW_ID, s.WORKFLOW_NAME, s.RUN_ID, ''QUEUED'',
    s.REQUESTED_BY, s.REQUESTED_BY, CURRENT_ROLE(), ''SNOWFLAKE_SQL'',
    CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(),
    DATEADD(''minute'', 360, CURRENT_TIMESTAMP()),
    ''Queued through SP_WORKFLOW_REQUEST_RUN'', CURRENT_TIMESTAMP()
  );

  RETURN V_RUN_ID;"""

            patched = ddl.replace(needle, tracking_sql, 1)
            patched = patched.replace(
                'CREATE OR REPLACE PROCEDURE "SP_WORKFLOW_REQUEST_RUN"',
                f'CREATE OR REPLACE PROCEDURE {QUALIFIED_NAME}',
                1,
            )
            cur.execute(patched)
            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
