"""Explicit DEV run of the imported WF1→(WF2,WF3)→WF4 SQL snapshot.

Requires task-engine init and import-wf1 --apply. Stop other prototype execution
workers while this harness drives its worker. Does not invoke the old engine,
modify its catalog or schedules, or execute WF5–WF10.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine.settings import Settings
from task_engine.store import Store
from task_engine.dependencies import DependencyWorker, Graphs
from task_engine.snowflake_tasks import SnowflakeTasks
from task_engine.worker import Worker


class Tasks(SnowflakeTasks):
    def __init__(self, settings, graphs, cascade):
        super().__init__(settings)
        self.graphs, self.cascade = graphs, cascade
        self.executions = {}
        self.observations = {}
        self.frontier_checked = False

    def execute(self, job):
        nodes = {n['node_key']:n for n in self.graphs.read(self.cascade)['nodes']}
        if job['run_id'] in [nodes[k]['run_id'] for k in ('WF2','WF3')]:
            assert all(nodes[k]['run_id'] for k in ('WF2','WF3')), 'Both siblings must commit before either native submission'
            self.frontier_checked = True
        self.executions[str(job['run_id'])] = self.executions.get(str(job['run_id']), 0)+1
        return super().execute(job)

    def history(self, job):
        row = super().history(job)
        if row:
            self.observations[str(job['run_id'])] = row
        return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.parse_args()
    db = Store(Settings.from_environment())
    graphs = Graphs(db)
    graph = next((g for g in graphs.status()['graphs'] if g['key']=='wf1-sql-trial'), None)
    assert graph, 'Import WF1 first using the explicit read-only Snowflake snapshot command'
    request = dict(requestId='live-wf1-'+uuid.uuid4().hex[:12])
    cascade = graphs.start(graph['id'], request, 'LOCAL_DEV_DEPENDENCY_TEST')['cascadeId']
    assert graphs.start(graph['id'], request, 'LOCAL_DEV_DEPENDENCY_TEST')['reused']
    tasks = Tasks(db.settings, graphs, cascade)
    worker = Worker(db, tasks)
    dispatch = DependencyWorker(db)
    restart = False
    previous = None
    deadline = time.monotonic()+600
    print('Running copied WF1 SQL diamond '+cascade, flush=True)
    while time.monotonic()<deadline:
        dispatch.step();worker.step()
        detail = graphs.read(cascade)
        nodes = {n['node_key']:n for n in detail['nodes']}
        actual = {key:n['run_state'] or n['state'] for key,n in nodes.items()}
        if actual!=previous:
            print(json.dumps(actual), flush=True);previous=actual
        assert detail['cascade']['state']!='UNKNOWN', 'Unresolved native outcome requires review'
        assert detail['dispatch']['state']!='ERROR', detail['dispatch']
        if not restart and nodes['WF1']['run_state']=='SUCCESS' and nodes['WF2']['run_id'] is None:
            dispatch = DependencyWorker(db)
            restart = True
            print('Replaced dependency worker after committed parent outcome, before child admission', flush=True)
        if detail['cascade']['state']=='SUCCESS' and all(not n['cleanup_pending'] for n in nodes.values()):
            break
        time.sleep(.5)
    else:
        raise TimeoutError('WF1 dependency trial did not complete within ten minutes')
    assert restart and tasks.frontier_checked
    assert len(tasks.executions)==4 and all(n==1 for n in tasks.executions.values())
    native = {key:tasks.observations[str(n['run_id'])] for key,n in nodes.items()}
    assert all(row['state']=='SUCCEEDED' and row['attempt_number']==1 for row in native.values())
    assert all(native['WF1']['completed_time']<=native[key]['query_start_time'] for key in ('WF2','WF3'))
    assert max(native[key]['query_start_time'] for key in ('WF2','WF3')) < min(native[key]['completed_time'] for key in ('WF2','WF3'))
    assert native['WF4']['query_start_time']>=max(native[key]['completed_time'] for key in ('WF2','WF3'))
    for node in nodes.values():
        jobs = db.read(node['run_id'])['jobs']
        assert len(jobs)==1 and jobs[0]['cleanup_state']=='DONE'
        assert tasks.inspect(jobs[0], missing_ok=True) is None
    before_runs = {key:n['run_id'] for key,n in nodes.items()}
    graphs.retry_dispatch(cascade)
    dispatch.step()
    assert before_runs=={n['node_key']:n['run_id'] for n in graphs.read(cascade)['nodes']}
    start = datetime.now(timezone.utc);before = tasks.calls;checks = 0
    print('Beginning 90-second dependency/execution worker idle proof', flush=True)
    while (datetime.now(timezone.utc)-start).total_seconds()<90:
        assert not dispatch.step()
        assert not worker.step()
        checks+=1;time.sleep(1)
    end = datetime.now(timezone.utc)
    assert tasks.calls==before
    tagged = tasks.query("SELECT COUNT(*) AS COUNT FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.QUERY_HISTORY(END_TIME_RANGE_START=>TO_TIMESTAMP_LTZ(%s),END_TIME_RANGE_END=>TO_TIMESTAMP_LTZ(%s),RESULT_LIMIT=>10000)) WHERE QUERY_TAG='KUMO_DEV_POSTGRES_TASK_PROTOTYPE'", (str(start),str(end)))
    assert tagged[0]['count']==0
    report = dict(ok=True, environment='DEV', graphId=str(graph['id']), graphRevision=graph['revision'], cascadeId=cascade,
                  source=graph['source'],
                  nodes={key:dict(runId=str(n['run_id']),state=n['run_state'],parentRuns=n['parent_runs'],queryId=native[key]['query_id'],
                                  nativeStart=native[key]['query_start_time'].isoformat(),nativeEnd=native[key]['completed_time'].isoformat(),nativeSubmissions=tasks.executions[str(n['run_id'])]) for key,n in nodes.items()},
                  assertions=dict(durableParentCompletionEvent=True, workerReplacedBeforeChildAdmission=restart, siblingsAdmittedBeforeNativeSubmission=tasks.frontier_checked,
                                  nativeSiblingOverlap=True, joinWaitsForBothParents=True, repeatedDispatchCreatesNoNewRuns=True, temporaryTasksAbsent=True),
                  idle=dict(start=start.isoformat(),end=end.isoformat(),combinedWorkerChecks=checks,snowflakeAdapterCalls=0,taggedSnowflakeQueries=tagged[0]['count']))
    path = Path('review/dev-postgres-dependencies-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    path.write_text(json.dumps(report,indent=2)+'\n')
    print('PASS '+str(path),flush=True)


if __name__=='__main__':
    main()
