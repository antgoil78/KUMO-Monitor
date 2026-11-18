-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_EXECUTE_WORKFLOW(VARCHAR)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_EXECUTE_WORKFLOW(P_RUN_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: execute only the jobs frozen for this workflow run'
EXECUTE AS CALLER
AS $$
function statement(sql, binds) { return snowflake.createStatement({sqlText: sql, binds: binds || []}); }
function execute(sql, binds) { var s = statement(sql, binds); s.execute(); return s; }
function rows(sql, binds) {
    var s = statement(sql, binds), r = s.execute(), values = [];
    while (r.next()) { var row = {}; for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i); values.push(row); }
    return values;
}
function object(value) { return typeof value === 'string' ? JSON.parse(value) : value; }
function first(row) { return row[Object.keys(row)[0]]; }
if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(P_RUN_ID))) throw new Error('Invalid execution ID');
var env = object(first(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0]));
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
var ns = env.DATABASE + '.' + env.META_SCHEMA, app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var log = ns + '.WORKFLOW_EXECUTION_LOG', runId = String(P_RUN_ID);
var root = rows('SELECT * FROM ' + log + ' WHERE EXECUTION_ID = ? AND JOB_ID IS NULL', [runId])[0];
if (!root) throw new Error('Workflow execution not found');
var expectedTemporary = app + '.TMP_ORCH_WF_' + root.WORKFLOW_ID.replace(/-/g, '_').toUpperCase() + '_' + runId.replace(/-/g, '_').toUpperCase();
var expectedScheduled = app + '.TASK_ORCH_WF_' + root.WORKFLOW_ID.replace(/-/g, '_').toUpperCase() + '_SCHEDULER';
var runtime = rows("SELECT SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_NAME') AS NAME, " +
    "SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_GRAPH_RUN_GROUP_ID') AS GROUP_ID, " +
    "SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_GRAPH_ORIGINAL_SCHEDULED_TIMESTAMP')::TIMESTAMP_LTZ AS SCHEDULED_AT")[0];
var nativeTask = String(runtime.NAME).replace(/"/g, '').toUpperCase();
var temporary = nativeTask === expectedTemporary;
if (!temporary && !(nativeTask === expectedScheduled && root.TRIGGER_SOURCE === 'SCHEDULED' && root.RUN_MODE === 'WORKFLOW'))
    throw new Error('This runner must originate in its workflow execution task');
var claim = rows('SELECT UUID_STRING() AS ID')[0].ID;
var originalTimeout = rows("SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION")[0];
var timeoutValue = originalTimeout && (originalTimeout.value || originalTimeout.VALUE) || '0';
var inTransaction = false, ownsRun = false, dependencyWarnings = [];
function evaluateDependencies(executionId) {
    if (executionId === runId) {
        try {
            var notification = object(first(rows('CALL ' + app + '.SP_ORCH_NOTIFY(?)', [executionId])[0]));
            if (notification.ok === false) dependencyWarnings.push('Notification: ' + notification.reason);
        } catch (error) { dependencyWarnings.push('Notification: ' + String(error.message || error)); }
    }
    var report;
    try {
        report = object(first(rows('CALL ' + app + '.SP_ORCH_DISPATCH_DEPENDENCIES(?)', [executionId])[0]));
        if (!report || report.ok !== true) throw new Error(report && report.error || 'Invalid eligibility response');
        (report.errors || []).forEach(function(error) { dependencyWarnings.push('Dependency dispatch: ' + JSON.stringify(error)); });
    } catch (error) {
        report = {ok: false, executionId: executionId, error: String(error.message || error)};
        dependencyWarnings.push('Dependency evaluation for ' + executionId + ': ' + report.error);
    }
    report.checkedAt = new Date().toISOString();
    try {
        // Preserve command results/counters and never change the workload status.
        execute('UPDATE ' + log + " SET RESULT = OBJECT_INSERT(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'dependencyEvaluation', PARSE_JSON(?), TRUE), UPDATED_AT = SYSDATE() " +
            'WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ? AND ENDED_AT IS NOT NULL', [JSON.stringify(report), executionId, claim]);
    } catch (error) { dependencyWarnings.push('Could not save dependency evaluation for ' + executionId + ': ' + String(error.message || error)); }
    return report;
}
function updateJob(executionId, status, error, queryId, result) {
    var updated = execute('UPDATE ' + log + ' SET STATUS = ?, ENDED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), ERROR_MESSAGE = ?, QUERY_ID = ?, RESULT = PARSE_JSON(?) ' +
        'WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ?', [status, error || null, queryId || null, JSON.stringify(result || {}), executionId, claim]);
    if (updated.getNumRowsAffected() === 1) evaluateDependencies(executionId);
    execute('UPDATE ' + log + ' SET HEARTBEAT_AT = SYSDATE(), UPDATED_AT = SYSDATE() WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ?', [runId, claim]);
}
function finishTask() {
    // A dropped running task completes its current invocation. Failure to clean
    // up or launch the next request is repaired by the new housekeeping task.
    var warnings = [];
    try { if (temporary) execute('DROP TASK IF EXISTS ' + expectedTemporary); } catch (error) { warnings.push(String(error.message || error)); }
    try {
        var next = rows('SELECT EXECUTION_ID FROM ' + log + " WHERE WORKFLOW_ID = ? AND JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED') ORDER BY REQUESTED_AT, EXECUTION_ID LIMIT 1", [root.WORKFLOW_ID]);
        if (next.length) rows('CALL ' + app + '.SP_ORCH_START_RUN(?)', [next[0].EXECUTION_ID]);
    } catch (error) { warnings.push(String(error.message || error)); }
    return warnings;
}
try {
    execute('BEGIN'); inTransaction = true;
    // Same lock/order as admission and configuration writers. Release it as
    // soon as RUNNING is committed; workloads never execute while it is held.
    execute('UPDATE ' + ns + '.DEPENDENCIES SET ALLOW = ALLOW');
    execute('UPDATE ' + ns + '.WORKFLOW SET TASK_NAME = TASK_NAME WHERE WORKFLOW_ID = ?', [root.WORKFLOW_ID]);
    var current = rows('SELECT STATUS FROM ' + log + ' WHERE EXECUTION_ID = ?', [runId])[0];
    if (['QUEUED', 'CLAIMED'].indexOf(current.STATUS) < 0) {
        execute('COMMIT'); inTransaction = false;
        if (current.STATUS !== 'RUNNING') finishTask();
        return {ok: true, runId: runId, status: current.STATUS, reused: true};
    }
    var active = rows('SELECT EXECUTION_ID FROM ' + log + " WHERE WORKFLOW_ID = ? AND JOB_ID IS NULL AND EXECUTION_ID <> ? AND STATUS IN ('CLAIMED','RUNNING')", [root.WORKFLOW_ID, runId]);
    var older = rows('SELECT EXECUTION_ID FROM ' + log + " WHERE WORKFLOW_ID = ? AND JOB_ID IS NULL AND STATUS = 'QUEUED' ORDER BY REQUESTED_AT, EXECUTION_ID LIMIT 1", [root.WORKFLOW_ID]);
    if (active.length || (current.STATUS === 'QUEUED' && older.length && older[0].EXECUTION_ID !== runId)) {
        execute('COMMIT'); inTransaction = false;
        return {ok: true, runId: runId, status: current.STATUS, waiting: true};
    }
    // One canonical two-level graph is shared with eligibility reporting.
    // Queued upstream work has priority. In the reverse direction only an
    // already-running descendant blocks, so two queued relatives cannot deadlock.
    var blockers = rows([
        'WITH relations AS (SELECT * FROM IDENTIFIER(?))',
        ' SELECT DISTINCT r.EXECUTION_ID, r.WORKFLOW_ID, r.STATUS,',
        " CASE WHEN rel.WORKFLOW_ID = ? THEN 'UPSTREAM' ELSE 'DOWNSTREAM' END AS DIRECTION",
        ' FROM relations rel JOIN IDENTIFIER(?) r ON r.JOB_ID IS NULL AND r.EXECUTION_ID <> ?',
        " WHERE (rel.WORKFLOW_ID = ? AND r.WORKFLOW_ID = rel.UPSTREAM_WORKFLOW_ID AND r.STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING'))",
        " OR (rel.UPSTREAM_WORKFLOW_ID = ? AND r.WORKFLOW_ID = rel.WORKFLOW_ID AND r.STATUS = 'RUNNING')",
        ' ORDER BY r.WORKFLOW_ID, r.EXECUTION_ID'
    ].join('\n'), [app + '.V_ORCH_WORKFLOW_RELATIONS', root.WORKFLOW_ID, log, runId, root.WORKFLOW_ID, root.WORKFLOW_ID]);
    if (blockers.length) {
        execute('UPDATE ' + log + " SET STATUS = 'QUEUED', CLAIMED_AT = NULL, ERROR_MESSAGE = NULL, " +
            "RESULT = OBJECT_INSERT(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'startGate', PARSE_JSON(?), TRUE), UPDATED_AT = SYSDATE() " +
            "WHERE EXECUTION_ID = ? AND JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED')",
            [JSON.stringify({state: 'WAITING', checkedAt: new Date().toISOString(), blockers: blockers}), runId]);
        execute('COMMIT'); inTransaction = false;
        return {ok: true, runId: runId, status: 'QUEUED', waiting: true, blockers: blockers};
    }
    var claimed = execute('UPDATE ' + log + " SET STATUS = 'RUNNING', ERROR_MESSAGE = NULL, RESULT = OBJECT_DELETE(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'startGate'), CLAIM_TOKEN = ?, CLAIMED_AT = COALESCE(CLAIMED_AT, SYSDATE()), HEARTBEAT_AT = SYSDATE(), STARTED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), TASK_FQN = ?, TASK_RUN_GROUP_ID = ?, TASK_SCHEDULED_AT = ?::TIMESTAMP_LTZ " +
        "WHERE EXECUTION_ID = ? AND JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED')", [claim, nativeTask, runtime.GROUP_ID, runtime.SCHEDULED_AT, runId]);
    if (claimed.getNumRowsAffected() !== 1) throw new Error('Execution claim conflict');
    execute('UPDATE ' + log + ' SET CLAIM_TOKEN = ?, TASK_FQN = ?, TASK_RUN_GROUP_ID = ?, TASK_SCHEDULED_AT = ?::TIMESTAMP_LTZ WHERE WORKFLOW_RUN_ID = ? AND JOB_ID IS NOT NULL',
        [claim, nativeTask, runtime.GROUP_ID, runtime.SCHEDULED_AT, runId]);
    execute('COMMIT'); inTransaction = false; ownsRun = true;
    var jobs = rows('SELECT EXECUTION_ID, CONFIG_SNAPSHOT FROM ' + log + ' WHERE WORKFLOW_RUN_ID = ? AND JOB_ID IS NOT NULL AND COALESCE(CONFIG_SNAPSHOT:selected::BOOLEAN, TRUE) ORDER BY JOB_POSITION', [runId]);
    var omitted = rows('SELECT COUNT(*) AS N FROM ' + log + " WHERE WORKFLOW_RUN_ID = ? AND JOB_ID IS NOT NULL AND STATUS = 'SKIPPED' AND CONFIG_SNAPSHOT:selected::BOOLEAN = FALSE", [runId])[0].N;
    var parentStatus = null, failures = 0, successes = 0, skipped = Number(omitted || 0);
    var snapshot = object(root.CONFIG_SNAPSHOT || {}), jobExecutionMode = snapshot.jobExecutionMode || 'SEQUENTIAL';
    if (['SEQUENTIAL', 'PARALLEL'].indexOf(jobExecutionMode) < 0) throw new Error('Invalid frozen job execution mode');
    if (jobExecutionMode === 'PARALLEL') {
        // ASYNC statements are children of this native workflow task. Await
        // every result separately so a failed/timed-out job cannot cancel peers.
        var declarations = [], launches = [], waits = [];
        jobs.forEach(function(row, index) {
            if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(row.EXECUTION_ID))) throw new Error('Invalid frozen job execution ID');
            var job = object(row.CONFIG_SNAPSHOT), seconds = job.definition.TIMEOUT_SECONDS === null ? 21600 : Number(job.definition.TIMEOUT_SECONDS);
            if (!Number.isInteger(seconds) || seconds < 1 || seconds > 2147483) throw new Error('Invalid job timeout');
            var name = 'job_' + index;
            declarations.push(name + ' RESULTSET;');
            launches.push('ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = ' + seconds + ';\n' + name +
                " := ASYNC (CALL " + app + ".SP_ORCH_EXECUTE_JOB('" + row.EXECUTION_ID + "', '" + claim + "'));\n");
            waits.push({seconds: seconds, sql: 'BEGIN AWAIT ' + name + '; EXCEPTION WHEN OTHER THEN\n' +
                'UPDATE ' + log + " SET STATUS = 'FAILED', ENDED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), ERROR_MESSAGE = :SQLERRM" +
                " WHERE EXECUTION_ID = '" + row.EXECUTION_ID + "' AND CLAIM_TOKEN = '" + claim + "' AND STATUS IN ('PENDING','INITIATING','QUEUED','RUNNING');\nEND;\n"});
        });
        try {
            execute('EXECUTE IMMEDIATE ?', ['DECLARE\n' + declarations.join('\n') + '\nBEGIN\n' + launches.join('') +
                'ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = ' + Number(timeoutValue) + ';\n' +
                waits.sort(function(a, b) { return a.seconds - b.seconds; }).map(function(wait) { return wait.sql; }).join('') + 'END;']);
        } finally { execute('ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = ' + Number(timeoutValue)); }
        var results = rows('SELECT EXECUTION_ID, STATUS, RESULT FROM ' + log + ' WHERE WORKFLOW_RUN_ID = ? AND JOB_ID IS NOT NULL AND CONFIG_SNAPSHOT:selected::BOOLEAN = TRUE', [runId]);
        results.forEach(function(row) {
            if (['SUCCESS', 'WARNING', 'FAILED', 'CANCELLED', 'SKIPPED'].indexOf(row.STATUS) < 0) throw new Error('Parallel job did not finish');
            if (row.STATUS === 'FAILED' || row.STATUS === 'CANCELLED') failures++;
            else if (row.STATUS === 'SKIPPED') skipped++;
            else successes++;
            if (!(object(row.RESULT || {}) || {}).dependencyEvaluation) evaluateDependencies(row.EXECUTION_ID);
        });
    } else jobs.forEach(function(row, index) {
        var job = object(row.CONFIG_SNAPSHOT), rule = job.dependency, reason = null;
        if (job.definition.IS_ENABLED === false) reason = 'Job is disabled';
        if (rule.ALLOW === false) reason = 'ALLOW is false';
        if (index > 0 && (parentStatus === 'SKIPPED' || rule[parentStatus === 'FAILED' ? 'ERROR' : parentStatus] === false))
            reason = 'Parent result ' + parentStatus + ' does not satisfy the dependency';
        if (reason) {
            updateJob(row.EXECUTION_ID, 'SKIPPED', reason); parentStatus = 'SKIPPED'; skipped++; return;
        }
        execute('UPDATE ' + log + " SET STATUS = 'QUEUED', UPDATED_AT = SYSDATE() WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ? AND STATUS IN ('PENDING','INITIATING')", [row.EXECUTION_ID, claim]);
        execute('UPDATE ' + log + " SET STATUS = 'RUNNING', STARTED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), HEARTBEAT_AT = SYSDATE() WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ? AND STATUS IN ('PENDING','INITIATING','QUEUED')", [row.EXECUTION_ID, claim]);
        var jobStatement = null;
        try {
            var seconds = job.definition.TIMEOUT_SECONDS === null ? 21600 : Number(job.definition.TIMEOUT_SECONDS);
            if (!Number.isInteger(seconds) || seconds < 1 || seconds > 2147483) throw new Error('Invalid job timeout');
            execute('ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = ' + seconds);
            if (job.sqlCondition) {
                var condition = rows(job.sqlCondition);
                if (condition.length !== 1 || Object.keys(condition[0]).length !== 1 || typeof first(condition[0]) !== 'boolean')
                    throw new Error('SQL condition must return exactly one BOOLEAN value');
                if (!first(condition[0])) {
                    updateJob(row.EXECUTION_ID, 'SKIPPED', 'SQL condition returned false'); parentStatus = 'SKIPPED'; skipped++; return;
                }
            }
            if (job.definition.JOB_TYPE === 'DBT') jobStatement = statement(job.resolved.execSql);
            else {
                // A scripting block is one statement and preserves multi-
                // statement SQL jobs without splitting semicolons in strings.
                var block = 'BEGIN\n' + String(job.resolved.execSql).replace(/;+\s*$/, '') + ';\nEND;';
                jobStatement = statement('EXECUTE IMMEDIATE ?', [block]);
            }
            jobStatement.execute();
            updateJob(row.EXECUTION_ID, 'SUCCESS', null, jobStatement.getQueryId(), {commandCompleted: true});
            parentStatus = 'SUCCESS'; successes++;
        } catch (error) {
            var queryId = null;
            try { if (jobStatement) queryId = jobStatement.getQueryId(); } catch (ignored) {}
            updateJob(row.EXECUTION_ID, 'FAILED', String(error.message || error), queryId);
            parentStatus = 'FAILED'; failures++;
        } finally {
            execute('ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = ' + Number(timeoutValue));
        }
    });
    var status = failures ? 'FAILED' : (successes ? 'SUCCESS' : 'SKIPPED');
    execute('UPDATE ' + log + ' SET STATUS = ?, ENDED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), HEARTBEAT_AT = SYSDATE(), RESULT = PARSE_JSON(?), ERROR_MESSAGE = ? ' +
        "WHERE EXECUTION_ID = ? AND CLAIM_TOKEN = ? AND STATUS = 'RUNNING'", [status,
         JSON.stringify({succeeded: successes, failed: failures, skipped: skipped}), failures ? 'One or more jobs failed' : null, runId, claim]);
    // Job checks see the parent workflow RUNNING. Recheck once its final result
    // is durable, before cleanup/advancing the next request for this workflow.
    var dependencyEvaluation = evaluateDependencies(runId);
    return {ok: true, runId: runId, status: status, taskFqn: nativeTask,
        dependencyEvaluation: dependencyEvaluation, warnings: finishTask().concat(dependencyWarnings)};
} catch (error) {
    if (inTransaction) execute('ROLLBACK');
    if (ownsRun) {
        execute('UPDATE ' + log + " SET STATUS = IFF(JOB_ID IS NOT NULL AND STATUS IN ('PENDING','INITIATING','QUEUED'), 'SKIPPED', 'FAILED'), ENDED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), ERROR_MESSAGE = ? WHERE WORKFLOW_RUN_ID = ? AND CLAIM_TOKEN = ? AND STATUS IN ('PENDING','INITIATING','QUEUED','RUNNING','CLAIMED')",
            [String(error.message || error), runId, claim]);
        try {
            var ended = rows('SELECT EXECUTION_ID FROM ' + log + ' WHERE WORKFLOW_RUN_ID = ? AND CLAIM_TOKEN = ? AND ENDED_AT IS NOT NULL ' +
                'AND RESULT:dependencyEvaluation IS NULL ORDER BY JOB_POSITION NULLS LAST', [runId, claim]);
            ended.forEach(function(row) { evaluateDependencies(row.EXECUTION_ID); });
        } catch (ignored) {}
        finishTask();
    }
    throw error;
}
$$;
