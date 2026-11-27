"""Manual previews follow real admission scopes without creating executions."""
import os
import unittest
import uuid
from unittest.mock import patch

import test_postgres_monitor as fixtures
from task_engine.integration import blueprint
from flask import Flask


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'),'Requires isolated PostgreSQL test database')
class RunPlanTests(unittest.TestCase):
    setUp=fixtures.PostgresMonitorTests.setUp
    start=fixtures.PostgresMonitorTests.start

    def preview(self,name='WF1',children=True):
        return self.monitor.preview(self.graph['id'],self.ids[name],children)

    def test_preview_matches_the_admitted_join_and_does_not_create_work(self):
        tables=['cascades','cascade_nodes','runs','attempts','dependency_work','dependency_decisions']
        def counts():
            with self.store.connect() as conn:
                return {t:conn.execute('SELECT count(*) AS n FROM kumo_task_engine.'+t).fetchone()['n'] for t in tables}
        before=counts()
        plan=self.preview()
        self.assertEqual({n['name'] for n in plan['nodes']},{'WF1','WF2','WF3','WF4'})
        join=next(n for n in plan['nodes'] if n['name']=='WF4')
        self.assertEqual({p['name'] for p in join['parents']},{'WF2','WF3'})
        self.assertTrue(all(p['inPlan'] for p in join['parents']))
        self.assertEqual(counts(),before)
        for node in plan['nodes']:
            for job in node['jobs']:
                self.assertNotIn('sql',job);self.assertNotIn('command',job)
        group=self.start();saved=self.graphs.read(group)['cascade']['snapshot']
        self.assertEqual({n['key'] for n in plan['nodes']},{n['key'] for n in saved['nodes']})
        for node in plan['nodes']:
            actual=next(n for n in saved['nodes'] if n['key']==node['key'])
            self.assertEqual([p['key'] for p in node['parents']],[p['key'] for p in actual['parents']])

    def test_middle_entry_bypasses_own_parents_and_labels_external_join_parent(self):
        plan=self.preview('WF2');entry=next(n for n in plan['nodes'] if n['key']==plan['rootKey'])
        self.assertEqual(entry['parents'],[])
        join=next(n for n in plan['nodes'] if n['name']=='WF4')
        self.assertEqual({p['name']:p['inPlan'] for p in join['parents']},{'WF2':True,'WF3':False})
        own=self.preview('WF2',False)
        self.assertEqual([n['name'] for n in own['nodes']],['WF2'])
        self.assertEqual(own['nodes'][0]['parents'],[])

    def test_disabled_parent_is_omitted_and_ancestor_rule_is_visible(self):
        self.config.workflow(self.ids['WF2'],dict(revision=self.config.read()['revision'],enabled=False),'EDITOR')
        plan=self.preview()
        self.assertEqual({n['name'] for n in plan['nodes']},{'WF1','WF3','WF4'})
        self.assertEqual([n['name'] for n in plan['omitted']],['WF2'])
        join=next(n for n in plan['nodes'] if n['name']=='WF4')
        self.assertEqual({p['name'] for p in join['parents']},{'WF1','WF3'})
        self.assertTrue(all('parentMustBeNewer' in p and 'success' in p for p in join['parents']))

    def test_unsupported_workflow_remains_visible_with_its_reason(self):
        with patch.dict(os.environ,{'KUMO_PG_BUSINESS_JOBS_ENABLED':'false'}):
            self.config.recompile('TEST')
            plan=self.preview('DBT workflow')
        self.assertEqual([n['name'] for n in plan['nodes']],['DBT workflow'])
        self.assertTrue(plan['issues']);self.assertTrue(plan['nodes'][0]['issues'])

    def test_read_only_route_validates_scope_and_catalog_identity(self):
        app=Flask(__name__);app.register_blueprint(blueprint);client=app.test_client()
        url=f'/api/orchestration/task-engine/graphs/{self.graph["id"]}/plan'
        with patch('task_engine.integration.store',return_value=self.store):
            response=client.get(url,query_string=dict(workflowId=self.ids['WF2'],includeChildren='false'))
            self.assertEqual(response.status_code,200)
            self.assertEqual([n['name'] for n in response.json['nodes']],['WF2'])
            self.assertEqual(client.get(url,query_string=dict(workflowId=self.ids['WF1'],includeChildren='invalid')).status_code,400)
            self.assertEqual(client.get(url).status_code,404)
            self.assertEqual(client.get(url.replace(str(self.graph['id']),str(uuid.uuid4())),query_string=dict(workflowId=self.ids['WF1'])).status_code,404)

    def test_dag_reads_postgres_and_bypasses_disabled_workflows_like_the_plan(self):
        self.config.workflow(self.ids['WF2'],dict(revision=self.config.read()['revision'],enabled=False),'EDITOR')
        app=Flask(__name__);app.register_blueprint(blueprint)
        with patch('task_engine.integration.store',return_value=self.store),patch('orchestration_configuration.sf.query_service',side_effect=AssertionError('No Snowflake reads')):
            response=app.test_client().get('/api/orchestration/task-engine/flow')
        self.assertEqual(response.status_code,200);flow=response.json
        self.assertEqual(flow['configurationStore'],'POSTGRES')
        self.assertNotIn(self.ids['WF2'],{w['WORKFLOW_ID'] for w in flow['workflows']})
        self.assertIn('DBT workflow',{w['WORKFLOW_NAME'] for w in flow['workflows']})
        self.assertEqual({e['PARENT_WORKFLOW_ID'] for e in flow['dependencies'] if e['CHILD_WORKFLOW_ID']==self.ids['WF4']},{self.ids['WF1'],self.ids['WF3']})
        plan=self.preview();ids={n['workflowId'] for n in plan['nodes']};by_key={n['key']:n['workflowId'] for n in plan['nodes']}
        self.assertEqual({(e['PARENT_WORKFLOW_ID'],e['CHILD_WORKFLOW_ID']) for e in flow['dependencies'] if e['PARENT_WORKFLOW_ID'] in ids and e['CHILD_WORKFLOW_ID'] in ids},
            {(by_key[p['key']],n['workflowId']) for n in plan['nodes'] for p in n['parents'] if p['inPlan']})
        self.assertFalse(self.store.status()['runs'])

    def test_dag_job_edges_skip_disabled_jobs_without_changing_configuration(self):
        identity=self.ids['WF1'];middle=str(uuid.uuid4());last=str(uuid.uuid4())
        first=next(r['JOB_ID'] for r in self.raw['raw_records'] if r['WORKFLOW_ID']==identity and r.get('JOB_ID'))
        def add_jobs(raw):
            for job,parent,enabled in [(middle,first,False),(last,middle,True)]:
                raw['jobs'].append(dict(JOB_ID=job,JOB_NAME=job,JOB_TYPE='SQL',SQL_COMMAND='SELECT 1',IS_ENABLED=enabled,TIMEOUT_SECONDS=60))
                raw['raw_records'].append(dict(WORKFLOW_ID=identity,JOB_ID=job,PARENT_ID=parent,TRIGGER_TYPE='DEP',ALLOW=True))
        self.config.change(dict(revision=self.config.read()['revision']),'EDITOR',add_jobs)
        before=self.config.read();flow=self.monitor.flow()
        self.assertEqual(self.config.read(),before)
        self.assertNotIn(middle,{j['JOB_ID'] for j in flow['jobs']})
        self.assertEqual(next(j['PARENT_JOB_ID'] for j in flow['jobs'] if j['JOB_ID']==last),first)
