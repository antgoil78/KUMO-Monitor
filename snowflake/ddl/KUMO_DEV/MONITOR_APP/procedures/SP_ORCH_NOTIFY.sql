-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_NOTIFY(VARCHAR)
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_NOTIFY(P_RUN_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='Workflow completion email with a durable send claim; never changes execution status'
EXECUTE AS OWNER
AS $$
function statement(sql, binds) { return snowflake.createStatement({sqlText: sql, binds: binds || []}); }
function execute(sql, binds) { var s = statement(sql, binds); s.execute(); return s; }
function rows(sql, binds) {
    var s = statement(sql, binds), r = s.execute(), values = [];
    while (r.next()) { var row = {}; for (var i = 1; i <= s.getColumnCount(); i++) row[s.getColumnName(i)] = r.getColumnValue(i); values.push(row); }
    return values;
}
function first(row) { return row[Object.keys(row)[0]]; }
function object(value) { return typeof value === 'string' ? JSON.parse(value) : value; }
if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(P_RUN_ID))) throw new Error('Invalid workflow run ID');
var env = object(first(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0]));
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
var ns = env.DATABASE + '.' + env.META_SCHEMA, app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
if (!/^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){2}$/.test(env._parameterTable || '')) throw new Error('Invalid parameter table address');
var log = ns + '.WORKFLOW_EXECUTION_LOG';
var run = rows('SELECT * FROM ' + log + ' WHERE EXECUTION_ID = ? AND JOB_ID IS NULL', [P_RUN_ID])[0];
if (!run) return {ok: true, status: 'SKIPPED', reason: 'NOT_WORKFLOW'};
if (['SUCCESS','WARNING','FAILED','CANCELLED'].indexOf(run.STATUS) < 0) return {ok: true, status: 'SKIPPED', reason: 'NOT_TERMINAL'};
var previous = object(run.RESULT || {}).notification;
if (previous) return previous;
// The activation timestamp prevents recovery from mailing historical failures.
var enabled = rows('SELECT 1 AS ENABLED FROM ' + env._parameterTable + ' p JOIN ' + log +
    " e ON e.EXECUTION_ID = ? WHERE p.INSTANCE_NAME = 'DEFAULT' AND p.PARAMETER_GROUP = 'ORCHESTRATION' " +
    "AND p.PARAMETER_KEY = 'NOTIFICATIONS_START_AT' AND p.ACTIVE_FL AND NOT p.IS_SECRET " +
    'AND e.ENDED_AT >= TRY_TO_TIMESTAMP_NTZ(p.PARAMETER_VALUE)', [P_RUN_ID]);
if (!enabled.length) return {ok: true, status: 'SKIPPED', reason: 'BEFORE_ACTIVATION'};
var claim = rows('SELECT UUID_STRING() AS ID')[0].ID;
var report = {ok: true, status: 'SENDING', claim: claim, attemptedAt: new Date().toISOString()};
var claimed = execute('UPDATE ' + log + " SET RESULT = OBJECT_INSERT(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'notification', PARSE_JSON(?), TRUE), UPDATED_AT = SYSDATE() " +
    "WHERE EXECUTION_ID = ? AND JOB_ID IS NULL AND STATUS IN ('SUCCESS','WARNING','FAILED','CANCELLED') AND RESULT:notification IS NULL", [JSON.stringify(report), P_RUN_ID]);
if (claimed.getNumRowsAffected() !== 1) return {ok: true, status: 'SKIPPED', reason: 'ALREADY_CLAIMED'};
function save(status, reason) {
    report.status = status; report.ok = status !== 'ERROR'; report.finishedAt = new Date().toISOString();
    if (reason) report.reason = reason;
    execute('UPDATE ' + log + " SET RESULT = OBJECT_INSERT(COALESCE(RESULT, OBJECT_CONSTRUCT()), 'notification', PARSE_JSON(?), TRUE), UPDATED_AT = SYSDATE() " +
        'WHERE EXECUTION_ID = ? AND RESULT:notification:claim::VARCHAR = ?', [JSON.stringify(report), P_RUN_ID, claim]);
    return report;
}
try {
    var success = run.STATUS === 'SUCCESS' || run.STATUS === 'WARNING';
    var settings = rows('SELECT n.*, g.RECIPIENTS FROM ' + ns + '.WORKFLOW_NOTIFICATIONS n LEFT JOIN ' + ns +
        '.EMAIL_GROUPS g ON g.GROUP_NAME = ' + (success ? 'n.SUCCESS_GROUP' : 'n.FAIL_GROUP') + ' WHERE n.WORKFLOW_ID = ?', [run.WORKFLOW_ID]);
    if (!settings.length) return save('SKIPPED', 'NOT_CONFIGURED');
    if (settings.length !== 1) throw new Error('Duplicate workflow notification settings or recipient groups');
    var setting = settings[0];
    if (!(success ? setting.ON_SUCCESS_EMAIL : setting.ON_FAIL_EMAIL)) return save('SKIPPED', 'DISABLED');
    report.group = success ? setting.SUCCESS_GROUP : setting.FAIL_GROUP;
    if (!String(report.group || '').trim()) return save('SKIPPED', 'NO_GROUP');
    if (!String(setting.RECIPIENTS || '').trim()) throw new Error('Notification group is missing or has no recipients');
    if (!String(setting.EMAIL_INTEGRATION || '').trim()) throw new Error('Email integration is missing');
    var snapshot = object(run.CONFIG_SNAPSHOT || {}), workflow = snapshot.workflow || {};
    var failures = rows('SELECT EXECUTION_ID, JOB_ID, STATUS, ERROR_CODE, ERROR_MESSAGE, QUERY_ID, ' +
        "CONFIG_SNAPSHOT:definition:JOB_NAME::VARCHAR AS JOB_NAME FROM " + log +
        " WHERE WORKFLOW_RUN_ID = ? AND JOB_ID IS NOT NULL AND STATUS IN ('FAILED','CANCELLED') ORDER BY JOB_POSITION", [P_RUN_ID]);
    var details = {workflow_name: workflow.WORKFLOW_NAME, workflow_group: workflow.WORKFLOW_GROUP,
        status: run.STATUS, run_id: P_RUN_ID, run_mode: run.RUN_MODE, trigger_source: run.TRIGGER_SOURCE,
        error_message: run.ERROR_MESSAGE, failed_jobs: failures};
    var sent = first(rows('CALL ' + app + '.SP_SEND_WORKFLOW_EMAIL(?,?,?,?,?,?,?,?,?,?,SYSDATE(),0,PARSE_JSON(?))',
        [setting.EMAIL_INTEGRATION, setting.RECIPIENTS, setting.ENVIRONMENT || env.DATABASE, success ? run.STATUS : 'ERROR',
         P_RUN_ID, workflow.WORKFLOW_GROUP || '', null, workflow.WORKFLOW_NAME || run.WORKFLOW_ID, 'WORKFLOW',
         success ? 'WORKFLOW_SUCCESS' : 'WORKFLOW_FAILED', JSON.stringify(details)])[0]);
    if (sent !== 'EMAIL_SENT') throw new Error('Email sender returned: ' + String(sent));
    return save('SENT');
} catch (error) {
    // A send can succeed before its reply is lost. Keep the attempt durable and
    // require operator review of ERROR/SENDING instead of automatically resending.
    return save('ERROR', String(error.message || error));
}
$$;
