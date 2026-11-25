-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_LAUNCH_RUN(VARCHAR)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_LAUNCH_RUN(P_RUN_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: wake a queued run; the executor alone checks and claims it'
EXECUTE AS OWNER
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
var env = object(first(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0]));
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
if (!uuid(P_RUN_ID)) throw new Error('Invalid execution ID');
var ns = env.DATABASE + '.' + env.META_SCHEMA, app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var log = ns + '.WORKFLOW_EXECUTION_LOG', run = null;
try {
    var runtime = rows("SELECT SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_NAME') AS NAME")[0];
    if (String(runtime.NAME).replace(/"/g, '').toUpperCase() !== app + '.TASK_ORCH_DISPATCH')
        throw new Error('Only the stream dispatcher can launch workflow tasks');
    var requested = rows('SELECT * FROM ' + log + ' WHERE EXECUTION_ID = ? AND JOB_ID IS NULL', [P_RUN_ID])[0];
    if (!requested || !uuid(requested.WORKFLOW_ID)) throw new Error('Execution not found');
    if (['QUEUED', 'CLAIMED'].indexOf(requested.STATUS) < 0)
        return {ok: true, status: requested.STATUS, wakeupScheduled: false};
    var active = rows('SELECT EXECUTION_ID FROM ' + log +
        " WHERE WORKFLOW_ID = ? AND JOB_ID IS NULL AND STATUS = 'RUNNING'", [requested.WORKFLOW_ID]);
    if (active.length) return {ok: true, status: requested.STATUS, waiting: true, wakeupScheduled: false};
    run = rows('SELECT * FROM ' + log +
        " WHERE WORKFLOW_ID = ? AND JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED') ORDER BY REQUESTED_AT, EXECUTION_ID LIMIT 1",
        [requested.WORKFLOW_ID])[0];
    if (run && run.EXECUTION_ID !== P_RUN_ID) return {ok: true, status: requested.STATUS, waiting: true, wakeupScheduled: false};
    if (!run) return {ok: true, status: requested.STATUS, wakeupScheduled: false};
    var address = app + '.TMP_ORCH_WF_' + run.WORKFLOW_ID.replace(/-/g, '_').toUpperCase() + '_' + run.EXECUTION_ID.replace(/-/g, '_').toUpperCase();
    if (run.TASK_FQN !== address) throw new Error('Unexpected temporary workflow task address');
    // Provisioning is idempotent and makes no execution claim. Concurrent
    // wakeups are safe: only the executor can atomically transition to RUNNING.
    var snapshot = object(run.CONFIG_SNAPSHOT), warehouse = snapshot.taskWarehouse;
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(warehouse)) throw new Error('Invalid execution warehouse');
    var body = "CALL " + app + ".SP_ORCH_EXECUTE_WORKFLOW('" + run.EXECUTION_ID + "')";
    execute('CREATE TASK IF NOT EXISTS ' + address + ' WAREHOUSE = ' + warehouse +
        " USER_TASK_TIMEOUT_MS = 21600000 TASK_AUTO_RETRY_ATTEMPTS = 0 COMMENT = 'KUMO parallel orchestration v2: temporary workflow run " + run.EXECUTION_ID + "' AS " + body);
    var task = rows("SHOW TASKS LIKE '" + address.split('.')[2] + "' IN SCHEMA " + app).filter(function(row) { return row.name === address.split('.')[2]; })[0];
    if (!task || String(task.definition).replace(/\s+/g, ' ').trim().toUpperCase() !== body.toUpperCase() || task.schedule ||
        task.comment !== 'KUMO parallel orchestration v2: temporary workflow run ' + run.EXECUTION_ID)
        throw new Error('Temporary workflow task definition does not match this execution');
    var finalizer = address + '_FINALIZER';
    var finalizerBody = 'INSERT INTO ' + ns + '.ORCH_DISPATCH_EVENTS (KIND, RUN_ID, TASK_RUN_GROUP_ID)' +
        " SELECT 'FINISH', '" + run.EXECUTION_ID + "', SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_GRAPH_RUN_GROUP_ID')";
    execute('CREATE TASK IF NOT EXISTS ' + finalizer + ' WAREHOUSE = ' + warehouse +
        " USER_TASK_TIMEOUT_MS = 60000 COMMENT = 'KUMO stream dispatch: finalizer " + run.EXECUTION_ID +
        "' FINALIZE = " + address + ' AS ' + finalizerBody);
    var finalizerTask = rows("SHOW TASKS LIKE '" + finalizer.split('.')[2] + "' IN SCHEMA " + app)
        .filter(function(row) { return row.name === finalizer.split('.')[2]; })[0];
    var relations = finalizerTask && object(finalizerTask.task_relations || '{}');
    if (!finalizerTask || String(finalizerTask.definition).replace(/\s+/g, ' ').trim().toUpperCase() !== finalizerBody.toUpperCase() ||
        finalizerTask.schedule || relations.FinalizedRootTask !== address)
        throw new Error('Unexpected execution finalizer definition');
    execute('ALTER TASK ' + finalizer + ' RESUME');
    var latest = rows('SELECT STATUS FROM ' + log + ' WHERE EXECUTION_ID = ?', [run.EXECUTION_ID])[0];
    if (['QUEUED', 'CLAIMED'].indexOf(latest.STATUS) >= 0) {
        execute('UPDATE ' + log + " SET STATUS = 'QUEUED', UPDATED_AT = SYSDATE() WHERE WORKFLOW_RUN_ID = ? AND JOB_ID IS NOT NULL AND STATUS = 'INITIATING'", [run.EXECUTION_ID]);
        execute('EXECUTE TASK ' + address);
    }
    else if (latest.STATUS !== 'RUNNING') {
        execute('DROP TASK IF EXISTS ' + finalizer); execute('DROP TASK IF EXISTS ' + address);
    }
    return {ok: true, startedRunId: run.EXECUTION_ID, status: latest.STATUS, taskFqn: address, wakeupScheduled: true};
} catch (error) {
    if (run) execute('UPDATE ' + log + " SET ERROR_MESSAGE = ?, UPDATED_AT = SYSDATE() WHERE EXECUTION_ID = ? AND STATUS IN ('QUEUED','CLAIMED')", [String(error.message || error), run.EXECUTION_ID]);
    return {ok: false, error: String(error.message || error)};
}
$$;
