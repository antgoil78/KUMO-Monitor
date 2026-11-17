"""Exercise the evaluator's actual SQL and procedure body without Snowflake writes."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/migrations'))
from migration_support import procedure_parts

SOURCE = procedure_parts((ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_EVALUATE_DEPENDENCIES.sql').read_text())[1]
EVENT = '00000000-0000-0000-0000-000000000001'
NODE = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const queries = [];
let currentCondition;
function query(sql, binds) {
  queries.push({sql, binds});
  if (sql.includes('SP_GET_ENVIRONMENT')) return [{ENV:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP'}}];
  if (sql.startsWith('SELECT EXECUTION_ID')) return input.events;
  if (sql.startsWith('WITH workflow_definitions')) return input.rules || [];
  if (sql.includes('SP_RESOLVE_WORKFLOW_COMMAND')) {
    const text = JSON.parse(binds[0]).sqlCommand;
    currentCondition = input.conditions?.[text] || {};
    return [{RESOLVED:currentCondition.resolved || {ok:true,execSql:text}}];
  }
  if (sql.startsWith('SELECT * FROM (')) {
    if (currentCondition.error) throw Error(currentCondition.error);
    return currentCondition.rows || [{VALUE:true}];
  }
  throw Error('Unexpected query: ' + sql);
}
const snowflake = {createStatement({sqlText,binds=[]}) {
  let values=[], keys=[];
  return {execute() {
    values=query(sqlText,binds);
    keys=(sqlText.startsWith('SELECT * FROM (') && currentCondition.columns) || [...new Set(values.flatMap(row=>Object.keys(row)))];
    let index=-1;
    return {next(){return ++index<values.length},getColumnValue(n){return values[index][keys[n-1]]}};
  },getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]}};
}};
let result, error;
try { result = new Function('snowflake','P_EXECUTION_ID',input.source)(snowflake,input.executionId); }
catch (e) { error = e.message; }
process.stdout.write(JSON.stringify({result,error,queries}));
"""


def procedure(rules=(), **options):
    event = dict(EXECUTION_ID=EVENT, WORKFLOW_RUN_ID='a-run', WORKFLOW_ID=options.pop('sourceWorkflow', 'a'),
                 RUN_MODE='WORKFLOW', STATUS='SUCCESS', ENDED_AT='2026-11-17 10:00:00')
    inputs = dict(source=SOURCE, executionId=EVENT, events=[event], rules=list(rules))
    inputs.update(options)
    output = subprocess.run(['node', '-e', NODE], input=json.dumps(inputs),
                            text=True, capture_output=True, check=True)
    return json.loads(output.stdout)


def dependency_query():
    return next(row for row in procedure()['queries'] if row['sql'].startswith('WITH workflow_definitions'))


class EligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.query = dependency_query()['sql']

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE workflows(WORKFLOW_ID TEXT, WORKFLOW_NAME TEXT, IS_ENABLED BOOLEAN);
            CREATE TABLE logs(EXECUTION_ID TEXT, WORKFLOW_ID TEXT, JOB_ID TEXT, RUN_MODE TEXT,
                              STATUS TEXT, REQUESTED_AT TEXT, ENDED_AT TEXT, STARTED_AT TEXT);
            CREATE TABLE dependencies(WORKFLOW_ID TEXT, JOB_ID TEXT, PARENT_ID TEXT, TRIGGER_TYPE TEXT,
                                      SUCCESS BOOLEAN, WARNING BOOLEAN, ERROR BOOLEAN, SQL TEXT, ALLOW BOOLEAN, PARENT_MUST_BE_NEWER BOOLEAN DEFAULT FALSE);
            INSERT INTO workflows VALUES ('a','Parent A',TRUE),('b','Parent B',TRUE),('c','Child',TRUE);
            INSERT INTO logs(EXECUTION_ID,WORKFLOW_ID,JOB_ID,RUN_MODE,STATUS,REQUESTED_AT,ENDED_AT) VALUES ('a-run','a',NULL,'WORKFLOW','SUCCESS','2026-11-17 09:00:00','2026-11-17 09:01:00'),
                                    ('b-run','b',NULL,'WORKFLOW','SUCCESS','2026-11-17 09:00:00','2026-11-17 09:01:00');
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW) VALUES ('c',NULL,'a','DEP',TRUE,FALSE,FALSE,NULL,TRUE),
                                            ('c',NULL,'b','DEP',TRUE,FALSE,FALSE,NULL,TRUE);
        ''')
        view = (ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/views/V_ORCH_WORKFLOW_RELATIONS.sql').read_text()
        definition = view[view.index('WITH workflow_rules AS'):].replace('KUMO_DEV.MONITOR_META.DEPENDENCIES', 'dependencies')
        self.db.executescript('CREATE VIEW fixture_relations(WORKFLOW_ID,PARENT_ID,UPSTREAM_WORKFLOW_ID,DEPTH) AS ' + definition)

    def evaluate(self, **options):
        source = options.pop('sourceWorkflow', 'a')
        sql = self.query
        for table in ('workflows', 'logs', 'dependencies', 'fixture_relations'):
            sql = sql.replace('IDENTIFIER(?)', table, 1)
        rules = [dict(row) for row in self.db.execute(sql, (source,))]
        for rule in rules:
            rule['PARENT_MUST_BE_NEWER'] = bool(rule['PARENT_MUST_BE_NEWER'])
        output = procedure(rules, sourceWorkflow=source, **options)
        self.assertNotIn('error', output, output)
        # No DDL, DML, task start or request-admission calls are permitted.
        self.assertTrue(all(row['sql'].startswith(('SELECT ', 'WITH ', 'CALL KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT',
                                                 'CALL KUMO_DEV.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND'))
                            for row in output['queries']))
        return output

    def child(self, **options):
        return next(child for child in self.evaluate(**options)['result']['children'] if child['workflowId'] == 'c')

    def test_all_parent_rules_must_pass_and_parent_run_ids_are_reported(self):
        child = self.child()
        self.assertTrue(child['eligible'])
        self.assertEqual({rule['parentExecutionId'] for rule in child['rules']}, {'a-run', 'b-run'})
        self.db.execute("UPDATE logs SET STATUS='FAILED' WHERE WORKFLOW_ID='b'")
        self.assertFalse(self.child()['eligible'])

    def add_grandparent(self):
        self.db.executescript("""
            INSERT INTO workflows VALUES ('g','Grandparent',TRUE);
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW)
            VALUES ('a',NULL,'g','DEP',TRUE,FALSE,FALSE,NULL,TRUE);
        """)

    def test_parent_and_grandparent_active_states_block_before_sql(self):
        self.add_grandparent()
        self.db.execute("UPDATE dependencies SET SQL='SELECT TRUE' WHERE WORKFLOW_ID='c'")
        for workflow, expected in [('a', 'PARENT_BUSY'), ('g', 'GRANDPARENT_BUSY')]:
            for status in ('PENDING', 'INITIATING', 'QUEUED', 'CLAIMED', 'RUNNING'):
                with self.subTest(workflow=workflow, status=status):
                    # This active run predates the latest successful parent.
                    self.db.execute("DELETE FROM logs WHERE EXECUTION_ID='upstream-active'")
                    self.db.execute("INSERT INTO logs VALUES ('upstream-active',?,NULL,'WORKFLOW',?,'2026-11-17 08:00:00',NULL,'2026-11-17 08:01:00')", (workflow, status))
                    output = self.evaluate()
                    child = next(c for c in output['result']['children'] if c['workflowId'] == 'c')
                    self.assertFalse(child['eligible'])
                    self.assertEqual(child['rules'][0]['state'], expected)
                    self.assertFalse(any('SP_RESOLVE_WORKFLOW_COMMAND' in q['sql'] for q in output['queries']))
                    if workflow == 'g':
                        self.assertEqual(child['rules'][0]['busyGrandparentCount'], 1)
                    else:
                        self.assertTrue(child['rules'][0]['parentBusy'])

    def test_activity_stops_at_grandparents_and_excludes_job_rows(self):
        self.add_grandparent()
        self.db.executescript("""
            INSERT INTO workflows VALUES ('h','Great grandparent',TRUE);
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE)
            VALUES ('g',NULL,'h','DEP'), ('b','job','h','DEP');
            INSERT INTO logs VALUES ('h-active','h',NULL,'WORKFLOW','RUNNING','2026-11-17 10:00:00',NULL,'2026-11-17 10:00:01');
            INSERT INTO logs VALUES ('g-job','g','job','WORKFLOW','RUNNING','2026-11-17 10:00:00',NULL,'2026-11-17 10:00:01');
        """)
        self.assertTrue(self.child()['eligible'])
        # A partial workflow execution is still an active workflow root.
        self.db.execute("INSERT INTO logs VALUES ('g-partial','g',NULL,'JOB_ONLY','RUNNING','2026-11-17 10:00:00',NULL,'2026-11-17 10:00:01')")
        self.assertEqual(self.child()['rules'][0]['state'], 'GRANDPARENT_BUSY')

    def test_all_grandparents_are_checked_and_completion_rechecks_grandchildren(self):
        self.add_grandparent()
        self.db.executescript("""
            INSERT INTO workflows VALUES ('h','Other grandparent',TRUE);
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW)
            VALUES ('a',NULL,'h','DEP',TRUE,FALSE,FALSE,NULL,TRUE);
            INSERT INTO logs VALUES ('g-run','g',NULL,'WORKFLOW','RUNNING','2026-11-17 08:00:00',NULL,'2026-11-17 08:01:00');
            INSERT INTO logs VALUES ('h-run','h',NULL,'WORKFLOW','RUNNING','2026-11-17 08:00:00',NULL,'2026-11-17 08:01:00');
        """)
        self.assertEqual(self.child()['rules'][0]['busyGrandparentCount'], 2)
        self.db.execute("UPDATE logs SET STATUS='SUCCESS',ENDED_AT='2026-11-17 10:00:00'")
        output = self.evaluate(sourceWorkflow='g')['result']
        self.assertEqual([c['workflowId'] for c in output['children']], ['a', 'c'])
        self.assertTrue(next(c for c in output['children'] if c['workflowId'] == 'c')['eligible'])
        # No direct or grandchild connection means no recheck for c.
        self.db.execute("UPDATE dependencies SET PARENT_ID='b' WHERE WORKFLOW_ID='a'")
        self.assertEqual(self.evaluate(sourceWorkflow='g')['result']['children'], [])

    def test_parent_precedes_child_even_if_ids_sort_in_reverse_and_cycles_do_not_loop(self):
        self.db.executescript("""
            INSERT INTO workflows VALUES ('z','Upstream',TRUE);
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW)
            VALUES ('z',NULL,'a','DEP',TRUE,FALSE,FALSE,NULL,TRUE);
            UPDATE dependencies SET PARENT_ID='z' WHERE WORKFLOW_ID='c' AND PARENT_ID='a';
            INSERT INTO logs VALUES ('z-run','z',NULL,'WORKFLOW','SUCCESS','2026-11-17 09:00:00','2026-11-17 09:01:00','2026-11-17 09:00:01');
        """)
        report = self.evaluate()['result']
        self.assertEqual([c['workflowId'] for c in report['children']], ['z', 'c'])
        self.db.execute("INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE) VALUES ('z',NULL,'c','DEP')")
        report = self.evaluate()['result']
        self.assertTrue(all(not c['eligible'] for c in report['children']))
        self.assertTrue(all(rule['state'] == 'DEPENDENCY_CYCLE' for c in report['children'] for rule in c['rules']))

    def test_freshness_first_run_and_per_rule_opt_out(self):
        self.db.execute('UPDATE dependencies SET PARENT_MUST_BE_NEWER=TRUE')
        self.assertTrue(self.child()['eligible'])  # First child run.
        self.db.execute("INSERT INTO logs VALUES ('child-old','c',NULL,'WORKFLOW','SUCCESS','2026-11-17 09:02:00','2026-11-17 09:10:00','2026-11-17 09:03:00')")
        self.assertEqual({rule['state'] for rule in self.child()['rules']}, {'STALE_PARENT'})
        self.db.execute("UPDATE dependencies SET PARENT_MUST_BE_NEWER=FALSE WHERE PARENT_ID='a'")
        self.assertEqual(self.child()['rules'][0]['state'], 'READY')
        self.assertFalse(self.child()['eligible'])
        self.db.execute("UPDATE logs SET ENDED_AT='2026-11-17 09:04:00' WHERE WORKFLOW_ID='b'")
        self.assertTrue(self.child()['eligible'])

    def test_freshness_is_strict_and_compares_with_start_instead_of_completion(self):
        self.db.execute('UPDATE dependencies SET PARENT_MUST_BE_NEWER=TRUE')
        self.db.execute("INSERT INTO logs VALUES ('child-old','c',NULL,'WORKFLOW','FAILED','2026-11-17 09:00:00','2026-11-17 09:10:00','2026-11-17 09:00:30')")
        # Parents completed after the child's start but before its completion.
        self.assertTrue(self.child()['eligible'])
        self.db.execute("UPDATE logs SET ENDED_AT='2026-11-17 09:00:30' WHERE WORKFLOW_ID='b'")
        self.assertEqual(self.child()['rules'][1]['state'], 'STALE_PARENT')
        # A never-started cancelled request must not erase the previous cutoff.
        self.db.execute("INSERT INTO logs VALUES ('cancelled','c',NULL,'WORKFLOW','CANCELLED','2026-11-17 10:00:00','2026-11-17 10:00:01',NULL)")
        self.assertEqual(self.child()['rules'][1]['state'], 'STALE_PARENT')
        self.db.execute("UPDATE dependencies SET SQL='SELECT bad'")
        output = self.evaluate()
        self.assertFalse(any('SP_RESOLVE_WORKFLOW_COMMAND' in row['sql'] for row in output['queries']))
        rule = output['result']['children'][0]['rules'][1]
        self.assertTrue(rule['parentMustBeNewer'])
        self.assertEqual(rule['childLastStartedAt'], '2026-11-17 09:00:30')

    def test_latest_run_includes_queued_running_and_failed_instead_of_old_success(self):
        for status in ('QUEUED', 'CLAIMED', 'RUNNING', 'FAILED'):
            with self.subTest(status=status):
                self.db.execute("DELETE FROM logs WHERE EXECUTION_ID='new-b'")
                self.db.execute("INSERT INTO logs(EXECUTION_ID,WORKFLOW_ID,JOB_ID,RUN_MODE,STATUS,REQUESTED_AT,ENDED_AT) VALUES ('new-b','b',NULL,'WORKFLOW',?,'2026-11-17 10:00:00',?)",
                                (status, '2026-11-17 10:01:00' if status == 'FAILED' else None))
                child = self.child()
                rule = next(rule for rule in child['rules'] if rule['parentWorkflowId'] == 'b')
                self.assertFalse(child['eligible'])
                self.assertEqual(rule['parentExecutionId'], 'new-b')
                self.assertEqual(rule['state'], 'BLOCKED' if status == 'FAILED' else 'WAITING')

    def test_completed_job_cannot_release_a_workflow_that_is_still_running(self):
        self.db.execute("UPDATE logs SET STATUS='RUNNING',ENDED_AT=NULL WHERE WORKFLOW_ID='a'")
        child = self.child()
        self.assertFalse(child['eligible'])
        self.assertEqual(child['rules'][0]['state'], 'WAITING')

    def test_latest_timestamp_ties_use_execution_id_deterministically(self):
        self.db.execute("INSERT INTO logs(EXECUTION_ID,WORKFLOW_ID,JOB_ID,RUN_MODE,STATUS,REQUESTED_AT,ENDED_AT) VALUES ('z-run','b',NULL,'WORKFLOW','FAILED','2026-11-17 09:00:00','end')")
        child = self.child()
        self.assertFalse(child['eligible'])
        self.assertEqual(child['rules'][1]['parentExecutionId'], 'z-run')

    def test_missing_unfinished_skipped_cancelled_and_partial_parents_do_not_release(self):
        for status, mode, ended, expected in [('SUCCESS','WORKFLOW',None,'WAITING'),
                ('SKIPPED','WORKFLOW','end','BLOCKED'), ('CANCELLED','WORKFLOW','end','BLOCKED'),
                ('SUCCESS','JOB_ONLY','end','PARTIAL_PARENT_RUN'), ('SUCCESS','FROM_JOB','end','PARTIAL_PARENT_RUN')]:
            with self.subTest(status=status, mode=mode, ended=ended):
                self.db.execute("UPDATE logs SET STATUS=?,RUN_MODE=?,ENDED_AT=? WHERE WORKFLOW_ID='b'", (status, mode, ended))
                child = self.child()
                self.assertFalse(child['eligible'])
                self.assertEqual(child['rules'][1]['state'], expected)
        self.db.execute("DELETE FROM logs WHERE WORKFLOW_ID='b'")
        self.assertEqual(self.child()['rules'][1]['state'], 'WAITING')

    def test_false_rejects_null_ignores_and_failed_uses_error_flag(self):
        for status, flag in [('SUCCESS','SUCCESS'), ('WARNING','WARNING'), ('FAILED','ERROR'), ('ERROR','ERROR')]:
            for value in (True, False, None):
                with self.subTest(status=status, value=value):
                    self.db.execute("UPDATE logs SET STATUS=? WHERE WORKFLOW_ID='b'", (status,))
                    self.db.execute(f'UPDATE dependencies SET "{flag}"=? WHERE PARENT_ID=\'b\'', (value,))
                    self.assertEqual(self.child()['eligible'], value is not False)

    def test_job_assignments_and_external_or_runner_children_are_not_candidates(self):
        self.db.executescript('''
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW) VALUES ('c','job','a','DEP',FALSE,FALSE,FALSE,'SELECT FALSE',FALSE);
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW) VALUES ('external',NULL,'a',NULL,TRUE,TRUE,TRUE,NULL,TRUE);
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW) VALUES ('runner',NULL,'a','RUNNER',TRUE,TRUE,TRUE,NULL,TRUE);
            INSERT INTO logs(EXECUTION_ID,WORKFLOW_ID,JOB_ID,RUN_MODE,STATUS,REQUESTED_AT,ENDED_AT) VALUES ('job','b','some-job','WORKFLOW','FAILED','2026-11-17 11:00:00','end');
        ''')
        output = self.evaluate()['result']
        self.assertEqual(len(output['children']), 1)
        self.assertEqual(len(output['children'][0]['rules']), 2)
        self.assertTrue(output['children'][0]['eligible'])

    def test_disabled_disallowed_and_active_children_are_not_eligible(self):
        self.db.execute("UPDATE workflows SET IS_ENABLED=FALSE WHERE WORKFLOW_ID='c'")
        self.assertEqual(self.child()['rules'][0]['state'], 'DISABLED')
        self.db.execute("UPDATE workflows SET IS_ENABLED=TRUE WHERE WORKFLOW_ID='c'")
        self.db.execute("UPDATE dependencies SET ALLOW=FALSE WHERE PARENT_ID='b'")
        self.assertFalse(self.child()['eligible'])
        self.db.execute('UPDATE dependencies SET ALLOW=TRUE')
        # Check ANY active run, even if a later request has already failed.
        self.db.executescript('''
            INSERT INTO logs(EXECUTION_ID,WORKFLOW_ID,JOB_ID,RUN_MODE,STATUS,REQUESTED_AT,ENDED_AT) VALUES ('active','c',NULL,'WORKFLOW','RUNNING','2026-11-17 09:00:00',NULL);
            INSERT INTO logs(EXECUTION_ID,WORKFLOW_ID,JOB_ID,RUN_MODE,STATUS,REQUESTED_AT,ENDED_AT) VALUES ('later','c',NULL,'WORKFLOW','FAILED','2026-11-17 10:00:00','end');
        ''')
        self.assertEqual(self.child()['rules'][0]['state'], 'BUSY')

    def test_duplicate_missing_and_mixed_trigger_configuration_fails_closed(self):
        self.db.execute("INSERT INTO dependencies SELECT * FROM dependencies WHERE PARENT_ID='b'")
        self.assertFalse(self.child()['eligible'])
        self.assertEqual(self.child()['rules'][1]['state'], 'DUPLICATE_RULE')
        self.db.execute("DELETE FROM dependencies WHERE PARENT_ID='b'")
        self.db.execute("INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW) VALUES ('c',NULL,'unknown','DEP',TRUE,TRUE,TRUE,NULL,TRUE)")
        self.assertEqual(self.child()['rules'][1]['state'], 'INVALID_REFERENCE')
        self.db.execute("UPDATE dependencies SET PARENT_ID='b',TRIGGER_TYPE='RUNNER' WHERE PARENT_ID='unknown'")
        self.assertEqual(self.child()['rules'][1]['state'], 'INVALID_TRIGGER')

    def test_conditions_wait_for_every_status_rule_and_every_sql_must_pass(self):
        self.db.execute("UPDATE dependencies SET SQL='SELECT TRUE'")
        self.db.execute("UPDATE logs SET STATUS='RUNNING',ENDED_AT=NULL WHERE WORKFLOW_ID='b'")
        output = self.evaluate()
        self.assertFalse(any('SP_RESOLVE_WORKFLOW_COMMAND' in row['sql'] for row in output['queries']))
        self.db.execute("UPDATE logs SET STATUS='SUCCESS',ENDED_AT='end' WHERE WORKFLOW_ID='b'")
        self.assertTrue(self.child()['eligible'])
        self.db.execute("UPDATE dependencies SET SQL='SELECT FALSE' WHERE PARENT_ID='b'")
        self.assertFalse(self.child(conditions={'SELECT FALSE': {'rows': [{'VALUE': False}]}})['eligible'])

    def test_sql_errors_null_non_boolean_or_wrong_shape_never_release_child(self):
        self.db.execute("UPDATE dependencies SET SQL='SELECT bad' WHERE PARENT_ID='b'")
        for condition in ({'error': 'Query failed'}, {'rows': []}, {'rows': [{'V': True}, {'V': True}]},
                          {'rows': [{'V': None}]}, {'rows': [{'V': 1}]}, {'rows': [{'V': True,'W': True}]},
                          {'rows': [{'V': True}], 'columns': ['V','V']},
                          {'resolved': {'ok': False}}, {'resolved': {'ok': True, 'execSql': 'DELETE FROM t'}}):
            with self.subTest(condition=condition):
                child = self.child(conditions={'SELECT bad': condition})
                self.assertFalse(child['eligible'])
                self.assertEqual(child['rules'][1]['state'], 'EVALUATION_ERROR')

    def test_condition_error_isolated_to_its_child_and_resolver_receives_parent_run(self):
        self.db.execute("UPDATE dependencies SET SQL='SELECT bad' WHERE PARENT_ID='b'")
        self.db.executescript('''
            INSERT INTO workflows VALUES ('d','Other child',TRUE);
            INSERT INTO dependencies(WORKFLOW_ID,JOB_ID,PARENT_ID,TRIGGER_TYPE,SUCCESS,WARNING,ERROR,SQL,ALLOW) VALUES ('d',NULL,'a','DEP',TRUE,FALSE,FALSE,NULL,TRUE);
        ''')
        output = self.evaluate(conditions={'SELECT bad': {'error': 'Invalid condition'}})
        children = output['result']['children']
        self.assertFalse(children[0]['eligible'])
        self.assertTrue(children[1]['eligible'])
        resolver = next(row for row in output['queries'] if 'SP_RESOLVE_WORKFLOW_COMMAND' in row['sql'])
        self.assertEqual(resolver['binds'][1], 'b-run')

    def test_unfinished_or_partial_source_is_not_evaluated_and_unknown_source_rejected(self):
        for mode, status, ended in [('WORKFLOW','RUNNING',None), ('JOB_ONLY','SUCCESS','end'), ('FROM_JOB','SUCCESS','end')]:
            output = procedure(events=[dict(WORKFLOW_ID='a',WORKFLOW_RUN_ID='a-run',RUN_MODE=mode,STATUS=status,ENDED_AT=ended)])
            self.assertFalse(output['result']['evaluated'])
            self.assertEqual(output['result']['children'], [])
            self.assertFalse(any(row['sql'].startswith('WITH ') for row in output['queries']))
        self.assertIn('not found', procedure(events=[])['error'])
        self.assertIn('UUID', procedure(executionId="'; DELETE FROM t")['error'])


if __name__ == '__main__':
    unittest.main()
