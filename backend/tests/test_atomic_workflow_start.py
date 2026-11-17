"""Exercise the real start gate SQL, task launcher and executor claim boundary."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/migrations'))
sys.path.insert(0, str(ROOT / 'backend/tests'))
from migration_support import procedure_parts
import test_post_execution_dependencies as completion

VIEW = ROOT / 'snowflake/ddl/KUMO_DEV/MONITOR_APP/views/V_ORCH_WORKFLOW_RELATIONS.sql'


class AtomicClaimTests(unittest.TestCase):
    def executor(self, **options):
        return completion.CompletionTests().execute(**options)

    def test_blocked_runs_never_claim_or_execute_for_any_trigger_or_mode(self):
        blocker = dict(EXECUTION_ID='upstream', WORKFLOW_ID='parent', STATUS='RUNNING', DIRECTION='UPSTREAM')
        for trigger in ('MANUAL', 'SCHEDULED', 'DEPENDENCY'):
            for mode in ('WORKFLOW', 'JOB_ONLY', 'FROM_JOB'):
                with self.subTest(trigger=trigger, mode=mode):
                    output = self.executor(triggerSource=trigger, runMode=mode, blockers=[blocker])
                    self.assertNotIn('error', output)
                    self.assertTrue(output['result']['waiting'])
                    self.assertEqual(output['root']['STATUS'], 'QUEUED')
                    self.assertEqual(output['root']['RESULT']['startGate']['blockers'], [blocker])
                    self.assertFalse(any("SET STATUS = 'RUNNING'" in q['sql'] or q['sql'].startswith('EXECUTE IMMEDIATE') for q in output['queries']))
                    self.assertEqual(output['calls'], [])

    def test_lock_check_and_claim_are_in_one_transaction_and_work_is_after_commit(self):
        output = self.executor()
        queries = [q['sql'] for q in output['queries']]
        begin = queries.index('BEGIN')
        lock = next(i for i,q in enumerate(queries) if '.DEPENDENCIES SET ALLOW = ALLOW' in q)
        gate = next(i for i,q in enumerate(queries) if q.startswith('WITH relations AS'))
        claim = next(i for i,q in enumerate(queries) if "SET STATUS = 'RUNNING'" in q)
        commit = queries.index('COMMIT', claim)
        workload = next(i for i,q in enumerate(queries) if q.startswith('EXECUTE IMMEDIATE'))
        self.assertLess(begin, lock)
        self.assertLess(lock, gate)
        self.assertLess(gate, claim)
        self.assertLess(claim, commit)
        self.assertLess(commit, workload)
        self.assertNotIn('COMMIT', queries[lock:claim])

    def test_gate_errors_fail_closed_before_any_claim(self):
        output = self.executor(gateFails=True)
        self.assertIn('Start gate unavailable', output['error'])
        self.assertEqual(output['root']['STATUS'], 'QUEUED')
        self.assertTrue(any(q['sql']=='ROLLBACK' for q in output['queries']))
        self.assertFalse(any(q['sql'].startswith('EXECUTE IMMEDIATE') for q in output['queries']))

    def test_duplicate_invocation_observes_running_and_does_not_execute_work(self):
        output = self.executor(initialStatus='RUNNING')
        self.assertTrue(output['result']['reused'])
        self.assertEqual(output['root']['STATUS'], 'RUNNING')
        self.assertFalse(any("SET STATUS = 'RUNNING'" in q['sql'] or q['sql'].startswith('EXECUTE IMMEDIATE') for q in output['queries']))


class ActivitySqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        output = completion.CompletionTests().execute()
        cls.query = next(q['sql'] for q in output['queries'] if q['sql'].startswith('WITH relations AS'))

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE dependencies(WORKFLOW_ID TEXT, JOB_ID TEXT, PARENT_ID TEXT);
            INSERT INTO dependencies VALUES ('b',NULL,'a'),('c',NULL,'b'),('d',NULL,'c'),('a','job','unrelated');
            CREATE TABLE runs(EXECUTION_ID TEXT, WORKFLOW_ID TEXT, JOB_ID TEXT, STATUS TEXT);
        ''')
        view = VIEW.read_text()
        definition = view[view.index('WITH workflow_rules AS'):].replace('KUMO_DEV.MONITOR_META.DEPENDENCIES', 'dependencies')
        self.db.executescript('CREATE VIEW fixture_relations(WORKFLOW_ID,PARENT_ID,UPSTREAM_WORKFLOW_ID,DEPTH) AS ' + definition)

    def blockers(self, workflow):
        query = self.query.replace('IDENTIFIER(?)','fixture_relations',1).replace('IDENTIFIER(?)','runs',1)
        return [dict(r) for r in self.db.execute(query,(workflow,'self',workflow,workflow))]

    def test_all_active_parent_and_grandparent_states_block(self):
        for wf in ('a','b'):
            for status in ('PENDING','INITIATING','QUEUED','CLAIMED','RUNNING'):
                with self.subTest(workflow=wf,status=status):
                    self.db.execute('DELETE FROM runs')
                    self.db.execute('INSERT INTO runs VALUES (?,?,NULL,?)',('other',wf,status))
                    result = self.blockers('c')
                    self.assertEqual(len(result),1)
                    self.assertEqual(result[0]['DIRECTION'],'UPSTREAM')

    def test_running_descendant_blocks_upstream_and_queued_descendant_does_not(self):
        self.db.execute("INSERT INTO runs VALUES ('child','c',NULL,'RUNNING')")
        self.assertEqual(self.blockers('a')[0]['DIRECTION'],'DOWNSTREAM')
        self.assertEqual(self.blockers('b')[0]['DIRECTION'],'DOWNSTREAM')
        self.db.execute("UPDATE runs SET STATUS='QUEUED'")
        self.assertEqual(self.blockers('a'),[])
        self.assertEqual(self.blockers('b'),[])
        self.assertEqual(self.blockers('c'),[])  # own queued run is not a relation.

    def test_queued_upstream_wins_without_deadlocking_queued_descendant(self):
        self.db.executescript("INSERT INTO runs VALUES ('parent','a',NULL,'QUEUED'),('child','c',NULL,'QUEUED');")
        self.assertEqual(self.blockers('a'),[])
        self.assertEqual(len(self.blockers('c')),1)
        self.db.execute("UPDATE runs SET STATUS='RUNNING' WHERE WORKFLOW_ID='a'")
        self.assertEqual(len(self.blockers('c')),1)
        self.db.execute("UPDATE runs SET STATUS='SUCCESS' WHERE WORKFLOW_ID='a'")
        self.assertEqual(self.blockers('c'),[])

    def test_depth_boundary_jobs_siblings_and_terminal_runs_are_excluded(self):
        self.db.executescript("""
          INSERT INTO runs VALUES ('deep','d',NULL,'RUNNING'),('job','b','job','RUNNING'),
              ('unrelated','unrelated',NULL,'RUNNING'),('done','c',NULL,'SUCCESS');
        """)
        self.assertEqual(self.blockers('a'),[])  # d is a great-grandchild.
        self.db.executescript("DELETE FROM runs; INSERT INTO runs VALUES ('parent','a',NULL,'RUNNING');")
        self.assertEqual(self.blockers('d'),[])  # a is a great-grandparent.


LAUNCHER = r"""
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const wf='00000000-0000-0000-0000-000000000001',run='00000000-0000-0000-0000-000000000002';
const task='KUMO_DEV.MONITOR_APP.TMP_ORCH_WF_'+wf.replaceAll('-','_')+'_'+run.replaceAll('-','_');
const root={EXECUTION_ID:run,WORKFLOW_ID:wf,STATUS:input.status||'QUEUED',TASK_FQN:task,CONFIG_SNAPSHOT:{taskWarehouse:'WH'}};
const queries=[];
function query(sql,b){
 queries.push({sql,binds:b});
 if(sql.includes('SP_GET_ENVIRONMENT'))return [{ENV:{DATABASE:'KUMO_DEV',META_SCHEMA:'MONITOR_META',MONITOR_APP_SCHEMA:'MONITOR_APP'}}];
 if(sql.startsWith('SELECT * FROM'))return [root];
 if(sql.startsWith('SELECT EXECUTION_ID'))return input.sameWorkflowRunning?[{EXECUTION_ID:'other'}]:[];
 if(sql.startsWith('SELECT STATUS'))return [{STATUS:root.STATUS}];
 if(sql.startsWith('SHOW TASKS'))return [{name:task.split('.')[2],definition:"CALL KUMO_DEV.MONITOR_APP.SP_ORCH_EXECUTE_WORKFLOW('"+run+"')",schedule:null,comment:'KUMO parallel orchestration v2: temporary workflow run '+run}];
 if(sql.startsWith('UPDATE')||sql.startsWith('CREATE TASK')||sql.startsWith('EXECUTE TASK'))return [];
 throw Error('Unexpected SQL: '+sql);
}
const snowflake={createStatement({sqlText,binds=[]}){let values=[],keys=[];return {
 execute(){values=query(sqlText,binds);keys=[...new Set(values.flatMap(r=>Object.keys(r)))];let i=-1;return {next(){return ++i<values.length},getColumnValue(n){return values[i][keys[n-1]]}};},
 getColumnCount(){return keys.length},getColumnName(n){return keys[n-1]}};}};
const result=new Function('snowflake','P_RUN_ID',input.source)(snowflake,run);
process.stdout.write(JSON.stringify({result,queries,root}));
"""


class LauncherTests(unittest.TestCase):
    def launch(self, **options):
        source=procedure_parts((ROOT/'snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_START_RUN.sql').read_text())[1]
        output=subprocess.run(['node','-e',LAUNCHER],input=json.dumps(dict(source=source,**options)),text=True,capture_output=True,check=True)
        return json.loads(output.stdout)

    def test_launcher_only_wakes_and_never_claims_the_root(self):
        output=self.launch()
        self.assertTrue(output['result']['wakeupScheduled'])
        self.assertEqual(output['root']['STATUS'],'QUEUED')
        self.assertFalse(any(q['sql'] in ('BEGIN','COMMIT') or "SET STATUS = 'CLAIMED'" in q['sql'] or "SET STATUS = 'RUNNING'" in q['sql'] for q in output['queries']))

    def test_repeated_and_terminal_wakeups_do_not_replay_work(self):
        for options in ({'status':'SUCCESS'},{'status':'RUNNING'},{'sameWorkflowRunning':True}):
            with self.subTest(options=options):
                output=self.launch(**options)
                self.assertFalse(output['result']['wakeupScheduled'])
                self.assertFalse(any(q['sql'].startswith('EXECUTE TASK') for q in output['queries']))
