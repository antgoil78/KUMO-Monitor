-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_RUN_SCHEDULED(VARCHAR)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_RUN_SCHEDULED(P_WORKFLOW_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: snapshot and run enabled jobs inside the persistent workflow scheduler'
EXECUTE AS CALLER
AS $$
function value(sql, binds) {
    var s = snowflake.createStatement({sqlText: sql, binds: binds || []}), r = s.execute();
    if (!r.next()) throw new Error('No procedure result');
    var v = r.getColumnValue(1); return typeof v === 'string' ? JSON.parse(v) : v;
}
var env = value('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()');
['DATABASE', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
var app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var runtime = snowflake.execute({sqlText: "SELECT SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_GRAPH_RUN_GROUP_ID')"});
runtime.next();
var group = String(runtime.getColumnValue(1));
var result = value('CALL ' + app + '.SP_ORCH_REQUEST_RUN(?, ?, ?, ?, ?)',
    [P_WORKFLOW_ID, 'SCHEDULED', null, 'SCHEDULED:' + group, 'SCHEDULER']);
if (!result.ok) throw new Error(result.error);
var executed = value('CALL ' + app + '.SP_ORCH_EXECUTE_WORKFLOW(?)', [result.runId]);
if (executed.waiting) value('CALL ' + app + '.SP_ORCH_START_RUN(?)', [result.runId]);
return executed;
$$;
