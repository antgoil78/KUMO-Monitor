-- Source: KUMO_DEV.MONITOR_APP.SP_CLEANUP_OLD_TMP_TASKS(FLOAT)
-- Refresh: python snowflake/export_ddl.py --object PROCEDURE 'KUMO_DEV.MONITOR_APP.SP_CLEANUP_OLD_TMP_TASKS(FLOAT)'
USE DATABASE "KUMO_DEV";
USE SCHEMA "KUMO_DEV"."MONITOR_APP";

CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_CLEANUP_OLD_TMP_TASKS("P_MINUTES_OLD" FLOAT)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
EXECUTE AS OWNER
AS '
var contextRows = snowflake.execute({sqlText: "CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()"});
contextRows.next();
var environment = contextRows.getColumnValue(1);
if (typeof environment === ''string'') environment = JSON.parse(environment);

function exec(sqlText, binds) {
  return snowflake.execute({ sqlText, binds: binds || [] });
}

const taskPrefix = environment.DATABASE + ''.'' + environment.MONITOR_APP_SCHEMA + ''.TMP_WF_'';

function assertSafeTaskFqn(taskFqn) {
  const s = String(taskFqn || "").trim();

  // Expected: DB.SCHEMA.TASK_NAME
  // Allows normal generated Snowflake identifiers only.
  if (!/^[A-Z0-9_]+\\.[A-Z0-9_]+\\.[A-Z0-9_]+$/i.test(s)) {
    throw new Error("Unsafe TASK_FQN: " + s);
  }

  if (!s.toUpperCase().startsWith(taskPrefix)) {
    throw new Error("Task is outside this database and workflow execution namespace");
  }
  return s;
}

const minutesOld = Math.max(1, Math.floor(Number(P_MINUTES_OLD || 60)));

const rows = exec(`
  SELECT
      t.TASK_FQN,
      t.RUN_ID
  FROM ${environment.DATABASE}.${environment.META_SCHEMA}.WORKFLOW_TMP_TASKS t
  JOIN ${environment.DATABASE}.${environment.META_SCHEMA}.WORKFLOW_HISTORY h
    ON h.RUN_ID = t.RUN_ID
  WHERE COALESCE(t.STATUS, '''') <> ''DROPPED''
    AND t.CREATED_AT < DATEADD(''minute'', -1 * CAST(? AS INTEGER), SYSDATE())
    AND UPPER(TRIM(h.STATUS)) IN (''SUCCESS'', ''FAILED'', ''ERROR'', ''CANCELLED'', ''CANCELED'')
`, [minutesOld]);

let cleaned = [];
let failed = [];

while (rows.next()) {
  const taskFqnRaw = rows.getColumnValue(1);
  const runId = rows.getColumnValue(2);

  try {
    const taskFqn = assertSafeTaskFqn(taskFqnRaw);

    try {
      exec(`ALTER TASK IF EXISTS ${taskFqn} SUSPEND`);
    } catch (e1) {
      // Continue. Task may already be gone or not suspendable.
    }

    try {
      exec(`SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(?)`, [taskFqn]);
    } catch (e2) {
      // Continue. There may be no active execution.
    }

    exec(`DROP TASK IF EXISTS ${taskFqn}`);

    exec(`
      UPDATE ${environment.DATABASE}.${environment.META_SCHEMA}.WORKFLOW_TMP_TASKS
      SET STATUS = ''DROPPED'',
          CLEANED_AT = SYSDATE(),
          CLEANUP_ERROR = NULL
      WHERE TASK_FQN = ?
    `, [taskFqnRaw]);

    cleaned.push(taskFqn);

  } catch (e) {
    exec(`
      UPDATE ${environment.DATABASE}.${environment.META_SCHEMA}.WORKFLOW_TMP_TASKS
      SET STATUS = ''CLEANUP_FAILED'',
          CLEANUP_ERROR = ?
      WHERE TASK_FQN = ?
    `, [String(e), taskFqnRaw]);

    failed.push({
      task_fqn: String(taskFqnRaw),
      run_id: String(runId),
      error: String(e)
    });
  }
}

return {
  minutes_old: minutesOld,
  cleaned: cleaned,
  failed: failed
};
';
