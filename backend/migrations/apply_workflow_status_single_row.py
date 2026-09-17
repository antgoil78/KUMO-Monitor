"""Keep exactly one WORKFLOW_STATUS row per DBT workflow."""

from snowflake.connector import DictCursor

import snowflake_client as sf


STATUS_TABLE = "KUMO_TST.META.WORKFLOW_STATUS"
PROCEDURES = (
    "KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(VARCHAR)",
    "KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(VARCHAR, VARCHAR)",
)
DBT_COMPLETE = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_DBT_COMPLETE(VARCHAR, BOOLEAN, VARCHAR)"


def _ddl(cur, procedure):
    cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (procedure,))
    return cur.fetchone()["DDL"]


def _qualify(ddl):
    return ddl.replace(
        "CREATE OR REPLACE PROCEDURE ",
        "CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.",
        1,
    )


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            # Preserve the latest requested run (or latest loaded row when history
            # is unavailable) and remove older rows before changing writers.
            cur.execute(
                f"""
                DELETE FROM {STATUS_TABLE} t
                USING (
                  SELECT WORKFLOW_ID, RUN_ID
                  FROM (
                    SELECT
                      s.WORKFLOW_ID,
                      s.RUN_ID,
                      ROW_NUMBER() OVER (
                        PARTITION BY s.WORKFLOW_ID
                        ORDER BY COALESCE(h.REQUESTED_AT, s.LOADED_DTTM) DESC NULLS LAST,
                                 s.RUN_ID DESC
                      ) AS RN
                    FROM {STATUS_TABLE} s
                    LEFT JOIN KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY h
                      ON h.RUN_ID = s.RUN_ID
                  )
                  WHERE RN > 1
                ) old
                WHERE t.WORKFLOW_ID = old.WORKFLOW_ID
                  AND t.RUN_ID = old.RUN_ID
                """
            )

            old_on = """ON t.WORKFLOW_ID = s.WORKFLOW_ID
        AND t.RUN_ID = s.RUN_ID"""
            new_on = "ON t.WORKFLOW_ID = s.WORKFLOW_ID"
            old_update = """WHEN MATCHED THEN UPDATE SET
        STATUS = ''PROCESSING'',
        LOADED_DTTM = NULL"""
            new_update = """WHEN MATCHED THEN UPDATE SET
        STATUS = ''PROCESSING'',
        LOADED_DTTM = NULL,
        RUN_ID = s.RUN_ID"""

            for procedure in PROCEDURES:
                ddl = _ddl(cur, procedure)
                if old_on in ddl:
                    ddl = ddl.replace(old_on, new_on, 1)
                if old_update in ddl:
                    ddl = ddl.replace(old_update, new_update, 1)
                if new_on not in ddl or "RUN_ID = s.RUN_ID" not in ddl:
                    raise RuntimeError(f"Unexpected WORKFLOW_STATUS MERGE in {procedure}")
                cur.execute(_qualify(ddl))

            complete = _ddl(cur, DBT_COMPLETE)
            run_filter = """
     AND RUN_ID = :P_RUN_ID"""
            if run_filter in complete:
                complete = complete.replace(run_filter, "", 1)
            cur.execute(_qualify(complete))

            cur.execute(
                f"""
                SELECT COUNT(*) AS TOTAL_ROWS,
                       COUNT(DISTINCT WORKFLOW_ID) AS WORKFLOW_COUNT
                FROM {STATUS_TABLE}
                """
            )
            return cur.fetchone()
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
