CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_FINISH_RUN(P_RUN_ID VARCHAR, P_GROUP_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO stream dispatch: reconcile one completed native invocation without replaying its workload'
EXECUTE AS OWNER
AS $$
function rows(sql, binds) {
    var s = snowflake.createStatement({sqlText: sql, binds: binds || []}), r = s.execute(), result = [];
    while (r.next()) { var row = {}; for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i); result.push(row); }
    return result;
}
function first(row) { return row[Object.keys(row)[0]]; }
function value(sql, binds) { var v = first(rows(sql, binds)[0]); return typeof v === 'string' ? JSON.parse(v) : v; }
var env = value('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()');
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(P_RUN_ID)) || !P_GROUP_ID)
    throw new Error('Use a recorded run and native task group');
var ns = env.DATABASE + '.' + env.META_SCHEMA, app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var log = ns + '.WORKFLOW_EXECUTION_LOG', events = ns + '.ORCH_DISPATCH_DELIVERIES';
var run = rows('SELECT * FROM ' + log + ' WHERE EXECUTION_ID = ? AND JOB_ID IS NULL', [P_RUN_ID])[0];
if (!run) throw new Error('Execution not found');
// A delayed/duplicated completion must never terminate or delete a later invocation.
if (run.TASK_RUN_GROUP_ID && String(run.TASK_RUN_GROUP_ID) !== String(P_GROUP_ID)) return {ok: true, stale: true};
var address = app + '.TMP_ORCH_WF_' + run.WORKFLOW_ID.replace(/-/g, '_').toUpperCase() + '_' + P_RUN_ID.replace(/-/g, '_').toUpperCase();
if (run.TASK_FQN !== address) throw new Error('Unexpected execution task address');
var history = rows('SELECT STATE, ERROR_CODE, ERROR_MESSAGE FROM TABLE(' + env.DATABASE +
    ".INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => ?, SCHEDULED_TIME_RANGE_START => DATEADD('day', -7, CURRENT_TIMESTAMP()), RESULT_LIMIT => 1000)) " +
    'WHERE DATABASE_NAME = ? AND SCHEMA_NAME = ? AND GRAPH_RUN_GROUP_ID = ? ORDER BY SCHEDULED_TIME DESC LIMIT 1',
    [address.split('.')[2], env.DATABASE, env.MONITOR_APP_SCHEMA, P_GROUP_ID]);
if (!history.length || ['SUCCEEDED', 'FAILED', 'CANCELLED', 'FAILED_AND_AUTO_SUSPENDED'].indexOf(history[0].STATE) < 0)
    throw new Error('Native completion is not yet visible; retry finalization');
var native = history[0], terminal = ['SUCCESS', 'WARNING', 'FAILED', 'ERROR', 'CANCELLED', 'SKIPPED'];
var waiting = ['QUEUED', 'CLAIMED'].indexOf(run.STATUS) >= 0 && native.STATE === 'SUCCEEDED';
if (!waiting && terminal.indexOf(run.STATUS) < 0) {
    var status = native.STATE === 'CANCELLED' ? 'CANCELLED' : 'FAILED';
    rows('UPDATE ' + log + " SET STATUS = IFF(JOB_ID IS NOT NULL AND STATUS IN ('PENDING','INITIATING','QUEUED'), 'SKIPPED', ?), " +
        'ENDED_AT = SYSDATE(), UPDATED_AT = SYSDATE(), ERROR_CODE = ?, ERROR_MESSAGE = ? ' +
        "WHERE WORKFLOW_RUN_ID = ? AND STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')",
        [status, String(native.ERROR_CODE || ''), native.ERROR_MESSAGE || 'Native task ended without recording a terminal execution', P_RUN_ID]);
}
if (waiting) {
    rows('UPDATE ' + events + " SET STATE = 'WAITING', UPDATED_AT = SYSDATE() WHERE KIND = 'LAUNCH' AND RUN_ID = ? AND STATE <> 'ERROR'", [P_RUN_ID]);
} else {
    // A job may have committed its status just before its dependency hook was
    // interrupted. Backfill only a missing durable signal; never rerun the job.
    rows('INSERT INTO ' + ns + ".ORCH_DISPATCH_EVENTS (KIND, RUN_ID) SELECT 'DEPENDENCY', j.EXECUTION_ID FROM " + log +
        ' j WHERE j.WORKFLOW_RUN_ID = ? AND j.JOB_ID IS NOT NULL AND j.ENDED_AT IS NOT NULL ' +
        'AND NOT EXISTS (SELECT 1 FROM ' + ns + ".ORCH_DISPATCH_EVENTS d WHERE d.KIND = 'DEPENDENCY' AND d.RUN_ID = j.EXECUTION_ID)", [P_RUN_ID]);
    var report = value('CALL ' + app + '.SP_ORCH_DISPATCH_DEPENDENCIES(?)', [P_RUN_ID]);
    if (!report.ok || (report.errors || []).length) throw new Error('Dependency dispatch requires retry: ' + JSON.stringify(report));
    rows('UPDATE ' + log + " SET RESULT = OBJECT_INSERT(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'dependencyEvaluation', PARSE_JSON(?), TRUE), UPDATED_AT = SYSDATE() WHERE EXECUTION_ID = ?",
        [JSON.stringify(report), P_RUN_ID]);
    // Existing send claims deliberately do not resend uncertain SENDING/ERROR emails.
    value('CALL ' + app + '.SP_ORCH_NOTIFY(?)', [P_RUN_ID]);
    // Real terminal events release existing requests in both directions of the
    // activity gate. The executor still checks/claims atomically before work.
    rows('UPDATE ' + events + " SET STATE = 'READY', ATTEMPTS = 0, LAST_ERROR = NULL, UPDATED_AT = SYSDATE() WHERE KIND = 'LAUNCH' AND STATE = 'WAITING' " +
        "AND RUN_ID IN (SELECT EXECUTION_ID FROM " + log + " WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED'))");
}
// The root is suspended (manual EXECUTE TASK), and the native run has ended.
// Drop its finalizer before the root, including a finalizer still returning from INSERT.
rows('DROP TASK IF EXISTS ' + address + '_FINALIZER');
rows('DROP TASK IF EXISTS ' + address);
return {ok: true, runId: P_RUN_ID, waiting: waiting};
$$;
