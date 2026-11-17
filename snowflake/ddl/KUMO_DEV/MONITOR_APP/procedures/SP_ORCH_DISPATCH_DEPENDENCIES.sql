-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_DISPATCH_DEPENDENCIES(VARCHAR)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_DISPATCH_DEPENDENCIES(P_EXECUTION_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: admit and start eligible child workflows once per parent execution set'
EXECUTE AS OWNER
AS $$
function rows(sql, binds) {
    var s = snowflake.createStatement({sqlText: sql, binds: binds || []}), r = s.execute(), values = [];
    while (r.next()) {
        var row = {};
        for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i);
        values.push(row);
    }
    return values;
}
function first(row) { return row[Object.keys(row)[0]]; }
function object(value) { return typeof value === 'string' ? JSON.parse(value) : value; }
var env = object(first(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0]));
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
var app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var log = env.DATABASE + '.' + env.META_SCHEMA + '.WORKFLOW_EXECUTION_LOG';
var report = object(first(rows('CALL ' + app + '.SP_ORCH_EVALUATE_DEPENDENCIES(?)', [P_EXECUTION_ID])[0]));
if (!report || report.ok !== true) throw new Error('Dependency eligibility evaluation failed');
report.dispatches = []; report.errors = [];
if (!report.evaluated) return report;
var source = rows('SELECT REQUESTED_BY FROM ' + log + ' WHERE EXECUTION_ID = ?', [P_EXECUTION_ID])[0];
if (!source) throw new Error('Source execution no longer exists');
var admitted = [];
// Enqueue all siblings before starting any: a fast sibling must see the other
// sibling's new QUEUED run, rather than a success left over from an older run.
report.children.forEach(function(child) {
    if (!child.eligible && !child.rules.some(function(rule) { return rule.state === 'BUSY'; })) return;
    try {
        // Admission rechecks current rules under its locks and derives its own
        // stable request ID from the complete parent workflow/run vector.
        var request = object(first(rows('CALL ' + app + '.SP_ORCH_REQUEST_RUN(?, ?, NULL, NULL, ?, FALSE, ?)',
            [child.workflowId, 'DEPENDENCY', source.REQUESTED_BY, P_EXECUTION_ID])[0]));
        if (!request || request.ok !== true) throw new Error(request && request.error || 'Dependency admission failed');
        var dispatch = {workflowId: child.workflowId, workflowName: child.workflowName,
            runId: request.runId || null, status: request.status || 'NOT_READY', reused: request.reused === true};
        report.dispatches.push(dispatch);
        if (request.runId && ['QUEUED', 'CLAIMED'].indexOf(request.status) >= 0) admitted.push(dispatch);
    } catch (error) {
        report.errors.push({workflowId: child.workflowId, error: String(error.message || error)});
    }
});
admitted.forEach(function(dispatch) {
    try {
        var started = object(first(rows('CALL ' + app + '.SP_ORCH_START_RUN(?)', [dispatch.runId])[0]));
        if (!started || started.ok !== true) throw new Error(started && started.error || 'Dependency task wakeup failed');
        dispatch.wakeupScheduled = started.wakeupScheduled === true;
    } catch (error) {
        // The durable request remains QUEUED/CLAIMED for native reconciliation.
        dispatch.error = String(error.message || error);
        report.errors.push({workflowId: dispatch.workflowId, runId: dispatch.runId, error: dispatch.error});
    }
});
return report;
$$;
