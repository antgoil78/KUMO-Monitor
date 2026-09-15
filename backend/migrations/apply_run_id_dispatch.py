"""Make workflow dispatch preserve the run id claimed by the master task."""

import snowflake_client as sf


SCHEMA = "KUMO_ADMIN.WORKFLOW_MANAGER"
RUN_PROC = f"{SCHEMA}.SP_RUN_WORKFLOW(VARCHAR)"
DISPATCH_PROC = f"{SCHEMA}.SP_WORKFLOW_DISPATCH(VARCHAR)"
MASTER_TASK = f"{SCHEMA}.TASK_WF_MASTER_DISPATCHER"


def _ddl(cur, object_type, name):
    cur.execute("SELECT GET_DDL(%s, %s)", (object_type, name))
    return cur.fetchone()[0]


def _replace_once(value, old, new):
    if value.count(old) != 1:
        raise RuntimeError(f"Expected exactly one occurrence of {old!r}, found {value.count(old)}")
    return value.replace(old, new, 1)


def _replace_nth(value, old, new, occurrence):
    start = -1
    for _ in range(occurrence):
        start = value.find(old, start + 1)
        if start < 0:
            raise RuntimeError(f"Could not find occurrence {occurrence} of {old!r}")
    return value[:start] + new + value[start + len(old):]


def main():
    with sf.connection() as conn:
        cur = conn.cursor()
        try:
            dispatch_ddl = _ddl(cur, "PROCEDURE", DISPATCH_PROC)
            dispatch_ddl = _replace_once(
                dispatch_ddl,
                '"SP_WORKFLOW_DISPATCH"("P_WORKFLOW_ID" VARCHAR)',
                '"SP_WORKFLOW_DISPATCH"("P_WORKFLOW_ID" VARCHAR, "P_RUN_ID" VARCHAR)',
            )
            dispatch_ddl = _replace_once(
                dispatch_ddl,
                "WHERE TRIM(WORKFLOW_ID) = TRIM(?)\n        AND UPPER(TRIM(STATUS))",
                "WHERE TRIM(WORKFLOW_ID) = TRIM(?)\n        AND RUN_ID = ?\n        AND UPPER(TRIM(STATUS))",
            )
            # This exact indentation first occurs on the queue candidate query.
            dispatch_ddl = _replace_nth(
                dispatch_ddl,
                "[P_WORKFLOW_ID]\n  );",
                "[P_WORKFLOW_ID, P_RUN_ID]\n  );",
                1,
            )
            cur.execute(dispatch_ddl)

            run_ddl = _ddl(cur, "PROCEDURE", RUN_PROC)
            run_ddl = _replace_once(
                run_ddl,
                '"SP_RUN_WORKFLOW"("P_WORKFLOW_ID" VARCHAR)',
                '"SP_RUN_WORKFLOW"("P_WORKFLOW_ID" VARCHAR, "P_RUN_ID" VARCHAR)',
            )
            # The two-argument entry point is only used for an existing queue row.
            run_ddl = _replace_once(
                run_ddl,
                "AND UPPER(TRIM(STATUS)) IN (''QUEUED'', ''DISPATCHING'');",
                "AND RUN_ID = :P_RUN_ID\n    AND UPPER(TRIM(STATUS)) IN (''QUEUED'', ''DISPATCHING'');",
            )
            run_ddl = _replace_once(
                run_ddl,
                "WHERE TRIM(WORKFLOW_ID) = TRIM(:P_WORKFLOW_ID)\n       AND UPPER(TRIM(STATUS)) = ''QUEUED''",
                "WHERE TRIM(WORKFLOW_ID) = TRIM(:P_WORKFLOW_ID)\n       AND RUN_ID = :P_RUN_ID\n       AND UPPER(TRIM(STATUS)) = ''QUEUED''",
            )
            run_ddl = _replace_once(
                run_ddl,
                "CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(:P_WORKFLOW_ID);",
                "CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(:P_WORKFLOW_ID, :P_RUN_ID);",
            )
            cur.execute(run_ddl)

            task_ddl = _ddl(cur, "TASK", MASTER_TASK)
            old_call = "CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_RUN_WORKFLOW(:V_WORKFLOW_ID);"
            new_call = "CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_RUN_WORKFLOW(:V_WORKFLOW_ID, :V_RUN_ID);"
            if old_call in task_ddl:
                task_ddl = _replace_once(task_ddl, old_call, new_call)
            elif new_call not in task_ddl:
                raise RuntimeError("Master task has an unexpected SP_RUN_WORKFLOW call")
            if "V_ERROR       STRING;" not in task_ddl:
                task_ddl = _replace_once(
                    task_ddl,
                    "V_RUN_ID      STRING;",
                    "V_RUN_ID      STRING;\n  V_ERROR       STRING;",
                )
            if "V_ERROR := SQLERRM;" not in task_ddl:
                task_ddl = _replace_once(
                    task_ddl,
                    "WHEN OTHER THEN\n        /*",
                    "WHEN OTHER THEN\n        V_ERROR := SQLERRM;\n\n        /*",
                )
            task_ddl = task_ddl.replace("COALESCE(SQLERRM, '?')", "COALESCE(:V_ERROR, '?')")
            cur.execute(task_ddl)
            cur.execute(f"ALTER TASK {MASTER_TASK} RESUME")
        finally:
            cur.close()


if __name__ == "__main__":
    main()
