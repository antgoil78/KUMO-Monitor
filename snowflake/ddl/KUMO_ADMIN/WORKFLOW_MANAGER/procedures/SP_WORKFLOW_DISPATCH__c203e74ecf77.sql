-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(VARCHAR, VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH("P_WORKFLOW_ID" VARCHAR, "P_RUN_ID" VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
EXECUTE AS OWNER
AS '
function escMsg(s) {
  return (s === null || s === undefined) ? "" : String(s);
}

function log(runId, level, msg) {
  snowflake.execute({
    sqlText: `
      INSERT INTO KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_LOGS
        (RUN_ID, LOG_LEVEL, MESSAGE)
      VALUES (?, ?, ?)
    `,
    binds: [runId, level, msg]
  });
}

function oneRow(sqlText, binds) {
  const rs = snowflake.execute({
    sqlText: sqlText,
    binds: binds || []
  });

  if (rs.next()) {
    const obj = {};
    for (let i = 1; i <= rs.getColumnCount(); i++) {
      obj[rs.getColumnName(i)] = rs.getColumnValue(i);
    }
    return obj;
  }

  return null;
}

function isSafeProjectFqn(fqn) {
  if (!fqn) return false;

  const parts = String(fqn).trim().split(".");
  if (parts.length !== 3) return false;

  const allowed = ''ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.$"'';

  for (const p of parts) {
    if (!p || !String(p).trim()) return false;

    for (let i = 0; i < p.length; i++) {
      if (allowed.indexOf(p[i]) === -1) return false;
    }
  }

  return true;
}

function markFailedAndDone(runId, msg) {
  snowflake.execute({
    sqlText: `
      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
         SET STATUS = ''FAILED'',
             START_TIME = COALESCE(START_TIME, SYSDATE()),
             END_TIME = SYSDATE(),
             ERROR_MESSAGE = ?,
             UPDATED_AT = SYSDATE()
       WHERE RUN_ID = ?
    `,
    binds: [msg, runId]
  });

  /*
    Kept as DONE to preserve your existing behavior.
    If you have adopted FAILED as a queue terminal state everywhere,
    change this to STATUS = ''FAILED''.
  */
  snowflake.execute({
    sqlText: `
      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
         SET STATUS = ''DONE''
       WHERE RUN_ID = ?
    `,
    binds: [runId]
  });
}

function trimTrailingSemicolons(s) {
  let x = String(s || "").trim();
  while (x.endsWith(";")) {
    x = x.slice(0, -1).trim();
  }
  return x;
}

function normalizeHook(v) {
  if (v === null || v === undefined) return [];
  if (Array.isArray(v)) return v;

  const s = String(v).trim();
  if (!s) return [];

  return [s];
}

/*
  Load workflow and task hooks.
*/
const wf = oneRow(
  `
    SELECT
        w.WORKFLOW_ID,
        w.WORKFLOW_NAME,
        COALESCE(w.WORKFLOW_TYPE, ''DBT'') AS WORKFLOW_TYPE,
        w.DBT_COMMAND,
        w.SQL_COMMAND,
        w.IS_ENABLED,
        COALESCE(w.DBT_TARGET, '''') AS DBT_TARGET,
        COALESCE(w.DBT_PROJECT_FQN, '''') AS DBT_PROJECT_FQN,
        t.ON_SUCCESS AS ON_SUCCESS,
        t.ON_FAIL    AS ON_FAIL
     FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOWS w
     LEFT JOIN KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_TASKS t
       ON t.WORKFLOW_ID = w.WORKFLOW_ID
    WHERE w.WORKFLOW_ID = ?
  `,
  [P_WORKFLOW_ID]
);

if (!wf) {
  return {
    ok: false,
    reason: "WORKFLOW_NOT_FOUND",
    workflow_id: String(P_WORKFLOW_ID)
  };
}

const workflowType = String(wf.WORKFLOW_TYPE || "DBT").trim().toUpperCase();
const wtype = (workflowType === "SQL" || workflowType === "DBT") ? workflowType : "DBT";

/*
  Claim/dispatch one queue row.

  Important:
  - DISPATCHING is preferred because the master dispatcher already claimed it.
  - QUEUED allows direct/manual calls to SP_RUN_WORKFLOW.
  - STARTING is retained as a legacy compatibility state.
  - CLAIMED is intentionally not used anymore.
*/
let claimed = null;

for (let attempt = 0; attempt < 5; attempt++) {
  const cand = oneRow(
    `
      SELECT
          QUEUE_ID,
          RUN_ID,
          STATUS,
          CHAIN_ID,
          CHAIN_DEPTH,
          PARENT_RUN_ID,
          DBT_COMMAND_OVERRIDE
       FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
      WHERE TRIM(WORKFLOW_ID) = TRIM(?)
        AND RUN_ID = ?
        AND UPPER(TRIM(STATUS)) IN (''DISPATCHING'', ''QUEUED'', ''STARTING'')
      ORDER BY
        CASE UPPER(TRIM(STATUS))
          WHEN ''DISPATCHING'' THEN 1
          WHEN ''QUEUED''      THEN 2
          WHEN ''STARTING''    THEN 3
          ELSE 9
        END,
        REQUESTED_AT,
        RUN_ID
      LIMIT 1
    `,
    [P_WORKFLOW_ID, P_RUN_ID]
  );

  if (!cand) {
    return {
      ok: false,
      reason: "NOTHING_DISPATCHABLE",
      workflow_id: String(P_WORKFLOW_ID)
    };
  }

  const runId = String(cand.RUN_ID);

  /*
    Now that RUN_ID is known, log early.
    This is useful for debugging cases where dispatch later returns ok=false.
  */
  log(
    runId,
    "INFO",
    "SP_DISPATCH: candidate queue_id=" + String(cand.QUEUE_ID) +
    ", status=" + String(cand.STATUS)
  );

  if (!wf.IS_ENABLED) {
    log(runId, "ERROR", "SP_DISPATCH: workflow disabled");

    markFailedAndDone(runId, "WORKFLOW_DISABLED");

    return {
      ok: false,
      run_id: runId,
      workflow_id: String(P_WORKFLOW_ID),
      reason: "WORKFLOW_DISABLED"
    };
  }

  /*
    Optional single-run concurrency guard.
    Keep this after candidate selection so we can log against RUN_ID.
  */
  const running = oneRow(
    `
      SELECT COUNT(*) AS CNT
        FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
       WHERE WORKFLOW_ID = ?
         AND UPPER(TRIM(STATUS)) = ''RUNNING''
    `,
    [P_WORKFLOW_ID]
  );

  if (running && Number(running.CNT) > 0) {
    log(runId, "INFO", "SP_DISPATCH: blocked because workflow already has RUNNING history row");

    return {
      ok: false,
      run_id: runId,
      workflow_id: String(P_WORKFLOW_ID),
      reason: "ALREADY_RUNNING"
    };
  }

  /*
    Atomic state transition.

    This replaces the old:
      QUEUED/STARTING -> CLAIMED

    New model:
      DISPATCHING/QUEUED/STARTING -> DISPATCHED

    SP_RUN_WORKFLOW should later move:
      DISPATCHED -> RUNNING

    Completion procs should later move:
      RUNNING -> DONE / FAILED
  */
  const upd = snowflake.execute({
    sqlText: `
      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
         SET STATUS = ''DISPATCHED'',
             CLAIMED_AT = COALESCE(CLAIMED_AT, SYSDATE()),
             CLAIMED_BY = COALESCE(CLAIMED_BY, CURRENT_USER())
       WHERE QUEUE_ID = ?
         AND UPPER(TRIM(STATUS)) IN (''DISPATCHING'', ''QUEUED'', ''STARTING'')
    `,
    binds: [cand.QUEUE_ID]
  });

  upd.next();
  const rowsUpdated = Number(upd.getColumnValue(1));

  if (rowsUpdated === 1) {
    claimed = cand;
    break;
  }

  /*
    Someone else changed this row before we did.
    Retry candidate lookup.
  */
}

if (!claimed) {
  return {
    ok: false,
    reason: "CLAIM_RACE_LOST",
    workflow_id: String(P_WORKFLOW_ID)
  };
}

const runId = String(claimed.RUN_ID);

log(runId, "INFO", "SP_DISPATCH: claimed and moved queue row to DISPATCHED");

/*
  Chain context.
*/
const chainId =
  (claimed.CHAIN_ID === null || claimed.CHAIN_ID === undefined)
    ? null
    : String(claimed.CHAIN_ID);

let chainDepth = null;

try {
  chainDepth =
    (claimed.CHAIN_DEPTH === null || claimed.CHAIN_DEPTH === undefined)
      ? null
      : Number(claimed.CHAIN_DEPTH);
} catch (e) {
  chainDepth = null;
}

const parentRunId =
  (claimed.PARENT_RUN_ID === null || claimed.PARENT_RUN_ID === undefined)
    ? null
    : String(claimed.PARENT_RUN_ID);

/*
  Normalize hooks to arrays.
*/
const onSuccess = normalizeHook(wf.ON_SUCCESS);
const onFail    = normalizeHook(wf.ON_FAIL);

/*
  Build exec_sql depending on workflow type.
*/
let execSql = null;

if (wtype === "DBT") {
  const projectFqnRaw = String(wf.DBT_PROJECT_FQN || "").trim();

  if (!projectFqnRaw) {
    log(runId, "ERROR", "SP_DISPATCH: DBT_PROJECT_FQN_MISSING");
    markFailedAndDone(runId, "DBT_PROJECT_FQN_MISSING");

    return {
      ok: false,
      run_id: runId,
      workflow_id: String(P_WORKFLOW_ID),
      reason: "DBT_PROJECT_FQN_MISSING"
    };
  }

  if (!isSafeProjectFqn(projectFqnRaw)) {
    log(runId, "ERROR", "SP_DISPATCH: INVALID_DBT_PROJECT_FQN: " + projectFqnRaw);
    markFailedAndDone(runId, "INVALID_DBT_PROJECT_FQN: " + projectFqnRaw);

    return {
      ok: false,
      run_id: runId,
      workflow_id: String(P_WORKFLOW_ID),
      reason: "INVALID_DBT_PROJECT_FQN",
      project: projectFqnRaw
    };
  }

  if (!wf.DBT_TARGET || String(wf.DBT_TARGET).trim() === "") {
    log(runId, "ERROR", "SP_DISPATCH: DBT_TARGET_MISSING");
    markFailedAndDone(runId, "DBT_TARGET_MISSING");

    return {
      ok: false,
      run_id: runId,
      workflow_id: String(P_WORKFLOW_ID),
      reason: "DBT_TARGET_MISSING"
    };
  }

  let argsCore = String(claimed.DBT_COMMAND_OVERRIDE || wf.DBT_COMMAND || "").trim();

  if (argsCore === "") {
    log(runId, "ERROR", "SP_DISPATCH: DBT_COMMAND_EMPTY");
    markFailedAndDone(runId, "DBT_COMMAND_EMPTY");

    return {
      ok: false,
      run_id: runId,
      workflow_id: String(P_WORKFLOW_ID),
      reason: "DBT_COMMAND_EMPTY"
    };
  }

  let args = argsCore;

  /*
    Add runId vars and target only when target is not already supplied.
  */
  if (!/(^|\\s)--target(\\s|=)/i.test(args)) {
    const runIdClean = String(runId).trim();
    const varsJson = JSON.stringify({ runId: runIdClean });

    args += " --vars ''" + varsJson + "''";
    args += " --target " + String(wf.DBT_TARGET).trim();
  }

  const argsSqlEsc = String(args).replace(/''/g, "''''");

  execSql =
    "EXECUTE DBT PROJECT " + projectFqnRaw +
    " ARGS = ''" + argsSqlEsc + "''";

  log(runId, "INFO", "WORKFLOW_TYPE=DBT");
  log(runId, "INFO", "DBT_EXEC_SQL: " + execSql);

  /*
    Mark history as RUNNING.
    The queue row remains DISPATCHED until SP_RUN_WORKFLOW actually fires the temp task.
  */
  snowflake.execute({
    sqlText: `
      UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
         SET STATUS = ''RUNNING'',
             START_TIME = SYSDATE(),
             DBT_ARGS = ?,
             UPDATED_AT = SYSDATE()
       WHERE RUN_ID = ?
    `,
    binds: [args, runId]
  });

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
  });

  return {
    ok: true,
    run_id: runId,
    workflow_id: String(P_WORKFLOW_ID),
    workflow_type: "DBT",
    exec_sql: execSql,
    chain_id: chainId,
    chain_depth: chainDepth,
    parent_run_id: parentRunId,
    on_success: onSuccess,
    on_fail: onFail
  };
}

/*
  SQL workflow.
*/
const sqlCmdRaw = String(wf.SQL_COMMAND || "");
let sqlCmd = trimTrailingSemicolons(sqlCmdRaw);

/* SQL_RUN_ID_PARAMETER_V2: replace every #[run_id]# token after RUN_ID is known. */
const runIdSql = String(runId).replace(/''/g, "''''");
sqlCmd = sqlCmd.split("#[run_id]#").join(runIdSql);

if (!sqlCmd || sqlCmd.trim() === "") {
  log(runId, "ERROR", "SP_DISPATCH: SQL_COMMAND_EMPTY");
  markFailedAndDone(runId, "SQL_COMMAND_EMPTY");

  return {
    ok: false,
    run_id: runId,
    workflow_id: String(P_WORKFLOW_ID),
    reason: "SQL_COMMAND_EMPTY"
  };
}

const preview =
  sqlCmd.length > 500
    ? sqlCmd.substring(0, 500) + " ..."
    : sqlCmd;

log(runId, "INFO", "WORKFLOW_TYPE=SQL");
log(runId, "INFO", "SQL_EXEC_SQL(preview): " + preview);

snowflake.execute({
  sqlText: `
    UPDATE KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY
       SET STATUS = ''RUNNING'',
           START_TIME = SYSDATE(),
           UPDATED_AT = SYSDATE()
     WHERE RUN_ID = ?
  `,
  binds: [runId]
});

execSql = sqlCmd;

return {
  ok: true,
  run_id: runId,
  workflow_id: String(P_WORKFLOW_ID),
  workflow_type: "SQL",
  exec_sql: execSql,
  chain_id: chainId,
  chain_depth: chainDepth,
  parent_run_id: parentRunId,
  on_success: onSuccess,
  on_fail: onFail
};
';
