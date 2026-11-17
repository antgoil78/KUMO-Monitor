-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_EXECUTE_JOB(VARCHAR,VARCHAR)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_EXECUTE_JOB(P_EXECUTION_ID VARCHAR, P_CLAIM_TOKEN VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: execute one frozen parallel job inside its claimed workflow task'
EXECUTE AS CALLER
AS $$
function statement(sql, binds) { return snowflake.createStatement({sqlText: sql, binds: binds || []}); }
function execute(sql, binds) { var s = statement(sql, binds); s.execute(); return s; }
function rows(sql, binds) {
    var s = statement(sql, binds), r = s.execute(), result = [];
    while (r.next()) { var row = {}; for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i); result.push(row); }
    return result;
}
function object(value) { return typeof value === 'string' ? JSON.parse(value) : value; }
function first(row) { return row[Object.keys(row)[0]]; }
function uuid(value) { return /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(value)); }
if (!uuid(P_EXECUTION_ID) || !uuid(P_CLAIM_TOKEN)) throw new Error('Invalid job claim');
var env = object(first(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0]));
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
var log = env.DATABASE + '.' + env.META_SCHEMA + '.WORKFLOW_EXECUTION_LOG';
var app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var record = rows('SELECT j.*, r.TASK_FQN AS ROOT_TASK FROM ' + log + ' j JOIN ' + log +
    " r ON r.EXECUTION_ID = j.WORKFLOW_RUN_ID AND r.JOB_ID IS NULL AND r.STATUS = 'RUNNING' AND r.CLAIM_TOKEN = j.CLAIM_TOKEN" +
    " WHERE j.EXECUTION_ID = ? AND j.JOB_ID IS NOT NULL AND j.CLAIM_TOKEN = ? AND j.CONFIG_SNAPSHOT:selected::BOOLEAN = TRUE" +
    " AND r.CONFIG_SNAPSHOT:jobExecutionMode::VARCHAR = 'PARALLEL'", [P_EXECUTION_ID, P_CLAIM_TOKEN])[0];
if (!record) throw new Error('This job does not belong to a running parallel workflow claim');
var nativeTask = rows("SELECT SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_NAME') AS NAME")[0].NAME;
if (String(nativeTask).replace(/"/g, '').toUpperCase() !== record.ROOT_TASK)
    throw new Error('Parallel jobs must originate in their workflow task');
if (['INITIATING', 'PENDING', 'QUEUED', 'SKIPPED'].indexOf(record.STATUS) < 0)
    return {ok: true, status: record.STATUS, reused: true};
var job = object(record.CONFIG_SNAPSHOT), status = 'SUCCESS', message = null, queryId = null;
var reason = job.definition.IS_ENABLED === false ? 'Job is disabled' : job.dependency.ALLOW === false ? 'ALLOW is false' : null;
if (reason || record.STATUS === 'SKIPPED') {
    status = 'SKIPPED'; message = reason || record.ERROR_MESSAGE;
} else {
    var claimed = execute('UPDATE ' + log + " SET STATUS = 'RUNNING', STARTED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), HEARTBEAT_AT = SYSDATE()" +
        " WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ? AND STATUS IN ('INITIATING','PENDING','QUEUED')", [P_EXECUTION_ID, P_CLAIM_TOKEN]);
    if (claimed.getNumRowsAffected() !== 1) throw new Error('Parallel job claim conflict');
    var command = null;
    try {
        if (job.sqlCondition) {
            var condition = rows(job.sqlCondition);
            if (condition.length !== 1 || Object.keys(condition[0]).length !== 1 || typeof first(condition[0]) !== 'boolean')
                throw new Error('SQL condition must return exactly one BOOLEAN value');
            if (!first(condition[0])) { status = 'SKIPPED'; message = 'SQL condition returned false'; }
        }
        if (status !== 'SKIPPED') {
            command = job.definition.JOB_TYPE === 'DBT' ? statement(job.resolved.execSql) :
                statement('EXECUTE IMMEDIATE ?', ['BEGIN\n' + String(job.resolved.execSql).replace(/;+\s*$/, '') + ';\nEND;']);
            command.execute(); queryId = command.getQueryId();
        }
    } catch (error) {
        status = 'FAILED'; message = String(error.message || error);
        try { if (command) queryId = command.getQueryId(); } catch (ignored) {}
    }
}
execute('UPDATE ' + log + ' SET STATUS = ?, ENDED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), ERROR_MESSAGE = ?, QUERY_ID = ?, RESULT = PARSE_JSON(?)' +
    ' WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ?',
    [status, message, queryId, JSON.stringify({commandCompleted: status === 'SUCCESS'}), P_EXECUTION_ID, P_CLAIM_TOKEN]);
// Session settings are deliberately not changed here. Each ASYNC call inherits
// its own timeout when submitted; changing a shared session inside jobs races.
var report;
try {
    report = object(first(rows('CALL ' + app + '.SP_ORCH_DISPATCH_DEPENDENCIES(?)', [P_EXECUTION_ID])[0]));
    if (!report || report.ok !== true) throw new Error('Dependency dispatch failed');
} catch (error) { report = {ok: false, error: String(error.message || error)}; }
report.checkedAt = new Date().toISOString();
execute('UPDATE ' + log + " SET RESULT = OBJECT_INSERT(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'dependencyEvaluation', PARSE_JSON(?), TRUE), UPDATED_AT = SYSDATE()" +
    ' WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ?', [JSON.stringify(report), P_EXECUTION_ID, P_CLAIM_TOKEN]);
return {ok: true, status: status};
$$;
