"""Add one-run DBT command overrides to the queue and dispatcher."""

from snowflake.connector import DictCursor

import snowflake_client as sf


QUEUE = "KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE"
DISPATCHER = "KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(VARCHAR)"


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute(f"ALTER TABLE {QUEUE} ADD COLUMN IF NOT EXISTS DBT_COMMAND_OVERRIDE VARCHAR")
            cur.execute("SELECT GET_DDL('PROCEDURE', %s) AS DDL", (DISPATCHER,))
            ddl = cur.fetchone()["DDL"]
            if "DBT_COMMAND_OVERRIDE" in ddl:
                return "already applied"
            old_select = """          PARENT_RUN_ID
       FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE"""
            new_select = """          PARENT_RUN_ID,
          DBT_COMMAND_OVERRIDE
       FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE"""
            old_command = '  let argsCore = String(wf.DBT_COMMAND || "").trim();'
            new_command = '  let argsCore = String(claimed.DBT_COMMAND_OVERRIDE || wf.DBT_COMMAND || "").trim();'
            if old_select not in ddl or old_command not in ddl:
                raise RuntimeError("Dispatcher definition did not match the expected version")
            ddl = ddl.replace(old_select, new_select, 1).replace(old_command, new_command, 1)
            cur.execute(ddl)
            return "applied"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
