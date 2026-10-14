-- Source: KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_WORKFLOW_TASK_ENTRY(VARCHAR)
-- Refresh: python snowflake/export_ddl.py
USE DATABASE "KUMO_ADMIN";
USE SCHEMA "KUMO_ADMIN"."WORKFLOW_MANAGER";

CREATE OR REPLACE PROCEDURE KUMO_ADMIN.WORKFLOW_MANAGER.X_SP_WORKFLOW_TASK_ENTRY("P_WORKFLOW_ID" VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
EXECUTE AS OWNER
AS '
function oneRow(sqlText, binds){
  const rs = snowflake.execute({sqlText, binds: binds || []});
  if (rs.next()){
    const obj = {};
    for (let i=1; i<=rs.getColumnCount(); i++){
      obj[rs.getColumnName(i)] = rs.getColumnValue(i);
    }
    return obj;
  }
  return null;
}

function callScalar(sqlText, binds){
  const rs = snowflake.execute({sqlText, binds: binds || []});
  rs.next();
  return rs.getColumnValue(1);
}

// Is there already queued work?
const q = oneRow(`
  SELECT COUNT(*) AS CNT
  FROM KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_RUN_QUEUE
  WHERE TRIM(WORKFLOW_ID)=TRIM(?)
    AND UPPER(TRIM(STATUS))=''QUEUED''
`, [P_WORKFLOW_ID]);

if (q && Number(q.CNT) === 0) {
  // No queued runs -> this is presumably a scheduled tick
  callScalar(
    `CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_REQUEST_RUN(?, ''SCHEDULE'')`,
    [P_WORKFLOW_ID]
  );
}

// Always dispatch (it will claim one queued row if available)
const dispatchRes = callScalar(
  `CALL KUMO_ADMIN.WORKFLOW_MANAGER.SP_WORKFLOW_DISPATCH(?)`,
  [P_WORKFLOW_ID]
);

return dispatchRes;
';
