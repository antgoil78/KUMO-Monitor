-- Public compatibility entry point: signal only; never launch from a browser worker.
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_START_RUN(P_RUN_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: signal an accepted run for stream dispatch'
EXECUTE AS OWNER
AS $$
function rows(sql, binds) {
    var s = snowflake.createStatement({sqlText: sql, binds: binds || []}), r = s.execute(), result = [];
    while (r.next()) { var row = {}; for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i); result.push(row); }
    return result;
}
var env = rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0].SP_GET_ENVIRONMENT;
if (typeof env === 'string') env = JSON.parse(env);
['DATABASE', 'META_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(P_RUN_ID))) throw new Error('Invalid execution ID');
var ns = env.DATABASE + '.' + env.META_SCHEMA;
var run = rows('SELECT STATUS FROM ' + ns + '.WORKFLOW_EXECUTION_LOG WHERE EXECUTION_ID = ? AND JOB_ID IS NULL', [P_RUN_ID])[0];
if (!run) throw new Error('Execution not found');
// Admission already wrote the signal. Repeated old-client wakeups must not
// turn WAITING or ERROR deliveries into an unbounded retry loop.
if (['QUEUED', 'CLAIMED'].indexOf(run.STATUS) >= 0)
    rows('INSERT INTO ' + ns + ".ORCH_DISPATCH_EVENTS (KIND, RUN_ID) SELECT 'LAUNCH', ? WHERE NOT EXISTS (SELECT 1 FROM " +
        ns + ".ORCH_DISPATCH_EVENTS WHERE KIND = 'LAUNCH' AND RUN_ID = ?)", [P_RUN_ID, P_RUN_ID]);
return {ok: true, status: run.STATUS, dispatcher: 'STREAM', wakeupScheduled: false};
$$;
