"""Master configuration edits against an isolated PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine.configuration import Configuration,TRIAL_KEY
from task_engine.dependencies import Graphs,DependencyWorker
from task_engine.settings import Settings
from task_engine.store import Store,Conflict
from test_postgres_dependencies import diamond


def fixture():
    body=diamond();body['key']=TRIAL_KEY
    for node in body['nodes']:
        node['plan']['jobs']=[dict(key=node['key']+'-sql',sql='SELECT 1')]
    return body


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test')
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.dependency_graphs,kumo_task_engine.dependency_workers,kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,kumo_task_engine.runs,kumo_task_engine.events,kumo_task_engine.attempts RESTART IDENTITY CASCADE')
        self.graphs=Graphs(self.store)
        self.graph=self.graphs.install(fixture(),'IMPORTER')['graph']
        self.service=Configuration(self.store)

    def read(self):return self.service.read()
    def patch_workflow(self,**changes):
        return self.service.workflow('WF2',dict(revision=self.read()['revision'],**changes),'EDITOR')
    def complete(self,cascade,key,state='SUCCESS'):
        nodes=self.graphs.read(cascade)['nodes'];run=next(n['run_id'] for n in nodes if n['node_key']==key)
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state=%s,native_started_at=clock_timestamp(),ended_at=clock_timestamp(),next_check_at=NULL WHERE run_id=%s",(state,run))
            self.store.refresh(conn,run)
        return self.store.read(run)

    def test_edit_and_restart_preserve_master_configuration_and_audit(self):
        saved=self.patch_workflow(workflowName='WF2 revised',workflowGroup='Trial',description='Durable',jobExecutionMode='PARALLEL',enabled=False)
        restarted=Configuration(Store(self.store.settings)).read()
        self.assertEqual(saved,restarted)
        workflow=next(w for w in restarted['workflows'] if w['WORKFLOW_ID']=='WF2')
        self.assertEqual(workflow['WORKFLOW_NAME'],'WF2 revised');self.assertFalse(workflow['IS_ENABLED'])
        self.assertEqual(workflow['JOB_EXECUTION_MODE'],'PARALLEL')
        self.assertEqual([(r['revision'],r['changed_by']) for r in restarted['revisions']],[(2,'EDITOR'),(1,'IMPORTER')])
        self.assertEqual(len(restarted['workflows']),4)

    def test_concurrent_editors_have_one_winner_and_stale_noop_is_rejected(self):
        def edit(name):
            try:return self.service.workflow('WF2',dict(revision=1,description=name),name)['revision']
            except Conflict:return 'CONFLICT'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(edit,['A','B']))
        self.assertCountEqual(results,[2,'CONFLICT'])
        with self.assertRaises(Conflict):self.service.workflow('WF2',dict(revision=1),'STALE')
        self.assertEqual(len(self.read()['revisions']),2)

    def test_snowflake_reimport_cannot_overwrite_postgres_master(self):
        changed=fixture();changed['nodes'][0]['plan']['jobs'][0]['sql']='SELECT 99'
        with self.assertRaisesRegex(Conflict,'PostgreSQL owns'):
            self.graphs.install(changed,'IMPORTER',expected_revision=1)
        self.assertEqual(self.read()['revision'],1)

    def test_sql_job_settings_are_validated_and_saved_with_revision(self):
        data=self.service.job('WF2-sql',dict(revision=1,jobName='SQL revised',description='Example',jobLayer='RAW',jobType='SQL',sqlCommand='SELECT 42',timeoutSeconds='45',enabled=False),'EDITOR')
        job=next(j for j in data['jobs'] if j['JOB_ID']=='WF2-sql')
        self.assertEqual((job['JOB_NAME'],job['SQL_COMMAND'],job['TIMEOUT_SECONDS'],job['IS_ENABLED']),('SQL revised','SELECT 42',45,False))
        for changes in [dict(sqlCommand='DELETE FROM BUSINESS'),dict(timeoutSeconds=True),dict(timeoutSeconds=301),dict(jobType='DBT'),dict(enabled='false')]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.service.job('WF2-sql',dict(revision=2,**changes),'INVALID')
        self.assertEqual(self.read()['revision'],2)

    def test_invalid_workflow_settings_do_not_change_master(self):
        for changes in [dict(workflowName='WF1'),dict(workflowName=''),dict(workflowName=' '),dict(jobExecutionMode='MIXED'),dict(enabled='false'),dict(schedule='* * * * *'),dict(allowExternalTrigger=True),dict(allowManualTrigger=False)]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.patch_workflow(**changes)
        self.assertEqual(self.read()['revision'],1)

    def test_dependency_rules_add_remove_and_keep_per_parent_freshness(self):
        data=self.read()
        wf4=[r for r in data['rows'] if r['workflowId']=='WF4' and not r['jobId']]
        wf4[0].update(success=None,error=True,parentMustBeNewer=True)
        wf4[1].update(allow=False,parentMustBeNewer=False)
        changed=self.service.dependencies(dict(rows=data['rows'],revision=1),'RULE_EDITOR')
        rules=[r for r in changed['rows'] if r['workflowId']=='WF4' and not r['jobId']]
        self.assertEqual([(r['parentId'],r['parentMustBeNewer'],r['allow']) for r in rules],[('WF2',True,True),('WF3',False,False)])
        self.assertIsNone(rules[0]['success']);self.assertTrue(rules[0]['error'])
        # Remove WF3's parent, making it an independent root in the next revision.
        row=next(r for r in changed['rows'] if r['id']=='WF3')
        row.update(parentId=None,trigger=None,success=None,warning=None,error=None,parentMustBeNewer=False)
        self.service.dependencies(dict(rows=changed['rows'],revision=2),'RULE_EDITOR')
        self.assertIsNone(next(r for r in self.read()['rows'] if r['id']=='WF3')['parentId'])

    def test_cycles_duplicate_parents_external_references_and_unsupported_conditions_fail_atomically(self):
        for case in ['cycle','duplicate','external','condition','runner','assignment']:
            data=self.read();rows=data['rows'];wf2=next(r for r in rows if r['id']=='WF2')
            if case=='cycle':wf2['parentId']='WF4'
            if case=='duplicate':rows.append(dict(wf2,id='duplicate'))
            if case=='external':wf2['parentId']='WF5'
            if case=='condition':wf2['sql']='SELECT TRUE'
            if case=='runner':wf2['trigger']='RUNNER'
            if case=='assignment':next(r for r in rows if r['jobId'])['allow']=False
            with self.subTest(case=case),self.assertRaises(ValueError):self.service.dependencies(dict(rows=rows,revision=1),'INVALID')
            self.assertEqual(self.read()['revision'],1)

    def test_history_failure_rolls_back_the_configuration_edit(self):
        with patch('task_engine.configuration.record_revision',side_effect=RuntimeError('Injected disk error')),self.assertRaises(RuntimeError):
            self.patch_workflow(description='Must roll back')
        self.assertEqual(self.read()['revision'],1)
        self.assertEqual(next(w for w in self.read()['workflows'] if w['WORKFLOW_ID']=='WF2')['DESCRIPTION'],'')

    def test_active_group_keeps_jobs_rules_and_enabled_flags_from_accepted_revision(self):
        cascade=self.graphs.start(self.graph['id'],dict(requestId='before-edit'),'RUNNER')['cascadeId']
        original=deepcopy(self.graphs.read(cascade)['cascade']['snapshot'])
        worker=DependencyWorker(self.store);worker.step()
        self.service.job('WF2-sql',dict(revision=1,sqlCommand='SELECT 42',enabled=False),'EDITOR')
        self.patch_workflow(enabled=False)
        data=self.read();next(r for r in data['rows'] if r['id']=='WF2')['success']=False
        self.service.dependencies(dict(rows=data['rows'],revision=data['revision']),'EDITOR')
        self.assertEqual(self.graphs.read(cascade)['cascade']['snapshot'],original)
        self.complete(cascade,'WF1');DependencyWorker(Store(self.store.settings)).step()
        nodes={n['node_key']:n for n in self.graphs.read(cascade)['nodes']}
        self.assertEqual(nodes['WF2']['state'],'ADMITTED')
        self.assertEqual(self.store.read(nodes['WF2']['run_id'])['jobs'][0]['snapshot']['sql'],'SELECT 1')
        self.graphs.cancel(cascade);worker.step()
        second=self.graphs.start(self.graph['id'],dict(requestId='after-edit'),'RUNNER')['cascadeId']
        worker.step()
        self.assertEqual(next(n['state'] for n in self.graphs.read(second)['nodes'] if n['node_key']=='WF2'),'SKIPPED')
        self.assertEqual(self.graphs.read(second)['cascade']['graph_revision'],4)

    def test_disabled_sql_job_creates_no_native_attempt_and_blocks_dependents(self):
        self.service.job('WF1-sql',dict(revision=1,enabled=False),'EDITOR')
        cascade=self.graphs.start(self.graph['id'],dict(requestId='disabled-root'),'RUNNER')['cascadeId']
        DependencyWorker(self.store).step()
        result=self.graphs.read(cascade)
        self.assertEqual(result['cascade']['state'],'FAILED')
        self.assertEqual(next(n['state'] for n in result['nodes'] if n['node_key']=='WF1'),'SKIPPED')
        self.assertFalse(self.store.status()['runs'])

    def test_disabled_middle_job_rewires_prerequisites_and_reorder_updates_next_plan(self):
        # Install a fresh trial fixture with three jobs before marking it master.
        with self.store.connect() as conn:conn.execute('DELETE FROM kumo_task_engine.dependency_graphs')
        body=fixture();body['nodes'][0]['plan']['jobs']=[dict(key=k,sql='SELECT 1',enabled=k!='b') for k in ['a','b','c']]
        graph=self.graphs.install(body,'IMPORTER')['graph']
        self.service.orders(dict(revision=1,orders=[dict(workflowId='WF1',jobIds=['c','b','a'])]),'EDITOR')
        cascade=self.graphs.start(graph['id'],dict(requestId='middle-disabled'),'RUNNER')['cascadeId']
        DependencyWorker(self.store).step()
        run=next(n['run_id'] for n in self.graphs.read(cascade)['nodes'] if n['node_key']=='WF1')
        jobs=self.store.read(run)['jobs']
        self.assertEqual([(j['job_key'],j['depends_on']) for j in jobs],[('c',[]),('a',['c'])])
        with self.assertRaises(ValueError):self.service.orders(dict(revision=2,orders=[dict(workflowId='WF1',jobIds=['a'])]),'INVALID')

    def test_configured_failure_rule_affects_only_new_execution(self):
        data=self.read();next(r for r in data['rows'] if r['id']=='WF2')['error']=True
        self.service.dependencies(dict(rows=data['rows'],revision=1),'EDITOR')
        cascade=self.graphs.start(self.graph['id'],dict(requestId='accept-error'),'RUNNER')['cascadeId']
        worker=DependencyWorker(self.store);worker.step();self.complete(cascade,'WF1','FAILED');worker.step()
        nodes={n['node_key']:n for n in self.graphs.read(cascade)['nodes']}
        self.assertEqual(nodes['WF2']['state'],'ADMITTED');self.assertEqual(nodes['WF3']['state'],'BLOCKED')

    def test_configuration_and_idle_checks_never_connect_to_snowflake(self):
        import snowflake_client
        with patch.object(snowflake_client,'connection',side_effect=AssertionError('No Snowflake')):
            self.read();self.patch_workflow(description='PG only')
            for _ in range(3):self.assertFalse(DependencyWorker(self.store).step())
        self.assertFalse(self.store.status()['runs'])

    def test_v3_configuration_migration_is_repeatable_and_preserves_active_snapshot(self):
        cascade=self.graphs.start(self.graph['id'],dict(requestId='migration'),'RUNNER')['cascadeId']
        before=self.graphs.read(cascade)
        with self.store.connect() as conn:
            conn.execute('DROP TABLE kumo_task_engine.graph_revisions')
            conn.execute('ALTER TABLE kumo_task_engine.dependency_graphs DROP COLUMN configuration_managed')
            conn.execute('DELETE FROM kumo_task_engine.schema_version WHERE version=4')
        self.store.initialize();self.store.initialize()
        self.assertEqual(self.graphs.read(cascade),before)
        self.assertEqual(len(self.read()['revisions']),1)
        self.assertEqual(self.read()['revision'],1)


if __name__=='__main__':unittest.main()
