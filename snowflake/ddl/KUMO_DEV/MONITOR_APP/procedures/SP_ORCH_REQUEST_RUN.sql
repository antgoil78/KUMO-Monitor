-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_REQUEST_RUN(VARCHAR, VARCHAR, VARCHAR, VARCHAR, VARCHAR, BOOLEAN)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_REQUEST_RUN(
    P_WORKFLOW_ID VARCHAR, P_RUN_MODE VARCHAR, P_JOB_ID VARCHAR,
    P_REQUEST_ID VARCHAR, P_REQUESTED_BY VARCHAR, P_FULL_REFRESH BOOLEAN DEFAULT FALSE
)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: snapshot enabled jobs for one on-demand workflow task'
EXECUTE AS OWNER
AS $$
function statement(sql, binds) { return snowflake.createStatement({sqlText: sql, binds: binds || []}); }
function execute(sql, binds) { var s = statement(sql, binds); s.execute(); return s; }
function rows(sql, binds) {
    var s = statement(sql, binds), r = s.execute(), values = [];
    while (r.next()) {
        var row = {};
        for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i);
        values.push(row);
    }
    return values;
}
function object(value) { return typeof value === 'string' ? JSON.parse(value) : value; }
function fail(message) { throw new Error(message); }
var environment = object(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0].SP_GET_ENVIRONMENT);
if (!environment) fail('Environment configuration is unavailable');
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(environment[key])) fail('Invalid environment identifier: ' + key);
});
var ns = environment.DATABASE + '.' + environment.META_SCHEMA;
var app = environment.DATABASE + '.' + environment.MONITOR_APP_SCHEMA;
var log = ns + '.WORKFLOW_EXECUTION_LOG';
var requestedMode = String(P_RUN_MODE || 'WORKFLOW').toUpperCase();
var scheduled = requestedMode === 'SCHEDULED';
var mode = scheduled ? 'WORKFLOW' : requestedMode;
var fullRefresh = P_FULL_REFRESH === true;
var requestId = String(P_REQUEST_ID || '').trim();
if (['WORKFLOW', 'JOB_ONLY', 'FROM_JOB'].indexOf(mode) < 0) fail('Choose WORKFLOW, JOB_ONLY or FROM_JOB');
if (!requestId || requestId.length > 128) fail('A request ID of at most 128 characters is required');
if ((mode === 'WORKFLOW' && P_JOB_ID) || (mode !== 'WORKFLOW' && !P_JOB_ID)) fail('Job selection does not match the run mode');
if (fullRefresh && (scheduled || mode === 'WORKFLOW')) fail('Full refresh requires a selected dbt job');
var started = false;
try {
    execute('BEGIN'); started = true;
    // Standard-table constraints are informational. Serialize requests against
    // this existing workflow row before checking idempotency and inserting.
    execute('UPDATE ' + ns + '.DEPENDENCIES SET ALLOW = ALLOW');
    var lock = execute('UPDATE ' + ns + '.WORKFLOW SET TASK_NAME = TASK_NAME WHERE WORKFLOW_ID = ?', [P_WORKFLOW_ID]);
    if (lock.getNumRowsAffected() !== 1) fail('Workflow not found or duplicate workflow ID');
    var previous = rows('SELECT WORKFLOW_RUN_ID, RUN_MODE, REQUESTED_JOB_ID, STATUS, TASK_FQN, CONFIG_SNAPSHOT FROM ' + log +
        ' WHERE WORKFLOW_ID = ? AND JOB_ID IS NULL AND REQUEST_ID = ?', [P_WORKFLOW_ID, requestId]);
    if (previous.length) {
        if (previous.length !== 1 || previous[0].RUN_MODE !== mode || previous[0].REQUESTED_JOB_ID !== (P_JOB_ID || null) ||
            !!object(previous[0].CONFIG_SNAPSHOT).fullRefresh !== fullRefresh)
            fail('Request ID already belongs to a different request');
        execute('COMMIT'); started = false;
        return {ok: true, runId: previous[0].WORKFLOW_RUN_ID, status: previous[0].STATUS,
                taskFqn: previous[0].TASK_FQN, reused: true};
    }
    var workflow = rows('SELECT * FROM ' + ns + '.WORKFLOW WHERE WORKFLOW_ID = ?', [P_WORKFLOW_ID])[0];
    if (!workflow.IS_ENABLED) fail('Workflow is disabled');
    if (scheduled) {
        var scheduler = app + '.TASK_ORCH_WF_' + String(P_WORKFLOW_ID).replace(/-/g, '_').toUpperCase() + '_SCHEDULER';
        var nativeTask = rows("SELECT SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_NAME') AS NAME")[0].NAME;
        if (String(nativeTask).replace(/"/g, '').toUpperCase() !== scheduler || !workflow.IS_SCHEDULE_ENABLED)
            fail('Scheduled requests must originate from the enabled workflow scheduler');
    } else if (!workflow.ALLOW_MANUAL_TRIGGER) fail('Manual execution is disabled for this workflow');
    var warehouse = workflow.WAREHOUSE_NAME || rows('SELECT CURRENT_WAREHOUSE() AS NAME')[0].NAME;
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(warehouse)) fail('Configure a valid workflow warehouse');
    var records = rows('SELECT * FROM ' + ns + '.DEPENDENCIES WHERE WORKFLOW_ID = ?', [P_WORKFLOW_ID]);
    var members = records.filter(function(row) { return row.JOB_ID !== null; });
    if (!members.length) fail('Assign at least one job to this workflow');
    var headers = records.filter(function(row) { return row.JOB_ID === null; });
    if (headers.length > 1) fail('Duplicate workflow dependency rows');
    if (headers.length && headers[0].ALLOW === false) fail('Workflow ALLOW is false');
    var byParent = {}, seen = {}, ordered = [], parent = P_WORKFLOW_ID;
    members.forEach(function(row) {
        if (seen[row.JOB_ID] || !row.PARENT_ID || byParent[row.PARENT_ID]) fail('Job dependencies must form one unambiguous parent chain');
        seen[row.JOB_ID] = true; byParent[row.PARENT_ID] = row;
    });
    while (byParent[parent]) {
        var next = byParent[parent]; ordered.push(next); delete byParent[parent]; parent = next.JOB_ID;
        if (ordered.length > members.length) fail('Job dependency cycle');
    }
    if (ordered.length !== members.length) fail('Missing, cyclic or disconnected job parent');
    var selectedIndex = 0;
    if (mode !== 'WORKFLOW') {
        selectedIndex = ordered.findIndex(function(row) { return row.JOB_ID === P_JOB_ID; });
        if (selectedIndex < 0) fail('The selected job is not assigned to this workflow');
    }
    var selected = mode === 'JOB_ONLY' ? [ordered[selectedIndex]] : ordered.slice(selectedIndex);
    var selectedIds = {};
    selected.forEach(function(dependency) { selectedIds[dependency.JOB_ID] = true; });
    var runId = rows('SELECT UUID_STRING() AS ID')[0].ID;
    var expectedTask = app + '.TMP_ORCH_WF_' + String(P_WORKFLOW_ID).replace(/-/g, '_').toUpperCase() + '_' + runId.replace(/-/g, '_').toUpperCase();
    var executions = [];
    var legacy = /\b(?:WORKFLOW_HISTORY|WORKFLOW_RUN_QUEUE|WORKFLOW_TMP_TASKS|APP_WORKFLOW_RUN_LOCKS|TASK_WF_MASTER_DISPATCHER|SP_RUN_WORKFLOW|SP_WORKFLOW_DISPATCH|SP_WORKFLOW_REQUEST_RUN|SP_SQL_COMPLETE|SP_DBT_COMPLETE|SP_CLEANUP_OLD_TMP_TASKS)\b/i;
    var firstRunnable = true;
    ordered.forEach(function(dependency, index) {
        var definitions = rows('SELECT * FROM ' + ns + '.JOB WHERE JOB_ID = ?', [dependency.JOB_ID]);
        if (definitions.length !== 1) fail('Missing or duplicate job definition: ' + dependency.JOB_ID);
        var definition = definitions[0];
        var refreshJob = fullRefresh && definition.JOB_ID === P_JOB_ID;
        if (refreshJob && (definition.JOB_TYPE !== 'DBT' || definition.IS_ENABLED === false || dependency.ALLOW === false))
            fail('Full refresh requires an enabled dbt job with ALLOW enabled');
        var included = !!selectedIds[dependency.JOB_ID] && definition.IS_ENABLED !== false;
        var skipReason = !selectedIds[dependency.JOB_ID] ? 'Not selected for this execution' :
            definition.IS_ENABLED === false ? 'Job is disabled' : dependency.ALLOW === false ? 'ALLOW is false' : null;
        if (!skipReason && ['SQL', 'DBT'].indexOf(definition.JOB_TYPE) < 0) fail('Parallel engine supports SQL and DBT jobs');
        var executionId = rows('SELECT UUID_STRING() AS ID')[0].ID;
        var resolution = skipReason ? {} : object(rows('CALL ' + app + '.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(?), ?)', [JSON.stringify({
            jobType: definition.JOB_TYPE, sqlCommand: definition.SQL_COMMAND,
            dbtCommand: definition.DBT_COMMAND, dbtProjectFqn: definition.DBT_PROJECT_FQN,
            dbtTarget: definition.DBT_TARGET, fullRefresh: refreshJob
        }), executionId])[0].SP_RESOLVE_WORKFLOW_COMMAND);
        if (!skipReason && (!resolution || !resolution.ok)) fail('Command resolution failed: ' + (resolution && resolution.error));
        if (legacy.test(resolution.execSql)) fail('This job references the existing execution engine and requires review: ' + definition.JOB_NAME);
        var sqlCondition = null;
        if (!skipReason && dependency.SQL && String(dependency.SQL).trim()) {
            var condition = object(rows('CALL ' + app + '.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(?), ?)',
                [JSON.stringify({jobType: 'SQL', sqlCommand: dependency.SQL}), executionId])[0].SP_RESOLVE_WORKFLOW_COMMAND);
            if (!condition.ok || !/^\s*(SELECT|WITH)\b/i.test(condition.execSql) || legacy.test(condition.execSql))
                fail('SQL conditions must be SELECT queries independent of the existing execution engine');
            sqlCondition = condition.execSql;
        }
        if (!skipReason && definition.TIMEOUT_SECONDS !== null && (!Number.isInteger(Number(definition.TIMEOUT_SECONDS)) || Number(definition.TIMEOUT_SECONDS) < 1))
            fail('Invalid job timeout');
        var initialStatus = skipReason ? 'SKIPPED' : firstRunnable ? 'INITIATING' : 'PENDING';
        if (!skipReason) firstRunnable = false;
        executions.push({executionId: executionId, jobId: definition.JOB_ID, position: index + 1,
            definition: definition, dependency: dependency, resolved: resolution, sqlCondition: sqlCondition,
            selected: included, initialStatus: initialStatus, skipReason: skipReason});
    });
    if (!executions.some(function(job) { return job.selected; })) fail('No enabled jobs in the selected scope');
    var requestedBy = String(P_REQUESTED_BY || rows('SELECT CURRENT_USER() AS NAME')[0].NAME).slice(0, 256);
    var snapshot = {workflow: workflow, workflowRule: headers[0] || null, runMode: mode,
        requestedJobId: P_JOB_ID || null, fullRefresh: fullRefresh, selectedJobIds: executions.filter(function(job) { return job.selected; }).map(function(job) { return job.jobId; }),
        upstreamBypassed: mode !== 'WORKFLOW', downstreamWorkflows: false, taskWarehouse: String(warehouse).toUpperCase(), taskLifecycle: 'TEMPORARY'};
    execute('INSERT INTO ' + log + ' (EXECUTION_ID, WORKFLOW_RUN_ID, WORKFLOW_ID, REQUEST_ID, RUN_MODE, REQUESTED_JOB_ID, TRIGGER_SOURCE, STATUS, REQUESTED_BY, TASK_FQN, CONFIG_SNAPSHOT)' +
        " SELECT ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, PARSE_JSON(?)",
        [runId, runId, P_WORKFLOW_ID, requestId, mode, P_JOB_ID || null, scheduled ? 'SCHEDULED' : 'MANUAL', requestedBy, expectedTask, JSON.stringify(snapshot)]);
    var parentExecution = runId;
    executions.forEach(function(job) {
        execute('INSERT INTO ' + log + ' (EXECUTION_ID, WORKFLOW_RUN_ID, WORKFLOW_ID, JOB_ID, PARENT_EXECUTION_ID, REQUEST_ID, RUN_MODE, REQUESTED_JOB_ID, TRIGGER_SOURCE, STATUS, JOB_POSITION, REQUESTED_BY, TASK_FQN, CONFIG_SNAPSHOT, ERROR_MESSAGE, ENDED_AT)' +
            " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, PARSE_JSON(?), ?, IFF(? = 'SKIPPED', SYSDATE(), NULL)",
            [job.executionId, runId, P_WORKFLOW_ID, job.jobId, job.selected ? parentExecution : runId, requestId, mode, P_JOB_ID || null, scheduled ? 'SCHEDULED' : 'MANUAL',
             job.initialStatus, job.position, requestedBy, expectedTask, JSON.stringify(job), job.skipReason, job.initialStatus]);
        if (job.selected) parentExecution = job.executionId;
    });
    execute('COMMIT'); started = false;
    return {ok: true, runId: runId, status: 'QUEUED', taskFqn: expectedTask, jobCount: executions.filter(function(job) { return job.selected; }).length, totalJobs: executions.length, reused: false};
} catch (error) {
    if (started) execute('ROLLBACK');
    return {ok: false, error: String(error.message || error)};
}
$$;
