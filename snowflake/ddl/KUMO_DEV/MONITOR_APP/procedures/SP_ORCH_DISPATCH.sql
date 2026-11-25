CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_DISPATCH()
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO stream dispatch: consume signals durably, launch independently, and stop when no work is ready'
EXECUTE AS OWNER
AS $$
function statement(sql, binds) { return snowflake.createStatement({sqlText: sql, binds: binds || []}); }
function rows(sql, binds) {
    var s = statement(sql, binds), r = s.execute(), result = [];
    while (r.next()) { var row = {}; for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i); result.push(row); }
    return result;
}
function first(row) { return row[Object.keys(row)[0]]; }
function value(sql, binds) { var v = first(rows(sql, binds)[0]); return typeof v === 'string' ? JSON.parse(v) : v; }
var env = value('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()');
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
var ns = env.DATABASE + '.' + env.META_SCHEMA, app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var current = String(first(rows("SELECT SYSTEM$TASK_RUNTIME_INFO('CURRENT_TASK_NAME') AS NAME")[0])).replace(/"/g, '').toUpperCase();
if (current !== app + '.TASK_ORCH_DISPATCH') throw new Error('Dispatch must run in its non-overlapping native task');
var events = ns + '.ORCH_DISPATCH_DELIVERIES', source = ns + '.ORCH_DISPATCH_EVENTS', log = ns + '.WORKFLOW_EXECUTION_LOG';
// Leave headroom below the native five-minute timeout for a slow launch and
// durable continuation. A large batch must not rely on timeout cancellation.
var deadline = Date.now() + 120000;
// Consume the snapshot into a separate durable table. Delivery bookkeeping
// must never write to the stream source and retrigger idle dispatch.
rows('INSERT INTO ' + events + ' (EVENT_ID, KIND, RUN_ID, TASK_RUN_GROUP_ID, STATE, CREATED_AT) ' +
    "SELECT s.EVENT_ID, s.KIND, s.RUN_ID, s.TASK_RUN_GROUP_ID, 'READY', s.CREATED_AT FROM " +
    ns + ".ORCH_DISPATCH_EVENTS_STREAM s WHERE s.KIND <> 'WAKE' AND NOT EXISTS (SELECT 1 FROM " +
    events + ' d WHERE d.EVENT_ID = s.EVENT_ID)');
// A process killed during the last allowed attempt leaves a visible ERROR,
// rather than an unserviceable READY row or an unbounded retry.
rows('UPDATE ' + events + " SET STATE = 'ERROR', LAST_ERROR = COALESCE(LAST_ERROR, 'Delivery interrupted during its final attempt'), UPDATED_AT = SYSDATE() WHERE STATE = 'READY' AND ATTEMPTS >= 3");
// Native retries resume durable READY work even after the stream was consumed.
var batch = rows('SELECT EVENT_ID, KIND, RUN_ID, TASK_RUN_GROUP_ID, ATTEMPTS FROM ' + events +
    " WHERE STATE = 'READY' AND ATTEMPTS < 3 ORDER BY IFF(KIND IN ('FINISH','DEPENDENCY'),0,1), CREATED_AT, EVENT_ID LIMIT 100");
var processed = [], errors = [], launchedWorkflows = Object.create(null);
for (var bi = 0; bi < batch.length && Date.now() < deadline; bi++) {
    var event = batch[bi];
    rows('UPDATE ' + events + " SET ATTEMPTS = ATTEMPTS + 1, UPDATED_AT = SYSDATE() WHERE EVENT_ID = ? AND STATE = 'READY'", [event.EVENT_ID]);
    try {
        var state = 'DONE', report;
        if (event.KIND === 'LAUNCH') {
            var root = rows('SELECT WORKFLOW_ID, STATUS FROM ' + log + ' WHERE EXECUTION_ID = ? AND JOB_ID IS NULL', [event.RUN_ID])[0];
            if (!root) throw new Error('Recorded execution is missing');
            if (['QUEUED','CLAIMED'].indexOf(root.STATUS) >= 0) {
                if (launchedWorkflows[root.WORKFLOW_ID]) state = 'WAITING';
                else {
                    report = value('CALL ' + app + '.SP_ORCH_LAUNCH_RUN(?)', [event.RUN_ID]);
                    if (!report.ok) throw new Error(report.error || 'Native launch failed');
                    if (!report.wakeupScheduled) state = 'WAITING';
                    else launchedWorkflows[root.WORKFLOW_ID] = true;
                }
            }
        } else if (event.KIND === 'FINISH') {
            report = value('CALL ' + app + '.SP_ORCH_FINISH_RUN(?, ?)', [event.RUN_ID, event.TASK_RUN_GROUP_ID]);
            if (!report.ok) throw new Error(report.error || 'Finalization failed');
        } else if (event.KIND === 'DEPENDENCY') {
            report = value('CALL ' + app + '.SP_ORCH_DISPATCH_DEPENDENCIES(?)', [event.RUN_ID]);
            if (!report.ok || (report.errors || []).length) throw new Error(JSON.stringify(report));
        } else throw new Error('Unknown dispatch event type');
        rows('UPDATE ' + events + ' SET STATE = ?, LAST_ERROR = NULL, UPDATED_AT = SYSDATE() WHERE EVENT_ID = ?', [state, event.EVENT_ID]);
        processed.push(event.EVENT_ID);
    } catch (error) {
        var message = String(error.message || error);
        rows('UPDATE ' + events + " SET STATE = IFF(ATTEMPTS >= 3, 'ERROR', 'READY'), LAST_ERROR = ?, UPDATED_AT = SYSDATE() WHERE EVENT_ID = ?", [message, event.EVENT_ID]);
        errors.push({eventId: event.EVENT_ID, error: message});
    }
}
// Only actual remaining delivery work creates a continuation. WAITING/ERROR
// rows never trigger themselves. New arrivals keep their own unread signal.
if (rows('SELECT COUNT(*) AS N FROM ' + events + " WHERE STATE = 'READY' AND ATTEMPTS < 3")[0].N > 0)
    rows('INSERT INTO ' + source + " (KIND) VALUES ('WAKE')");
// Retention work happens only during genuine dispatch; no cleanup clock.
if (batch.length) {
    rows('DELETE FROM ' + source + " WHERE CREATED_AT < DATEADD('day', -7, SYSDATE()) AND (KIND = 'WAKE' OR EVENT_ID IN (SELECT EVENT_ID FROM " + events + " WHERE STATE = 'DONE'))");
    rows('DELETE FROM ' + events + " WHERE STATE = 'DONE' AND UPDATED_AT < DATEADD('day', -7, SYSDATE())");
}
return {ok: errors.length === 0, processed: processed, errors: errors};
$$;
