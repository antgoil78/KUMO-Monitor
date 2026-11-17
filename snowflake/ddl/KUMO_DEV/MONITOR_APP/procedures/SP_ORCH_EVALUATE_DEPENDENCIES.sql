-- Source: KUMO_DEV.MONITOR_APP.SP_ORCH_EVALUATE_DEPENDENCIES(VARCHAR)
-- Read-only eligibility, called after job/workflow completion and recovery.
CREATE OR REPLACE PROCEDURE KUMO_DEV.MONITOR_APP.SP_ORCH_EVALUATE_DEPENDENCIES(P_EXECUTION_ID VARCHAR)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
COMMENT='KUMO parallel orchestration v2: report workflow dependency eligibility without dispatching'
EXECUTE AS CALLER
AS $$
function statement(sql, binds) { return snowflake.createStatement({sqlText: sql, binds: binds || []}); }
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
function first(row) { return row[Object.keys(row)[0]]; }
var terminal = ['SUCCESS', 'WARNING', 'FAILED', 'ERROR', 'CANCELLED', 'SKIPPED'];
if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(P_EXECUTION_ID)))
    throw new Error('Use a recorded job or workflow execution UUID');
var env = object(first(rows('CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT()')[0]));
['DATABASE', 'META_SCHEMA', 'MONITOR_APP_SCHEMA'].forEach(function(key) {
    if (!/^[A-Za-z_][A-Za-z0-9_$]*$/.test(env[key])) throw new Error('Invalid environment identifier');
});
var ns = env.DATABASE + '.' + env.META_SCHEMA, app = env.DATABASE + '.' + env.MONITOR_APP_SCHEMA;
var log = ns + '.WORKFLOW_EXECUTION_LOG';
var events = rows('SELECT EXECUTION_ID, WORKFLOW_RUN_ID, WORKFLOW_ID, RUN_MODE, STATUS, ENDED_AT ' +
    'FROM IDENTIFIER(?) WHERE EXECUTION_ID = ?', [log, P_EXECUTION_ID]);
if (events.length !== 1) throw new Error('Execution not found or duplicate execution ID');
var event = events[0];
var report = {ok: true, executionId: P_EXECUTION_ID, sourceWorkflowId: event.WORKFLOW_ID,
    sourceRunId: event.WORKFLOW_RUN_ID, evaluated: false, children: []};
if (!event.ENDED_AT || terminal.indexOf(event.STATUS) < 0) {
    report.reason = 'Execution has not finished'; return report;
}
if (event.RUN_MODE !== 'WORKFLOW') {
    report.reason = 'Partial runs do not release downstream workflows'; return report;
}

// Rank ALL runs before inspecting status. An older success cannot hide a newer
// queued/running/failed/partial run. Job assignments never participate here.
var dependencySql = [
    'WITH workflow_definitions AS (',
    ' SELECT WORKFLOW_ID, MAX(WORKFLOW_NAME) AS WORKFLOW_NAME, COUNT(*) AS DEFINITION_COUNT,',
    ' MAX(CASE WHEN IS_ENABLED THEN 1 ELSE 0 END) AS ENABLED',
    ' FROM IDENTIFIER(?) GROUP BY WORKFLOW_ID',
    '), workflow_runs AS (',
    ' SELECT EXECUTION_ID, WORKFLOW_ID, RUN_MODE, STATUS, STARTED_AT, ENDED_AT,',
    ' ROW_NUMBER() OVER (PARTITION BY WORKFLOW_ID ORDER BY REQUESTED_AT DESC, EXECUTION_ID DESC) AS RN',
    ' FROM IDENTIFIER(?) WHERE JOB_ID IS NULL',
    '), latest_runs AS (SELECT * FROM workflow_runs WHERE RN = 1),',
    ' child_starts AS (',
    ' SELECT WORKFLOW_ID, MAX(STARTED_AT) AS LAST_STARTED_AT FROM workflow_runs GROUP BY WORKFLOW_ID',
    '), busy_workflows AS (',
    " SELECT DISTINCT WORKFLOW_ID FROM workflow_runs WHERE STATUS IN ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING')",
    '), workflow_rules AS (SELECT * FROM IDENTIFIER(?) WHERE JOB_ID IS NULL),',
    ' relations AS (SELECT * FROM IDENTIFIER(?)),',
    ' rule_activity AS (',
    ' SELECT rel.WORKFLOW_ID, rel.PARENT_ID,',
    ' MAX(CASE WHEN rel.DEPTH = 1 AND b.WORKFLOW_ID IS NOT NULL THEN 1 ELSE 0 END) AS PARENT_BUSY,',
    ' COUNT(DISTINCT CASE WHEN rel.DEPTH = 2 THEN b.WORKFLOW_ID END) AS BUSY_GRANDPARENTS',
    ' FROM relations rel LEFT JOIN busy_workflows b ON b.WORKFLOW_ID = rel.UPSTREAM_WORKFLOW_ID',
    ' GROUP BY rel.WORKFLOW_ID, rel.PARENT_ID',
    '),',
    ' candidates AS (',
    ' SELECT DISTINCT d.WORKFLOW_ID FROM workflow_rules d',
    ' JOIN relations rel ON rel.WORKFLOW_ID = d.WORKFLOW_ID',
    " WHERE rel.UPSTREAM_WORKFLOW_ID = ? AND d.TRIGGER_TYPE = 'DEP'",
    '), rule_runs AS (',
    ' SELECT d.WORKFLOW_ID, d.PARENT_ID, d.TRIGGER_TYPE, d.ALLOW, d."SQL" AS SQL_CONDITION,',
    ' d."SUCCESS", d."WARNING", d."ERROR", d.PARENT_MUST_BE_NEWER, cs.LAST_STARTED_AT AS CHILD_LAST_STARTED_AT, child.WORKFLOW_NAME, child.ENABLED AS CHILD_ENABLED,',
    ' child.DEFINITION_COUNT AS CHILD_COUNT, parent.DEFINITION_COUNT AS PARENT_COUNT,',
    ' p.EXECUTION_ID AS PARENT_EXECUTION_ID, p.RUN_MODE AS PARENT_RUN_MODE,',
    ' p.STATUS AS PARENT_STATUS, p.ENDED_AT AS PARENT_ENDED_AT, busy.WORKFLOW_ID AS BUSY_WORKFLOW_ID,',
    ' activity.PARENT_BUSY, activity.BUSY_GRANDPARENTS,',
    ' COUNT(*) OVER (PARTITION BY d.WORKFLOW_ID, d.PARENT_ID) AS RULE_COUNT',
    ' FROM workflow_rules d JOIN candidates c ON c.WORKFLOW_ID = d.WORKFLOW_ID',
    ' LEFT JOIN workflow_definitions child ON child.WORKFLOW_ID = d.WORKFLOW_ID',
    ' LEFT JOIN workflow_definitions parent ON parent.WORKFLOW_ID = d.PARENT_ID',
    ' LEFT JOIN latest_runs p ON p.WORKFLOW_ID = d.PARENT_ID',
    ' LEFT JOIN busy_workflows busy ON busy.WORKFLOW_ID = d.WORKFLOW_ID',
    ' LEFT JOIN child_starts cs ON cs.WORKFLOW_ID = d.WORKFLOW_ID',
    ' LEFT JOIN rule_activity activity ON activity.WORKFLOW_ID = d.WORKFLOW_ID AND activity.PARENT_ID = d.PARENT_ID',
    ')',
    ' SELECT WORKFLOW_ID, WORKFLOW_NAME, PARENT_ID, PARENT_EXECUTION_ID, PARENT_STATUS, SQL_CONDITION,',
    ' PARENT_MUST_BE_NEWER, PARENT_ENDED_AT, CHILD_LAST_STARTED_AT, PARENT_BUSY, BUSY_GRANDPARENTS,',
    ' CASE',
    " WHEN COALESCE(CHILD_COUNT, 0) <> 1 OR COALESCE(PARENT_COUNT, 0) <> 1 THEN 'INVALID_REFERENCE'",
    " WHEN WORKFLOW_ID = PARENT_ID THEN 'SELF_DEPENDENCY'",
    " WHEN RULE_COUNT <> 1 THEN 'DUPLICATE_RULE'",
    " WHEN COALESCE(TRIGGER_TYPE, '') <> 'DEP' THEN 'INVALID_TRIGGER'",
    " WHEN CHILD_ENABLED <> 1 THEN 'DISABLED'",
    " WHEN ALLOW = FALSE THEN 'SKIPPED'",
    " WHEN BUSY_WORKFLOW_ID IS NOT NULL THEN 'BUSY'",
    " WHEN PARENT_EXECUTION_ID IS NULL THEN 'WAITING'",
    " WHEN PARENT_RUN_MODE <> 'WORKFLOW' THEN 'PARTIAL_PARENT_RUN'",
    " WHEN PARENT_ENDED_AT IS NULL OR PARENT_STATUS NOT IN ('SUCCESS','WARNING','FAILED','ERROR','CANCELLED','SKIPPED') THEN 'WAITING'",
    " WHEN PARENT_BUSY = 1 THEN 'PARENT_BUSY'",
    " WHEN BUSY_GRANDPARENTS > 0 THEN 'GRANDPARENT_BUSY'",
    " WHEN PARENT_STATUS IN ('CANCELLED','SKIPPED') THEN 'BLOCKED'",
    " WHEN PARENT_STATUS = 'SUCCESS' AND \"SUCCESS\" = FALSE THEN 'BLOCKED'",
    " WHEN PARENT_STATUS = 'WARNING' AND \"WARNING\" = FALSE THEN 'BLOCKED'",
    " WHEN PARENT_STATUS IN ('FAILED','ERROR') AND \"ERROR\" = FALSE THEN 'BLOCKED'",
    " WHEN PARENT_MUST_BE_NEWER = TRUE AND CHILD_LAST_STARTED_AT IS NOT NULL AND PARENT_ENDED_AT <= CHILD_LAST_STARTED_AT THEN 'STALE_PARENT'",
    " WHEN TRIM(COALESCE(SQL_CONDITION, '')) <> '' THEN 'SQL_CHECK'",
    " ELSE 'READY' END AS RULE_STATE",
    ' FROM rule_runs ORDER BY WORKFLOW_ID, PARENT_ID'
].join('\n');
// A grandparent completion also rechecks grandchildren which were waiting for it.
var rules = rows(dependencySql, [ns + '.WORKFLOW', log, ns + '.DEPENDENCIES', app + '.V_ORCH_WORKFLOW_RELATIONS', event.WORKFLOW_ID]);
var children = Object.create(null);
rules.forEach(function(rule) {
    var child = children[rule.WORKFLOW_ID];
    if (!child) {
        child = {workflowId: rule.WORKFLOW_ID, workflowName: rule.WORKFLOW_NAME, eligible: false, rules: []};
        children[rule.WORKFLOW_ID] = child; report.children.push(child);
    }
    child.rules.push({parentWorkflowId: rule.PARENT_ID, parentExecutionId: rule.PARENT_EXECUTION_ID,
        parentStatus: rule.PARENT_STATUS, parentMustBeNewer: rule.PARENT_MUST_BE_NEWER === true,
        parentBusy: rule.PARENT_BUSY === 1, busyGrandparentCount: Number(rule.BUSY_GRANDPARENTS || 0),
        parentEndedAt: rule.PARENT_ENDED_AT, childLastStartedAt: rule.CHILD_LAST_STARTED_AT, state: rule.RULE_STATE, sql: rule.SQL_CONDITION || ''});
});
// Grandparent events can return both a parent and its child. Admission must
// queue the parent first so the child's recheck sees its new QUEUED run.
// This is ordering only; activity checks above stop at two upstream levels.
var pending = report.children.slice(), ordered = [], emitted = Object.create(null);
while (pending.length) {
    var ready = pending.filter(function(child) {
        return child.rules.every(function(rule) {
            return !children[rule.parentWorkflowId] || emitted[rule.parentWorkflowId];
        });
    });
    if (!ready.length) {
        // Cycles are also rejected by admission. Avoid evaluating SQL or
        // dispatching any candidate whose ordering is blocked by a cycle.
        pending.forEach(function(child) {
            child.rules.forEach(function(rule) { rule.state = 'DEPENDENCY_CYCLE'; });
        });
        ordered = ordered.concat(pending); break;
    }
    ready.forEach(function(child) { emitted[child.workflowId] = true; ordered.push(child); });
    pending = pending.filter(function(child) { return !emitted[child.workflowId]; });
}
report.children = ordered;
report.children.forEach(function(child) {
    // Do not execute SQL conditions until EVERY parent/status/allow rule passes.
    if (child.rules.every(function(rule) { return rule.state === 'READY' || rule.state === 'SQL_CHECK'; })) {
        child.rules.forEach(function(rule) {
            if (rule.state !== 'SQL_CHECK') return;
            try {
                // [run_id] identifies the parent WORKFLOW execution for this rule;
                // no child execution exists at eligibility-evaluation time.
                var resolved = object(first(rows('CALL ' + app + '.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(?), ?)',
                    [JSON.stringify({jobType: 'SQL', sqlCommand: rule.sql}), rule.parentExecutionId])[0]));
                if (!resolved || !resolved.ok || !/^\s*(SELECT|WITH)\b/i.test(resolved.execSql))
                    throw new Error('SQL condition must resolve to a SELECT query');
                // A single subquery accepts SELECT/WITH and rejects DML, CALL and
                // scripting blocks. It must return one non-NULL Boolean cell.
                var condition = statement('SELECT * FROM (\n' + resolved.execSql + '\n) AS DEPENDENCY_CONDITION');
                var result = condition.execute();
                if (condition.getColumnCount() !== 1 || !result.next())
                    throw new Error('SQL condition must return exactly one BOOLEAN value');
                var value = result.getColumnValue(1);
                if (typeof value !== 'boolean' || result.next())
                    throw new Error('SQL condition must return exactly one BOOLEAN value');
                rule.state = value ? 'READY' : 'SKIPPED';
            } catch (error) {
                rule.state = 'EVALUATION_ERROR'; rule.error = String(error.message || error);
            }
        });
    }
    child.eligible = child.rules.length > 0 && child.rules.every(function(rule) { return rule.state === 'READY'; });
    child.state = child.eligible ? 'READY' : 'NOT_READY';
});
report.evaluated = true;
return report;
$$;
