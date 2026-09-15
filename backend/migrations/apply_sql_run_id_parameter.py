"""Add #[run_id]# substitution to SQL workflow commands."""

from snowflake.connector import DictCursor

import snowflake_client as sf


DISPATCHER = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(VARCHAR)"
MARKER = "SQL_RUN_ID_PARAMETER_V2"


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (DISPATCHER,))
            ddl = cur.fetchone()["DDL"]
            if MARKER in ddl:
                return "already applied"

            original = """const sqlCmdRaw = String(wf.SQL_COMMAND || "");
let sqlCmd = trimTrailingSemicolons(sqlCmdRaw);"""
            parameter_block = """

/* SQL_RUN_ID_PARAMETER_V2: replace every #[run_id]# token after RUN_ID is known. */
const runIdSql = String(runId).replace(/''/g, "''''");
sqlCmd = sqlCmd.split("#[run_id]#").join(runIdSql);"""
            legacy_block = """

/* SQL_RUN_ID_PARAMETER: replace every #[run_id]# token after RUN_ID is known. */
const runIdSql = String(runId).replace(/''/g, "''''");
sqlCmd = sqlCmd.replace(/#[run_id]#/gi, runIdSql);"""
            if legacy_block in ddl:
                ddl = ddl.replace(legacy_block, parameter_block, 1)
            elif original in ddl:
                ddl = ddl.replace(original, original + parameter_block, 1)
            else:
                raise RuntimeError("Dispatcher SQL command marker did not match the expected version")
            ddl = ddl.replace(
                "CREATE OR REPLACE PROCEDURE ",
                "CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.",
                1,
            )
            cur.execute(ddl)
            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
