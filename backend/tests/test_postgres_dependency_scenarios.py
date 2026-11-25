"""Adversarial scenarios for the current catalog engine, on isolated PostgreSQL.

These tests execute no Snowflake SQL. Queue/running/unknown task outcomes are
simulated through the durable PostgreSQL state used by the dispatcher.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine.catalog import CatalogConfiguration, import_catalog
from task_engine.dependencies import DependencyWorker, Graphs
from task_engine.monitor import Monitor
from task_engine.schedules import Scheduler, Schedules
from task_engine.settings import Settings
from task_engine.store import Store, WorkflowBusy
from test_postgres_monitor import catalog


# Independent reference for the agreed WF1–WF10 topology, not derived from
# execution_scope or its calculated guards.
EDGES = [('WF1','WF2'), ('WF1','WF3'), ('WF2','WF4'), ('WF3','WF4'),
         ('WF4','WF5'), ('WF4','WF6'), ('WF5','WF7'), ('WF6','WF7'),
         ('WF7','WF8'), ('WF7','WF9'), ('WF8','WF10'), ('WF9','WF10')]
NAMES = [f'WF{i}' for i in range(1, 11)]


def full_catalog():
    raw = catalog()
    ids = {w['WORKFLOW_NAME']: w['WORKFLOW_ID'] for w in raw['workflows']}
    for name in NAMES[4:]:
        identity, job = str(uuid.uuid4()), str(uuid.uuid4())
        ids[name] = identity
        raw['workflows'].append(dict(WORKFLOW_ID=identity, WORKFLOW_NAME=name,
            IS_ENABLED=True, IS_SCHEDULE_ENABLED=False, JOB_EXECUTION_MODE='SEQUENTIAL'))
        raw['jobs'].append(dict(JOB_ID=job, JOB_NAME=name+' SQL', JOB_TYPE='SQL',
            SQL_COMMAND='SELECT 1', IS_ENABLED=True, TIMEOUT_SECONDS=60))
        raw['raw_records'].append(dict(WORKFLOW_ID=identity, JOB_ID=job,
            PARENT_ID=identity, TRIGGER_TYPE='DEP', ALLOW=True))
        for parent, child in EDGES:
            if child == name:
                raw['raw_records'].append(dict(WORKFLOW_ID=identity, JOB_ID=None,
                    PARENT_ID=ids[parent], TRIGGER_TYPE='DEP', ALLOW=True,
                    SUCCESS=True, WARNING=False, ERROR=False, PARENT_MUST_BE_NEWER=True))
    return raw


@unittest.skipUnless(os.getenv('KUMO_TASK_ENGINE_TEST_DSN'), 'Requires isolated PostgreSQL test database')
class DependencyScenarioTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Settings(os.environ['KUMO_TASK_ENGINE_TEST_DSN']))
        with self.store.connect() as conn:
            assert conn.execute('SELECT current_database() AS name').fetchone()['name'].endswith('_test')
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.dependency_decisions,kumo_task_engine.dependency_graphs,kumo_task_engine.dependency_workers,'
                'kumo_task_engine.schedules,kumo_task_engine.scheduler_workers,kumo_task_engine.runs RESTART IDENTITY CASCADE')
        self.raw = full_catalog()
        self.graph = import_catalog(self.store, self.raw, 'SCENARIO TEST')
        self.ids = {w['WORKFLOW_NAME']: w['WORKFLOW_ID'] for w in self.raw['workflows']}
        self.names = {key: name for name, identity in self.ids.items()
                      for key in [self.graph['definition']['workflowKeys'][identity]]}
        self.graphs, self.monitor = Graphs(self.store), Monitor(self.store)
        self.worker = DependencyWorker(self.store)
        no_snowflake = patch('snowflake_client.connection', side_effect=AssertionError('Scenario tests must not contact Snowflake'))
        no_snowflake.start()
        self.addCleanup(no_snowflake.stop)

    def start(self, name, children=False, request=None):
        return self.graphs.start(self.graph['id'], dict(requestId=request or str(uuid.uuid4()),
            workflowId=self.ids[name], includeChildren=children), 'SCENARIO TEST')['cascadeId']

    def drain(self):
        for _ in range(100):
            if not self.worker.step():
                return
        self.fail('Dependency dispatch did not become idle after 100 steps')

    def node(self, group, name):
        return next(n for n in self.graphs.read(group)['nodes'] if self.names[n['node_key']] == name)

    def outcome(self, group, name, state='SUCCESS'):
        run = self.node(group, name)['run_id']
        self.assertIsNotNone(run, name+' has not been admitted')
        with self.store.transaction() as conn:
            conn.execute("""UPDATE kumo_task_engine.attempts SET state=%s,
                native_started_at=COALESCE(native_started_at,clock_timestamp()-interval '1 second'),
                ended_at=CASE WHEN %s='UNKNOWN' THEN NULL ELSE clock_timestamp() END,next_check_at=NULL WHERE run_id=%s""",
                (state,state,run))
            self.store.refresh(conn, run)

    def due(self, name):
        schedule = Schedules(self.store).create(dict(requestId=str(uuid.uuid4()), name=name,
            cron='0 0 1 1 *', timezone='Europe/Stockholm', enabled=True,
            target=dict(graphId=str(self.graph['id']), workflowId=self.ids[name])), 'SCENARIO TEST')['schedule']
        with self.store.connect() as conn:
            conn.execute("UPDATE kumo_task_engine.schedules SET next_fire_at=clock_timestamp() WHERE id=%s", (schedule['id'],))
        return schedule

    def clear_work(self):
        with self.store.connect() as conn:
            conn.execute('TRUNCATE kumo_task_engine.dependency_decisions,kumo_task_engine.cascades,kumo_task_engine.runs,'
                'kumo_task_engine.schedules,kumo_task_engine.dependency_workers RESTART IDENTITY CASCADE')

    def decisions(self, group=None):
        with self.store.connect() as conn:
            return conn.execute('''SELECT * FROM kumo_task_engine.dependency_decisions
                WHERE (%s::uuid IS NULL OR cascade_id=%s::uuid) ORDER BY id''', (group,group)).fetchall()

    def running(self, group, name):
        run = self.node(group, name)['run_id']
        self.assertIsNotNone(run)
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s", (run,))
            self.store.refresh(conn, run)
        self.drain()

    def rows_by_name(self):
        return {w['WORKFLOW_NAME']: w for w in self.monitor.payload()['rows']}

    def finish_groups(self, groups):
        """Prove liveness and exclusion after every simulated completion frontier."""
        for _ in range(40):
            self.drain()
            records = {g: self.graphs.read(g) for g in groups}
            if all(r['cascade']['state'] == 'SUCCESS' for r in records.values()):
                return
            active = [(g, self.names[n['node_key']]) for g,r in records.items()
                      for n in r['nodes'] if n['run_state'] in ('QUEUED', 'RUNNING')]
            self.assertTrue(active, 'No active work can release the waiting workflows')
            for index, (_, name) in enumerate(active):
                for _, other in active[index+1:]:
                    self.assertNotEqual(name, other)
                    self.assertNotIn((name, other), EDGES)
                    self.assertNotIn((other, name), EDGES)
            for group, name in active:
                self.outcome(group, name)
        self.fail('Workflows did not finish within 40 completion frontiers')

    def test_wf2_only_and_wf5_downstream_run_concurrently_to_the_end(self):
        # WF7 is an AND join: establish the other parent's valid outcome first.
        seed = self.start('WF6'); self.drain(); self.outcome(seed, 'WF6'); self.drain()
        with ThreadPoolExecutor(max_workers=2) as pool:
            two, five = list(pool.map(lambda n: self.start(n, children=n=='WF5'), ['WF2', 'WF5']))
        self.drain(); self.running(two, 'WF2'); self.running(five, 'WF5')
        row = self.rows_by_name()['WF4']
        self.assertFalse(row['canRun'])
        self.assertEqual(row['run']['status'], 'NOT RUN')
        self.assertIn('parent WF2 (running)', row['runUnavailable'])
        self.assertIn('child WF5 (running)', row['runUnavailable'])
        with self.assertRaises(WorkflowBusy): self.start('WF4')
        self.finish_groups([five])
        self.assertEqual(self.node(two, 'WF2')['run_state'], 'RUNNING')
        self.assertEqual(len(self.graphs.read(five)['nodes']), 5)
        self.outcome(two, 'WF2'); self.drain()

    def test_wf2_wf3_and_wf5_hold_scheduled_wf4_until_all_three_finish(self):
        seed = self.start('WF6'); self.drain(); self.outcome(seed, 'WF6'); self.drain()
        with ThreadPoolExecutor(max_workers=3) as pool:
            two, three, five = list(pool.map(lambda n: self.start(n, children=n=='WF5'), ['WF2','WF3','WF5']))
        self.drain()
        for group, name in [(two,'WF2'), (three,'WF3'), (five,'WF5')]: self.running(group,name)
        schedule = self.due('WF4'); self.assertTrue(Scheduler(self.store).step()); self.drain()
        with self.store.connect() as conn:
            queued = str(conn.execute('SELECT cascade_id FROM kumo_task_engine.schedule_occurrences WHERE schedule_id=%s',(schedule['id'],)).fetchone()['cascade_id'])
        row = self.rows_by_name()['WF4']
        self.assertEqual(row['run']['status'], 'WAITING')
        for phrase in ['parent WF2 (running)', 'parent WF3 (running)', 'child WF5 (running)']:
            self.assertIn(phrase, row['run']['message'])
        self.assertEqual({b['relationship'] for b in row['run']['waitingFor']}, {'PARENT','CHILD'})
        for group,name in [(two,'WF2'), (three,'WF3')]:
            self.outcome(group,name); self.drain()
            self.assertIsNone(self.node(queued,'WF4')['run_id'])
        self.assertEqual(self.rows_by_name()['WF4']['run']['message'], 'Waiting for child WF5 (running) to complete')
        self.outcome(five,'WF5'); self.drain()
        self.assertIsNotNone(self.node(queued,'WF4')['run_id'])
        self.assertEqual(self.node(five,'WF7')['run_state'], 'QUEUED')
        # The child workflow releases WF4 as soon as its own jobs finish;
        # its entire downstream group does not have to finish first.
        self.finish_groups([queued,five])
        admitted = [d for d in self.decisions(queued) if d['decision']=='NODE_ADMITTED' and d['facts']['workflowName']=='WF4']
        self.assertEqual(len(admitted), 1)
        self.assertTrue(any(c['relationship']=='CHILD' for d in self.decisions(queued)
            for c in d['facts'].get('relatedChecks',[])))

    def test_wf4_wait_reason_tracks_each_parent_and_child_completion_order(self):
        import itertools
        for order in itertools.permutations(['WF2','WF3','WF5']):
            with self.subTest(order=order):
                self.clear_work()
                groups = {name:self.start(name) for name in order}; self.drain()
                for name,group in groups.items(): self.running(group,name)
                self.due('WF4'); Scheduler(self.store).step(); self.drain()
                for index,name in enumerate(order):
                    row = self.rows_by_name()['WF4']
                    for pending in order[index:]: self.assertIn(pending,row['run']['message'])
                    self.assertEqual(row['run']['status'],'WAITING')
                    self.outcome(groups[name],name); self.drain()
                    for finished in order[:index+1]:
                        self.assertNotIn(finished,self.rows_by_name()['WF4']['run']['message'])
                self.assertEqual(self.rows_by_name()['WF4']['run']['status'],'QUEUED')
                self.assertIsNotNone(self.rows_by_name()['WF4']['run']['runId'])
                self.assertEqual(self.rows_by_name()['WF4']['run']['message'],'')

    def test_wf5_join_waits_for_running_wf6_and_unknown_needs_review(self):
        six = self.start('WF6'); self.drain(); self.running(six,'WF6')
        five = self.start('WF5',children=True); self.drain()
        self.outcome(five,'WF5'); self.drain()
        row = self.rows_by_name()['WF7']
        self.assertEqual(row['run']['status'],'WAITING')
        self.assertIn('Waiting for parent WF6 (running) to complete',row['run']['message'])
        self.outcome(six,'WF6','UNKNOWN'); self.drain()
        row = self.rows_by_name()['WF7']
        self.assertIn('needs review',row['run']['message'])
        self.assertIsNone(self.node(five,'WF7')['run_id'])
        self.outcome(six,'WF6'); self.drain()
        self.finish_groups([five])

    def test_wf5_join_explains_missing_and_failed_external_parent(self):
        for outcome in [None,'FAILED','CANCELLED']:
            with self.subTest(outcome=outcome):
                self.clear_work()
                if outcome:
                    six = self.start('WF6'); self.drain(); self.outcome(six,'WF6',outcome); self.drain()
                five = self.start('WF5',children=True); self.drain()
                self.outcome(five,'WF5'); self.drain()
                row = self.rows_by_name()['WF7']
                self.assertEqual(row['run']['status'],'BLOCKED')
                self.assertIn('Parent WF6',row['run']['message'])
                self.assertIn('No parent execution was available' if outcome is None else outcome.lower(),row['run']['message'])
                self.assertIsNone(self.node(five,'WF7')['run_id'])

    def test_wf5_join_explains_successful_but_stale_wf6(self):
        six=self.start('WF6'); self.drain(); self.outcome(six,'WF6'); self.drain()
        old_join=self.start('WF7'); self.drain(); self.running(old_join,'WF7'); self.outcome(old_join,'WF7'); self.drain()
        five=self.start('WF5',children=True); self.drain(); self.outcome(five,'WF5'); self.drain()
        row=self.rows_by_name()['WF7']
        self.assertEqual(row['run']['status'],'BLOCKED')
        self.assertIn('Parent WF6 (out of date): Parent completed before the child last started',row['run']['message'])
        self.assertIsNone(self.node(five,'WF7')['run_id'])

    def test_cancelled_child_releases_scheduled_wf4_without_completing_its_descendants(self):
        for name in ['WF2','WF3']:
            seed=self.start(name); self.drain(); self.outcome(seed,name); self.drain()
        five = self.start('WF5'); self.drain(); self.running(five,'WF5')
        self.due('WF4'); Scheduler(self.store).step(); self.drain()
        row = self.rows_by_name()['WF4']; self.assertIn('child WF5 (running)',row['run']['message'])
        self.graphs.cancel(five,actor='CANCELLATION TEST'); self.drain()
        self.assertIn('child WF5',self.rows_by_name()['WF4']['run']['message'])
        self.outcome(five,'WF5','CANCELLED'); self.drain()
        self.assertEqual(self.rows_by_name()['WF4']['run']['message'],'')
        self.assertIsNotNone(self.node(row['cascadeId'],'WF4')['run_id'])

    def test_queued_reservations_are_identified_as_waiting_not_running(self):
        self.start('WF2',children=True)
        row = self.rows_by_name()['WF5']
        self.assertFalse(row['canRun'])
        self.assertIn('parent WF4 (waiting to start)',row['runUnavailable'])
        self.assertNotIn('(running)',row['runUnavailable'])
        with self.assertRaisesRegex(WorkflowBusy,'waiting to start'):
            self.start('WF5',children=True)

    def test_monitor_explains_existing_blockers_without_rewriting_execution_snapshots(self):
        from psycopg.types.json import Jsonb
        for name in ['WF2','WF3']:
            seed=self.start(name); self.drain(); self.outcome(seed,name); self.drain()
        five=self.start('WF5'); self.drain(); self.running(five,'WF5')
        self.due('WF4'); Scheduler(self.store).step(); self.drain()
        group=self.rows_by_name()['WF4']['cascadeId']
        with self.store.connect() as conn:
            conn.execute("UPDATE kumo_task_engine.cascades SET snapshot=snapshot-'relationships' WHERE id=%s",(group,))
            conn.execute("UPDATE kumo_task_engine.cascade_nodes SET blockers=%s WHERE cascade_id=%s AND node_key=%s",
                (Jsonb([dict(parent='WF5',state='RUNNING',reason='Waiting for related execution to complete')]),
                 group,self.graph['definition']['workflowKeys'][self.ids['WF4']]))
        row=self.rows_by_name()['WF4']
        self.assertEqual(row['run']['message'],'Waiting for child WF5 (running) to complete')
        self.assertEqual(row['run']['waitingFor'][0]['relationship'],'CHILD')
        self.assertIn('Waiting for child WF5',self.monitor.history(dict(workflowId=self.ids['WF4']))['runs'][0]['message'])
        log=self.monitor.log(self.ids['WF4'],row['run']['runId'])
        self.assertIn('Waiting for child WF5',log['history']['ERROR_MESSAGE'])

    def test_monitor_reads_current_internal_and_external_parent_state_before_dispatch_reevaluates(self):
        for external in [False,True]:
            with self.subTest(external=external):
                self.clear_work()
                if external:
                    parent=self.start('WF6'); self.drain()
                    group=self.start('WF5',children=True); parent_name,child_name='WF6','WF7'
                else:
                    parent=group=self.start('WF1',children=True); parent_name,child_name='WF1','WF2'
                self.drain()
                node=self.node(parent,parent_name)
                # A claim can mark a run RUNNING before any native task starts.
                with self.store.connect() as conn:
                    conn.execute("UPDATE kumo_task_engine.runs SET state='RUNNING' WHERE id=%s",(node['run_id'],))
                self.assertIn(f'parent {parent_name} (queued)',self.rows_by_name()[child_name]['run']['message'])
                with self.store.connect() as conn:
                    conn.execute("UPDATE kumo_task_engine.runs SET started_at=clock_timestamp() WHERE id=%s",(node['run_id'],))
                row=self.rows_by_name()[child_name]
                self.assertIn(f'parent {parent_name} (running)',row['run']['message'])
                history=self.monitor.history(dict(workflowId=self.ids[child_name]))['runs'][0]
                self.assertIn(f'parent {parent_name} (running)',history['message'])
                self.outcome(parent,parent_name)
                # The worker has not evaluated the committed outcome yet.
                row=self.rows_by_name()[child_name]
                self.assertIn(f'Parent {parent_name} (success): Awaiting dependency evaluation',row['run']['message'])
                self.assertIsNone(self.node(group,child_name)['run_id'])
                self.drain()

    @unittest.expectedFailure
    def test_acceptance_wf2_and_wf5_both_with_downstream_can_start_in_either_order(self):
        """Known gap: future descendants must not prevent independent entry starts."""
        for order in [('WF2','WF5'),('WF5','WF2')]:
            self.clear_work()
            for name in ['WF3','WF6']:
                seed=self.start(name); self.drain(); self.outcome(seed,name); self.drain()
            groups=[self.start(name,children=True) for name in order]
            self.drain()
            self.assertTrue(all(self.node(group,name)['run_id'] for group,name in zip(groups,order)))
            self.finish_groups(groups)

    def test_full_scope_wf5_before_wf2_completes_while_reverse_admission_is_rejected(self):
        for name in ['WF3','WF6']:
            seed=self.start(name); self.drain(); self.outcome(seed,name); self.drain()
        five=self.start('WF5',children=True)
        two=self.start('WF2',children=True)
        self.drain(); self.running(five,'WF5'); self.running(two,'WF2')
        self.assertIsNone(self.node(two,'WF4')['run_id'])
        self.assertIn('child WF5 (running)',self.rows_by_name()['WF4']['run']['message'])
        self.finish_groups([five,two])
        self.assertEqual(sum(len(self.graphs.read(g)['nodes']) for g in [five,two]),13)
        self.clear_work()
        self.start('WF2',children=True)
        with self.assertRaises(WorkflowBusy):self.start('WF5',children=True)

    @unittest.expectedFailure
    def test_acceptance_wf2_wf3_wf5_all_with_downstream_release_wf4_after_child(self):
        """Known gap: overlapping full groups reject sibling/descendant entries."""
        groups={name:self.start(name,children=True) for name in ['WF2','WF3','WF5']}
        self.drain()
        for name,group in groups.items(): self.running(group,name)
        for name in ['WF2','WF3']:
            self.outcome(groups[name],name); self.drain()
        four=self.node(groups['WF2'],'WF4')
        self.assertEqual(four['state'],'WAITING')
        self.assertIn('child WF5',self.rows_by_name()['WF4']['run']['message'])
        self.outcome(groups['WF5'],'WF5'); self.drain()
        self.assertIsNotNone(self.node(groups['WF2'],'WF4')['run_id'])
        self.finish_groups(list(groups.values()))

    def test_all_100_ordered_workflow_pairs_match_gui_and_backend_for_three_active_states(self):
        for state in ['QUEUED', 'RUNNING', 'UNKNOWN']:
            for first in NAMES:
                self.clear_work()
                group = self.start(first)
                self.drain()
                if state == 'UNKNOWN':
                    self.outcome(group, first, 'UNKNOWN')
                    self.drain()
                elif state == 'RUNNING':
                    run = self.node(group, first)['run_id']
                    with self.store.transaction() as conn:
                        conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s", (run,))
                        self.store.refresh(conn, run)
                blocked = {first} | {b for a,b in EDGES if a == first} | {a for a,b in EDGES if b == first}
                rows = {w['WORKFLOW_NAME']: w for w in self.monitor.payload()['rows']}
                for second in NAMES:
                    with self.subTest(state=state, first=first, second=second):
                        self.assertEqual(rows[second]['canRun'], second not in blocked)
                        with self.store.transaction() as conn:
                            conn.execute('SAVEPOINT candidate')
                            body = dict(requestId=str(uuid.uuid4()), workflowId=self.ids[second], includeChildren=False)
                            if second in blocked:
                                with self.assertRaises(WorkflowBusy):
                                    self.graphs.start_in_transaction(conn, self.graph['id'], body, 'SECOND CLIENT')
                            else:
                                self.graphs.start_in_transaction(conn, self.graph['id'], body, 'SECOND CLIENT')
                            conn.execute('ROLLBACK TO SAVEPOINT candidate')

    def test_four_simultaneous_clicks_admit_the_same_workflow_once(self):
        def submit(_):
            try:
                return self.start('WF4')
            except WorkflowBusy:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.drain()
        self.assertEqual(len(self.store.status()['runs']), 1)

    def test_four_nonadjacent_levels_can_be_admitted_concurrently(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            groups = list(pool.map(self.start, ['WF1','WF4','WF7','WF10']))
        self.assertEqual(len(set(groups)), 4)
        self.drain()
        self.assertEqual(len(self.store.status()['runs']), 4)

    def test_wf10_allows_wf1_and_its_descendants_until_wf8_and_wf9_need_to_wait(self):
        tail = self.start('WF10'); self.drain()
        root = self.start('WF1', children=True); self.drain()
        for frontier in [['WF1'], ['WF2','WF3'], ['WF4'], ['WF5','WF6'], ['WF7']]:
            for name in frontier:
                self.assertIsNotNone(self.node(root, name)['run_id'])
                self.outcome(root, name)
            self.drain()
        for name in ['WF8','WF9']:
            node = self.node(root, name)
            self.assertIsNone(node['run_id'])
            self.assertEqual(node['state'], 'WAITING')
        self.outcome(tail, 'WF10'); self.drain()
        for name in ['WF8','WF9']:
            self.assertIsNotNone(self.node(root, name)['run_id'])
            self.outcome(root, name)
        self.drain(); self.outcome(root, 'WF10'); self.drain()
        self.assertEqual(self.graphs.read(root)['cascade']['state'], 'SUCCESS')
        self.assertEqual(len(self.store.status()['runs']), 11)

    def test_two_overlapping_full_cascades_finish_without_duplicate_or_adjacent_active_workflows(self):
        older = self.start('WF4', children=True)
        newer = self.start('WF1', children=True)
        expected = {older: 7, newer: 10}
        for _ in range(40):
            self.drain()
            groups = [self.graphs.read(group) for group in expected]
            if all(g['cascade']['state'] == 'SUCCESS' for g in groups):
                break
            active = [(str(g['cascade']['id']), self.names[n['node_key']]) for g in groups
                      for n in g['nodes'] if n['run_state'] in ('QUEUED','RUNNING')]
            self.assertTrue(active, 'Both cascades are stuck with no runnable task')
            for index, (_, name) in enumerate(active):
                for _, other in active[index+1:]:
                    self.assertNotEqual(name, other, 'Duplicate workflow admitted across generations')
                    self.assertNotIn((name,other), EDGES)
                    self.assertNotIn((other,name), EDGES)
            for group, name in active:
                self.outcome(group, name)
        else:
            self.fail('Cascades did not finish in 40 frontiers')
        self.assertEqual(len(self.store.status()['runs']), sum(expected.values()))
        for group, count in expected.items():
            self.assertEqual(len(self.graphs.read(group)['nodes']), count)
            self.assertEqual(self.graphs.read(group)['cascade']['state'], 'SUCCESS')

    def test_external_join_pins_current_parent_and_never_falls_back_to_older_success(self):
        for result in ['SUCCESS','FAILED','CANCELLED','UNKNOWN']:
            with self.subTest(result=result):
                self.clear_work()
                old = self.start('WF3'); self.drain(); self.outcome(old,'WF3'); self.drain()
                parent = self.start('WF3'); self.drain()
                group = self.start('WF2', children=True); self.drain()
                self.outcome(group,'WF2'); self.drain()
                self.assertIsNone(self.node(group,'WF4')['run_id'])
                self.outcome(parent,'WF3',result); self.drain()
                child = self.node(group,'WF4')
                if result == 'SUCCESS':
                    self.assertIsNotNone(child['run_id'])
                    self.assertEqual(child['parent_runs'][self.node(parent,'WF3')['node_key']], str(self.node(parent,'WF3')['run_id']))
                else:
                    self.assertIsNone(child['run_id'])
                    self.assertEqual(child['state'], 'WAITING' if result == 'UNKNOWN' else 'BLOCKED')

    def test_missing_external_parent_is_blocked_and_a_later_parent_run_does_not_rebind_it(self):
        group = self.start('WF2', children=True); self.drain()
        self.assertEqual(self.node(group,'WF4')['state'], 'BLOCKED')
        other = self.start('WF3'); self.drain(); self.outcome(other,'WF3'); self.drain()
        self.outcome(group,'WF2'); self.drain()
        self.assertEqual(self.node(group,'WF4')['state'], 'BLOCKED')
        self.assertEqual(self.graphs.read(group)['cascade']['state'], 'FAILED')
        self.observed = dict(child='WF4', state='BLOCKED', group='FAILED',
            reason='WF3 had no execution when WF2 and its downstream snapshot were accepted; later WF3 success does not rebind it')

    def test_siblings_with_children_reserve_their_shared_join_but_sibling_only_runs_can_overlap(self):
        self.start('WF2', children=True)
        with self.assertRaises(WorkflowBusy):
            self.start('WF3', children=True)
        self.clear_work()
        self.start('WF2'); self.start('WF3'); self.drain()
        self.assertEqual(len(self.store.status()['runs']), 2)
        self.observed = dict(withChildren='Second sibling request rejected because shared child WF4 is already reserved',
            workflowOnly='WF2 and WF3 admitted concurrently')

    def test_catalog_schedule_admission_failure_rolls_back_occurrence_group_and_next_due_time(self):
        schedule = self.due('WF1')
        with self.store.connect() as conn:
            before = conn.execute('SELECT next_fire_at FROM kumo_task_engine.schedules WHERE id=%s', (schedule['id'],)).fetchone()
        original = Graphs.start_in_transaction
        def interrupt(service, *args, **kwargs):
            original(service, *args, **kwargs)
            raise RuntimeError('Simulated failure after cascade admission')
        with patch.object(Graphs, 'start_in_transaction', autospec=True, side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                Scheduler(self.store).step()
        self.assertEqual(Schedules(self.store).status()['occurrences'], [])
        self.assertEqual(self.graphs.status()['cascades'], [])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT next_fire_at FROM kumo_task_engine.schedules WHERE id=%s', (schedule['id'],)).fetchone(), before)
        self.assertTrue(Scheduler(self.store).step())
        self.assertEqual(len(Schedules(self.store).status()['occurrences']), 1)

    def test_cycle_edit_is_rejected_without_changing_master_or_accepted_snapshot(self):
        group = self.start('WF1', children=True)
        before = deepcopy(self.graphs.read(group)['cascade']['snapshot'])
        configuration = CatalogConfiguration(self.store)
        data = configuration.read()
        root = next(r for r in data['rows'] if not r['jobId'] and r['workflowId'] == self.ids['WF1'])
        root.update(parentId=self.ids['WF10'], trigger='DEP', success=True, warning=False, error=False)
        with self.assertRaises(ValueError):
            configuration.dependencies(dict(revision=data['revision'], rows=data['rows']), 'CYCLIC EDIT')
        self.assertEqual(configuration.read()['revision'], data['revision'])
        self.assertEqual(self.graphs.read(group)['cascade']['snapshot'], before)

    def test_removing_an_edge_during_a_run_uses_new_adjacency_for_new_admissions(self):
        active = self.start('WF1'); self.drain()
        before = deepcopy(self.graphs.read(active)['cascade']['snapshot'])
        configuration = CatalogConfiguration(self.store)
        data = configuration.read()
        row = next(r for r in data['rows'] if not r['jobId'] and r['workflowId'] == self.ids['WF2'])
        row.update(parentId=None, trigger=None, success=None, warning=None, error=None, parentMustBeNewer=False)
        configuration.dependencies(dict(revision=data['revision'], rows=data['rows']), 'REMOVE EDGE')
        other = self.start('WF2'); self.drain()
        self.assertIsNotNone(self.node(other, 'WF2')['run_id'])
        self.assertEqual(self.node(active, 'WF1')['run_state'], 'QUEUED')
        self.assertEqual(self.graphs.read(active)['cascade']['snapshot'], before)
        self.observed = dict(oldRevision=before['catalogRevision'],
            newRevision=configuration.read()['revision'], concurrentWorkflows=['WF1','WF2'],
            reason='A removed edge no longer blocks new requests, even while the old neighbor has an active frozen snapshot')

    def test_due_schedule_must_not_lose_dependencies_by_joining_a_workflow_only_manual_run(self):
        manual = self.start('WF1', children=False)
        before = deepcopy(self.graphs.read(manual)['cascade']['snapshot'])
        self.due('WF1'); Scheduler(self.store).step()
        occurrence = Schedules(self.store).status()['occurrences'][0]
        group = self.graphs.read(occurrence['cascade_id'])
        self.drain(); self.outcome(manual,'WF1'); self.drain()
        self.observed = dict(issue='SCHED_SCOPE_MISMATCH', joinedManualOnly=str(group['cascade']['id']) == manual,
            expectedWorkflowCount=10, actualWorkflowCount=len(group['nodes']),
            resultingGroupState=self.graphs.read(occurrence['cascade_id'])['cascade']['state'])
        self.assertNotEqual(str(group['cascade']['id']), manual,
            'A workflow-only run cannot fulfill a schedule that includes dependencies')
        self.assertEqual(len(group['nodes']), 10)
        self.assertEqual(self.graphs.read(manual)['cascade']['snapshot'], before)
        scheduled = str(group['cascade']['id'])
        for frontier in [['WF1'],['WF2','WF3'],['WF4'],['WF5','WF6'],['WF7'],['WF8','WF9'],['WF10']]:
            for name in frontier:
                self.outcome(scheduled,name)
            self.drain()
        self.assertEqual(self.graphs.read(scheduled)['cascade']['state'],'SUCCESS')
        self.assertEqual(len(self.store.status()['runs']),11)
        rejected = next(d for d in self.decisions(manual) if d['decision']=='PENDING_SCOPE_INCOMPATIBLE')
        self.assertEqual(len(rejected['facts']['missingKeys']),9)
        self.assertTrue(any(d['decision']=='REQUEST_ACCEPTED' and len(d['facts']['selectedKeys'])==10
                            for d in self.decisions(scheduled)))

    def test_due_schedule_rejects_partial_scope_already_admitted_to_native_queue(self):
        manual = self.start('WF1'); self.drain()
        self.due('WF1'); Scheduler(self.store).step()
        scheduled = str(Schedules(self.store).status()['occurrences'][0]['cascade_id'])
        self.assertNotEqual(manual,scheduled)
        self.drain()
        self.assertIsNone(self.node(scheduled,'WF1')['run_id'])
        self.assertEqual(len(self.graphs.read(scheduled)['nodes']),10)
        self.outcome(manual,'WF1'); self.drain()
        self.assertIsNotNone(self.node(scheduled,'WF1')['run_id'])

    def test_compatible_pending_scope_reuse_is_logged_and_coalesces_repeated_due_occurrences(self):
        manual = self.start('WF1',children=True)
        schedule = self.due('WF1')
        scheduler = Scheduler(self.store); scheduler.step()
        with self.store.connect() as conn:
            conn.execute('UPDATE kumo_task_engine.schedules SET next_fire_at=clock_timestamp() WHERE id=%s',(schedule['id'],))
        scheduler.step()
        occurrences = Schedules(self.store).status()['occurrences']
        self.assertEqual(len(occurrences),2)
        self.assertEqual({str(o['cascade_id']) for o in occurrences},{manual})
        self.assertEqual(len(self.graphs.status()['cascades']),1)
        reused = [d for d in self.decisions(manual) if d['decision']=='REQUEST_REUSED']
        self.assertEqual(len(reused),2)
        self.assertTrue(all(d['facts']['reason']=='COMPATIBLE_PENDING_SCOPE' and not d['facts']['missingKeys'] for d in reused))
        self.assertEqual(len([d for d in self.decisions(manual) if d['decision']=='OCCURRENCE_ADMITTED']),2)

    def test_leaf_workflow_only_scope_can_fulfill_a_schedule_with_identical_effective_scope(self):
        manual = self.start('WF10')
        self.due('WF10'); Scheduler(self.store).step()
        self.assertEqual(str(Schedules(self.store).status()['occurrences'][0]['cascade_id']),manual)

    def test_pending_scope_from_an_old_configuration_revision_is_not_reused(self):
        manual = self.start('WF1',children=True)
        configuration = CatalogConfiguration(self.store)
        configuration.workflow(self.ids['WF1'],dict(revision=1,description='Next accepted revision'),'EDITOR')
        self.due('WF1'); Scheduler(self.store).step()
        scheduled = str(Schedules(self.store).status()['occurrences'][0]['cascade_id'])
        self.assertNotEqual(manual,scheduled)
        self.assertEqual(self.graphs.read(scheduled)['cascade']['graph_revision'],2)
        decision = next(d for d in self.decisions(manual) if d['decision']=='PENDING_SCOPE_INCOMPATIBLE')
        self.assertEqual(decision['facts']['pendingRevision'],1)
        self.assertEqual(decision['facts']['missingKeys'],[])

    def test_decisions_explain_parent_checks_and_blockers_and_survive_worker_recreation(self):
        group = self.start('WF1',children=True); self.drain()
        first = self.decisions(group)
        wf2 = next(d for d in first if d['decision']=='NODE_WAITING' and d['facts']['workflowName']=='WF2')
        self.assertEqual(wf2['facts']['parentChecks'][0]['decision'],'WAIT_FOR_PARENT')
        self.assertTrue(wf2['evaluation_id'])
        self.outcome(group,'WF1')
        self.worker = DependencyWorker(Store(self.store.settings)); self.drain()
        admitted = next(d for d in self.decisions(group) if d['decision']=='NODE_ADMITTED' and d['facts']['workflowName']=='WF2')
        self.assertEqual(admitted['facts']['parentChecks'][0]['decision'],'ACCEPTED')
        self.assertEqual(admitted['facts']['parentChecks'][0]['runId'],str(self.node(group,'WF1')['run_id']))
        self.assertEqual([d['id'] for d in self.decisions(group)][:len(first)],[d['id'] for d in first])
        self.assertTrue(any(d['decision']=='EVALUATION_ENQUEUED' for d in self.decisions(group)))

    def test_rejected_manual_request_is_logged_after_admission_rollback(self):
        self.start('WF4')
        with self.assertRaises(WorkflowBusy):
            self.start('WF2',request='blocked-manual')
        rejected = next(d for d in self.decisions() if d['request_id']=='blocked-manual')
        self.assertEqual(rejected['decision'],'REQUEST_REJECTED')
        self.assertIn('WF4',rejected['facts']['reason'])
        self.assertEqual(len(self.graphs.status()['cascades']),1)

    def test_audit_failure_rolls_back_admission_and_schedule_advance(self):
        schedule = self.due('WF1')
        with self.store.connect() as conn:
            before = conn.execute('SELECT next_fire_at FROM kumo_task_engine.schedules WHERE id=%s',(schedule['id'],)).fetchone()
        with patch('task_engine.dependencies.record_decision',side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                Scheduler(self.store).step()
        self.assertEqual(self.graphs.status()['cascades'],[])
        self.assertEqual(Schedules(self.store).status()['occurrences'],[])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT next_fire_at FROM kumo_task_engine.schedules WHERE id=%s',(schedule['id'],)).fetchone(),before)
        self.assertEqual([d['decision'] for d in self.decisions()],['SCHEDULE_EVALUATION_ERROR'])
        self.assertTrue(Scheduler(self.store).step())

    def test_error_retries_have_durable_audit_without_rolled_back_admission_or_sensitive_error_text(self):
        group = self.start('WF1',children=True)
        original = self.store.submit_in_transaction
        def fail_after_admission(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('password=never-store-this SELECT secret')
        with patch.object(self.store,'submit_in_transaction',side_effect=fail_after_admission):
            for _ in range(6):
                with self.store.connect() as conn:
                    conn.execute('UPDATE kumo_task_engine.dependency_work SET next_check_at=clock_timestamp() WHERE cascade_id=%s',(group,))
                self.worker.step()
        decisions = self.decisions(group)
        errors = [d for d in decisions if d['decision'].startswith('DISPATCH_')]
        self.assertEqual([d['facts']['attempt'] for d in errors],list(range(1,7)))
        self.assertEqual(errors[-1]['decision'],'DISPATCH_ERROR')
        self.assertFalse(any(d['decision'] in ('EVALUATION_STARTED','NODE_ADMITTED') for d in decisions))
        self.assertNotIn('never-store-this',str(decisions))
        self.assertEqual(self.store.status()['runs'],[])
        self.graphs.retry_dispatch(group); self.drain()
        self.assertIsNotNone(self.node(group,'WF1')['run_id'])
        self.assertTrue(any(d['decision']=='DISPATCH_RETRY_REQUESTED' for d in self.decisions(group)))

    def test_idle_and_execution_disabled_ticks_do_not_write_decision_history(self):
        scheduler = Scheduler(self.store)
        for _ in range(3):
            self.assertFalse(self.worker.step())
            self.assertFalse(scheduler.step())
            self.assertFalse(self.worker.step(execution_enabled=False))
            self.assertFalse(scheduler.step(execution_enabled=False))
        self.assertEqual(self.decisions(),[])

    def test_cancel_decisions_and_audit_history_survive_execution_record_deletion(self):
        group = self.start('WF1',children=True)
        self.graphs.cancel(group); self.drain()
        self.graphs.cancel(group)
        before = self.decisions(group)
        kinds = {d['decision'] for d in before}
        self.assertTrue({'CANCEL_REQUESTED','NODE_CANCELLED','GROUP_EVALUATED','CANCEL_IGNORED'} <= kinds)
        with self.store.connect() as conn:
            conn.execute('DELETE FROM kumo_task_engine.dependency_work WHERE cascade_id=%s',(group,))
            conn.execute('DELETE FROM kumo_task_engine.cascade_nodes WHERE cascade_id=%s',(group,))
            conn.execute('DELETE FROM kumo_task_engine.cascades WHERE id=%s',(group,))
        self.assertEqual([d['id'] for d in self.decisions(group)],[d['id'] for d in before])

    def test_due_schedule_must_not_join_a_group_that_already_has_cancellation_requested(self):
        cancelled = self.start('WF1', children=True)
        self.graphs.cancel(cancelled)
        self.due('WF1'); Scheduler(self.store).step()
        occurrence = Schedules(self.store).status()['occurrences'][0]
        scheduled = str(occurrence['cascade_id'])
        self.assertNotEqual(scheduled,cancelled,
            'A newly due occurrence must survive cancellation of an earlier request')
        self.assertFalse(self.graphs.read(scheduled)['cascade']['cancel_requested'])
        self.drain()
        self.assertEqual(self.graphs.read(cancelled)['cascade']['state'],'CANCELLED')
        self.assertTrue(all(n['run_id'] is None for n in self.graphs.read(cancelled)['nodes']))
        self.assertEqual(len(self.graphs.read(scheduled)['nodes']),10)
        for frontier in [['WF1'],['WF2','WF3'],['WF4'],['WF5','WF6'],['WF7'],['WF8','WF9'],['WF10']]:
            for name in frontier:
                self.outcome(scheduled,name)
            self.drain()
        self.assertEqual(self.graphs.read(scheduled)['cascade']['state'],'SUCCESS')
        self.assertEqual(len(self.store.status()['runs']),10)
        excluded = next(d for d in self.decisions(cancelled) if d['decision']=='PENDING_CANCELLATION_EXCLUDED')
        self.assertEqual(excluded['facts']['reason'],'CANCELLATION_REQUESTED')
        self.assertFalse(any(d['decision']=='REQUEST_REUSED' for d in self.decisions(cancelled)))
        self.observed = dict(issue='SCHED_CANCEL_RACE',joinedCancelledRequest=False,
            resultingScheduledGroupState='SUCCESS',workflowRuns=10,cancelledGroupRuns=0,
            exclusionDecision=excluded['decision'])

    def test_scheduled_root_waits_for_cancelling_native_run_confirmation(self):
        cancelled = self.start('WF1',children=True); self.drain()
        run = self.node(cancelled,'WF1')['run_id']
        with self.store.transaction() as conn:
            conn.execute("UPDATE kumo_task_engine.attempts SET state='RUNNING',native_started_at=clock_timestamp() WHERE run_id=%s",(run,))
            self.store.refresh(conn,run)
        self.graphs.cancel(cancelled)
        self.due('WF1'); Scheduler(self.store).step(); self.drain()
        scheduled = str(Schedules(self.store).status()['occurrences'][0]['cascade_id'])
        self.assertNotEqual(scheduled,cancelled)
        self.assertEqual(self.graphs.read(cancelled)['cascade']['state'],'RUNNING')
        self.assertIsNone(self.node(scheduled,'WF1')['run_id'])
        waiting = next(d for d in self.decisions(scheduled) if d['decision']=='NODE_WAITING' and d['facts']['workflowName']=='WF1')
        self.assertTrue(any(c['cascadeId']==cancelled and c['decision']=='WAIT_FOR_RELATED_EXECUTION' for c in waiting['facts']['relatedChecks']))
        self.outcome(cancelled,'WF1','CANCELLED')
        self.worker = DependencyWorker(Store(self.store.settings)); self.drain()
        self.assertEqual(self.graphs.read(cancelled)['cascade']['state'],'CANCELLED')
        self.assertIsNotNone(self.node(scheduled,'WF1')['run_id'])
        self.assertFalse(self.graphs.read(scheduled)['cascade']['cancel_requested'])

    def test_due_schedule_excludes_waiting_child_of_cancelling_group_and_retains_parent_rules(self):
        cancelled = self.start('WF1',children=True)
        self.graphs.cancel(cancelled)
        self.due('WF2'); Scheduler(self.store).step()
        scheduled = str(Schedules(self.store).status()['occurrences'][0]['cascade_id'])
        self.assertNotEqual(scheduled,cancelled)
        self.assertFalse(self.graphs.read(scheduled)['cascade']['cancel_requested'])
        self.drain()
        self.assertEqual(self.node(scheduled,'WF2')['state'],'BLOCKED')
        self.assertIsNone(self.node(scheduled,'WF2')['run_id'])
        blocked = next(d for d in self.decisions(scheduled) if d['decision']=='NODE_BLOCKED' and d['facts']['workflowName']=='WF2')
        self.assertTrue(any(p['external'] and p['state']=='CANCELLED' and p['decision']=='OUTCOME_REJECTED' for p in blocked['facts']['parentChecks']))
        self.assertTrue(any(d['decision']=='PENDING_CANCELLATION_EXCLUDED' for d in self.decisions(cancelled)))

    def test_repeated_due_occurrences_share_fresh_group_while_older_group_cancels(self):
        cancelled = self.start('WF1',children=True)
        self.graphs.cancel(cancelled)
        schedule = self.due('WF1'); Scheduler(self.store).step()
        with self.store.connect() as conn:
            conn.execute('UPDATE kumo_task_engine.schedules SET next_fire_at=clock_timestamp() WHERE id=%s',(schedule['id'],))
        # Recreating the scheduler keeps occurrence/group relationships durable.
        Scheduler(Store(self.store.settings)).step()
        occurrences = Schedules(self.store).status()['occurrences']
        self.assertEqual(len(occurrences),2)
        scheduled = str(occurrences[0]['cascade_id'])
        self.assertNotEqual(scheduled,cancelled)
        self.assertEqual({str(o['cascade_id']) for o in occurrences},{scheduled})
        self.assertEqual(len(self.graphs.status()['cascades']),2)
        self.assertEqual(len([d for d in self.decisions(scheduled) if d['decision']=='REQUEST_REUSED']),1)
        self.worker = DependencyWorker(Store(self.store.settings)); self.drain()
        self.assertEqual(self.graphs.read(cancelled)['cascade']['state'],'CANCELLED')
        self.assertIsNotNone(self.node(scheduled,'WF1')['run_id'])
        self.assertEqual(len(self.store.status()['runs']),1)

    def test_occurrence_already_shared_before_cancel_remains_in_its_accepted_group(self):
        original = self.start('WF1',children=True)
        self.due('WF1'); Scheduler(self.store).step()
        self.assertEqual(str(Schedules(self.store).status()['occurrences'][0]['cascade_id']),original)
        self.graphs.cancel(original); self.drain()
        self.assertEqual(Schedules(self.store).status()['occurrences'][0]['run_state'],'CANCELLED')
        decisions = self.decisions(original)
        reused = next(d for d in decisions if d['decision']=='REQUEST_REUSED')
        cancelled = next(d for d in decisions if d['decision']=='CANCEL_REQUESTED')
        self.assertLess(reused['id'],cancelled['id'])

    def test_concurrent_cancel_and_due_schedule_follow_the_committed_decision_order(self):
        for _ in range(4):
            self.clear_work()
            original = self.start('WF1',children=True)
            self.due('WF1')
            with ThreadPoolExecutor(max_workers=2) as pool:
                cancellation = pool.submit(self.graphs.cancel,original)
                scheduling = pool.submit(Scheduler(self.store).step)
                cancellation.result(); self.assertTrue(scheduling.result())
            occurrences = Schedules(self.store).status()['occurrences']
            self.assertEqual(len(occurrences),1)
            scheduled = str(occurrences[0]['cascade_id'])
            decisions = self.decisions()
            cancelled = next(d for d in decisions if d['decision']=='CANCEL_REQUESTED')
            if scheduled == original:
                reused = next(d for d in decisions if d['decision']=='REQUEST_REUSED')
                self.assertLess(reused['id'],cancelled['id'])
            else:
                excluded = next(d for d in decisions if d['decision']=='PENDING_CANCELLATION_EXCLUDED')
                self.assertLess(cancelled['id'],excluded['id'])
                self.assertFalse(self.graphs.read(scheduled)['cascade']['cancel_requested'])
            self.drain()
            self.assertEqual(self.graphs.read(original)['cascade']['state'],'CANCELLED')
            if scheduled != original:
                self.assertIsNotNone(self.node(scheduled,'WF1')['run_id'])

    def test_same_request_retry_after_cancel_stays_idempotent_but_new_schedule_is_separate(self):
        original = self.start('WF1',children=True,request='stable-original')
        self.graphs.cancel(original)
        self.assertEqual(self.start('WF1',children=True,request='stable-original'),original)
        self.due('WF1'); Scheduler(self.store).step()
        scheduled = str(Schedules(self.store).status()['occurrences'][0]['cascade_id'])
        self.assertNotEqual(scheduled,original)
        reused = next(d for d in self.decisions(original) if d['decision']=='REQUEST_REUSED')
        self.assertEqual(reused['facts']['reason'],'IDEMPOTENT_REQUEST')

    def test_exhausted_dependency_dispatch_failure_is_visible_in_the_monitor(self):
        group = self.start('WF1', children=True)
        with patch.object(self.store, 'submit_in_transaction', side_effect=RuntimeError('Admission unavailable')):
            for _ in range(6):
                with self.store.connect() as conn:
                    conn.execute("UPDATE kumo_task_engine.dependency_work SET next_check_at=clock_timestamp() WHERE cascade_id=%s", (group,))
                self.worker.step()
        # The next idle tick commits a healthy heartbeat even though this group
        # has exhausted its dispatch retries.
        self.assertFalse(self.worker.step())
        dispatch = self.graphs.read(group)['dispatch']
        data = self.monitor.payload()
        row = next(w for w in data['rows'] if w['WORKFLOW_NAME'] == 'WF1')
        self.observed = dict(issue='DISPATCH_ERRORS_HIDDEN', persistedDispatchState=dispatch['state'],
            retryCount=dispatch['attempts'], workerBadge=data['worker']['state'],
            workflowStatus=row['run']['status'], workflowMessage=row['run']['message'])
        self.assertEqual(dispatch['state'], 'ERROR')
        self.assertEqual(row['dispatch']['state'],'ERROR')
        self.assertEqual(row['dispatch']['attempts'],6)
        self.assertTrue(row['dispatch']['canRetry'])
        self.assertEqual(row['run']['status'],'WAITING')
        self.assertIn('Dependency dispatch paused after 6 attempts',row['run']['message'])
        self.assertIn('Dependency evaluation unavailable',row['run']['message'])
        self.assertEqual(data['worker']['state'],'HEALTHY')
        self.assertEqual(len(data['dispatchErrors']),1)
        self.assertEqual(data['dispatchErrors'][0]['cascadeId'],group)
        self.assertEqual(data['dispatchErrors'][0]['attempts'],6)
        self.assertTrue(data['dispatchErrors'][0]['canRetry'])


if __name__ == '__main__':
    unittest.main()
