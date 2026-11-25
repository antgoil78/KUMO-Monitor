-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_RECONCILE(VARCHAR)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_RECONCILE(P_WORKFLOW_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: recover failed runs, clean temporary workflow tasks and advance queues'
EXECUTE AS OWNER
AS $$
function rows(sql, binds) {
    var s = snowflake.createStatement({sqlText: sql, binds: binds || []}), r = s.execute(), values = [];
    while (r.next()) { var row = {}; for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i); values.push(row); }
    return values;
}
function first(row) { return row[Object.keys(row)[0]]; }
var environment = first(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0]);
if (typeof environment === 'string') environment = JSON.parse(environment);
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(environment[key])) throw new Error('Invalid environment identifier');
});
var app = environment.DATABASE + '.' + environment.MONITOR_APP_SCHEMA;
var ns = environment.DATABASE + '.' + environment.META_SCHEMA;
var log = ns + '.WORKFLOW_EXECUTION_LOG';
// Snowflake passes a SQL NULL procedure argument as JavaScript undefined.
var workflowId = P_WORKFLOW_ID == null ? null : String(P_WORKFLOW_ID);
var filter = '(?::VARCHAR IS NULL OR WORKFLOW_ID = ?)';
var active = rows('SELECT EXECUTION_ID, WORKFLOW_ID, STATUS, TASK_FQN, TASK_RUN_GROUP_ID FROM ' + log +
    " WHERE " + filter + " AND JOB_ID IS NULL AND STATUS IN ('CLAIMED','RUNNING')", [workflowId, workflowId]);
var recovered = [], removed = [], errors = [];
function evaluateRecoveredRun(runId) {
    // Recovery writes the terminal workflow/job statuses before evaluating them.
    var ended = rows('SELECT EXECUTION_ID FROM ' + log + ' WHERE WORKFLOW_RUN_ID = ? AND ENDED_AT IS NOT NULL ' +
        'AND RESULT:dependencyEvaluation IS NULL ORDER BY JOB_POSITION NULLS LAST', [runId]);
    ended.forEach(function(execution) {
        var report;
        try {
            report = first(rows('CALL ' + app + '.SP_ORCH_DISPATCH_DEPENDENCIES(?)', [execution.EXECUTION_ID])[0]);
            if (typeof report === 'string') report = JSON.parse(report);
            if (!report || report.ok !== true) throw new Error(report && report.error || 'Invalid eligibility response');
            (report.errors || []).forEach(function(error) { errors.push('Dependency dispatch: ' + JSON.stringify(error)); });
        } catch (error) {
            report = {ok: false, executionId: execution.EXECUTION_ID, error: String(error.message || error)};
            errors.push('Dependency evaluation for ' + execution.EXECUTION_ID + ': ' + report.error);
        }
        report.checkedAt = new Date().toISOString();
        try {
            rows('UPDATE ' + log + " SET RESULT = OBJECT_INSERT(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'dependencyEvaluation', PARSE_JSON(?), TRUE), UPDATED_AT = SYSDATE() " +
                'WHERE EXECUTION_ID = ? AND WORKFLOW_RUN_ID = ? AND ENDED_AT IS NOT NULL', [JSON.stringify(report), execution.EXECUTION_ID, runId]);
        } catch (error) { errors.push('Could not save dependency evaluation: ' + String(error.message || error)); }
    });
}
active.forEach(function(run) {
    try {
        var prefix = app + '.TMP_ORCH_WF_' + run.WORKFLOW_ID.replace(/-/g, '_').toUpperCase() + '_' + run.EXECUTION_ID.replace(/-/g, '_').toUpperCase();
        var scheduler = app + '.TASK_ORCH_WF_' + run.WORKFLOW_ID.replace(/-/g, '_').toUpperCase() + '_SCHEDULER';
        if (run.TASK_FQN !== prefix && run.TASK_FQN !== scheduler) throw new Error('Unexpected workflow task address');
        var task = run.TASK_FQN.split('.');
        var history = rows('SELECT STATE, ERROR_CODE, ERROR_MESSAGE FROM TABLE(' + environment.DATABASE +
            ".INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => ?, SCHEDULED_TIME_RANGE_START => DATEADD('day', -7, CURRENT_TIMESTAMP()), RESULT_LIMIT => 1000)) " +
            'WHERE DATABASE_NAME = ? AND SCHEMA_NAME = ? AND (?::VARCHAR IS NULL OR GRAPH_RUN_GROUP_ID = ?) ORDER BY SCHEDULED_TIME DESC LIMIT 1',
            [task[2], task[0], task[1], run.TASK_RUN_GROUP_ID, run.TASK_RUN_GROUP_ID]);
        if (history.length && ['FAILED', 'CANCELLED', 'FAILED_AND_AUTO_SUSPENDED'].indexOf(history[0].STATE) >= 0) {
            var result = history[0], status = result.STATE === 'CANCELLED' ? 'CANCELLED' : 'FAILED';
            rows('UPDATE ' + log + " SET STATUS = IFF(JOB_ID IS NOT NULL AND STATUS IN ('PENDING','INITIATING','QUEUED'), 'SKIPPED', ?), ENDED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), ERROR_CODE = ?, ERROR_MESSAGE = ? " +
                "WHERE WORKFLOW_RUN_ID = ? AND STATUS IN ('CLAIMED','RUNNING','PENDING','INITIATING','QUEUED')", [status, String(result.ERROR_CODE || ''), result.ERROR_MESSAGE || 'Workflow task did not complete', run.EXECUTION_ID]);
            recovered.push(run.EXECUTION_ID);
            evaluateRecoveredRun(run.EXECUTION_ID);
        } else if (run.STATUS === 'CLAIMED' && (!history.length || history[0].STATE === 'SUCCEEDED')) {
            // Resume provisioning after a lost reply or interrupted launcher.
            // The runner claims the same immutable run; no workload is replayed.
            rows('CALL ' + app + '.SP_ORCH_START_RUN(?)', [run.EXECUTION_ID]);
        }
    } catch (error) { errors.push(String(error.message || error)); }
});
// Also catch interruptions after final status was written but before the email
// hook ran. The activation marker excludes old runs; the notifier claims once.
try {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){2}$/.test(environment._parameterTable || '')) throw new Error('Invalid parameter table address');
    var notifications = rows('SELECT e.EXECUTION_ID FROM ' + log + ' e JOIN ' + environment._parameterTable + ' p ON ' +
        "p.INSTANCE_NAME = 'DEFAULT' AND p.PARAMETER_GROUP = 'ORCHESTRATION' AND p.PARAMETER_KEY = 'NOTIFICATIONS_START_AT' " +
        'AND p.ACTIVE_FL AND NOT p.IS_SECRET WHERE (?::VARCHAR IS NULL OR e.WORKFLOW_ID = ?) AND e.JOB_ID IS NULL ' +
        "AND e.STATUS IN ('SUCCESS','WARNING','FAILED','CANCELLED') AND e.RESULT:notification IS NULL " +
        'AND e.ENDED_AT >= TRY_TO_TIMESTAMP_NTZ(p.PARAMETER_VALUE) ORDER BY e.ENDED_AT LIMIT 100', [workflowId, workflowId]);
    notifications.forEach(function(run) {
        try {
            var report = first(rows('CALL ' + app + '.SP_ORCH_NOTIFY(?)', [run.EXECUTION_ID])[0]);
            if (typeof report === 'string') report = JSON.parse(report);
            if (report.ok === false) errors.push('Notification: ' + report.reason);
        } catch (error) { errors.push('Notification: ' + String(error.message || error)); }
    });
} catch (error) { errors.push('Notification recovery: ' + String(error.message || error)); }
// List only this engine's temporary tasks, rather than scanning old log rows
// and issuing DROP repeatedly for every historical execution.
var tasks = rows("SHOW TASKS LIKE 'TMP_ORCH_WF_%' IN SCHEMA " + app);
tasks.forEach(function(task) {
    try {
        if (!/^TMP_ORCH_WF_[A-F0-9_]+$/.test(task.name) || !String(task.comment).startsWith('KUMO parallel orchestration v2: temporary workflow run ')) return;
        var address = app + '.' + task.name;
        var run = rows('SELECT EXECUTION_ID, WORKFLOW_ID, STATUS FROM ' + log + ' WHERE JOB_ID IS NULL AND TASK_FQN = ? AND ' + filter, [address, workflowId, workflowId])[0];
        if (!run || ['QUEUED','CLAIMED','RUNNING'].indexOf(run.STATUS) >= 0) return;
        var expected = app + '.TMP_ORCH_WF_' + run.WORKFLOW_ID.replace(/-/g, '_').toUpperCase() + '_' + run.EXECUTION_ID.replace(/-/g, '_').toUpperCase();
        if (address !== expected) throw new Error('Unexpected cleanup task address');
        rows('DROP TASK IF EXISTS ' + address + '_FINALIZER');
        rows('DROP TASK IF EXISTS ' + address); removed.push(address);
    } catch (error) { errors.push(String(error.message || error)); }
});
var queued = rows('SELECT EXECUTION_ID FROM ' + log + ' q WHERE ' + filter +
    " AND JOB_ID IS NULL AND STATUS = 'QUEUED' AND NOT EXISTS (SELECT 1 FROM " + log +
    " a WHERE a.WORKFLOW_ID = q.WORKFLOW_ID AND a.JOB_ID IS NULL AND a.STATUS IN ('CLAIMED','RUNNING')) " +
    'QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKFLOW_ID ORDER BY REQUESTED_AT, EXECUTION_ID) = 1', [workflowId, workflowId]);
queued.forEach(function(run) {
    try { rows('CALL ' + app + '.SP_ORCH_START_RUN(?)', [run.EXECUTION_ID]); }
    catch (error) { errors.push(String(error.message || error)); }
});
// A finalizer can be lost after terminal status was committed. Recover missing
// dependency signals for this engine's admitted runs, including individual jobs,
// without relying on a native task that cleanup may already have removed.
rows('INSERT INTO ' + ns + ".ORCH_DISPATCH_EVENTS (KIND, RUN_ID) SELECT 'DEPENDENCY', e.EXECUTION_ID FROM " + log +
    " e WHERE (?::VARCHAR IS NULL OR e.WORKFLOW_ID = ?) AND e.ENDED_AT IS NOT NULL " +
    'AND EXISTS (SELECT 1 FROM ' + ns + ".ORCH_DISPATCH_EVENTS a WHERE a.KIND = 'LAUNCH' AND a.RUN_ID = e.WORKFLOW_RUN_ID) " +
    'AND NOT EXISTS (SELECT 1 FROM ' + ns + ".ORCH_DISPATCH_EVENTS d WHERE d.KIND = 'DEPENDENCY' AND d.RUN_ID = e.EXECUTION_ID) " +
    'AND (e.JOB_ID IS NOT NULL OR NOT EXISTS (SELECT 1 FROM ' + ns +
    ".ORCH_DISPATCH_EVENTS f WHERE f.KIND = 'FINISH' AND f.RUN_ID = e.EXECUTION_ID))", [workflowId, workflowId]);
// Explicit recovery is user-driven, never called by a scheduled maintenance task.
// Re-arm unfinished deliveries only in the requested scope.
rows('UPDATE ' + ns + ".ORCH_DISPATCH_DELIVERIES SET STATE = 'READY', ATTEMPTS = 0, LAST_ERROR = NULL, UPDATED_AT = SYSDATE() " +
    "WHERE (STATE = 'ERROR' OR (STATE = 'READY' AND ATTEMPTS >= 3) OR (KIND = 'LAUNCH' AND STATE = 'WAITING')) AND RUN_ID IN (SELECT EXECUTION_ID FROM " + log +
    " WHERE " + filter + ')', [workflowId, workflowId]);
rows('INSERT INTO ' + ns + ".ORCH_DISPATCH_EVENTS (KIND) SELECT 'WAKE' WHERE EXISTS (SELECT 1 FROM " + ns +
    ".ORCH_DISPATCH_DELIVERIES WHERE STATE = 'READY' AND ATTEMPTS < 3)");
return {ok: true, recoveredRunIds: recovered, removedTasks: removed, errors: errors};
$$;
