import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import config
import orchestration_history as history
import snowflake_client as sf

WF='00000000-0000-0000-0000-000000000001'
RUN='00000000-0000-0000-0000-000000000002'
JOB='00000000-0000-0000-0000-000000000003'
EXEC='00000000-0000-0000-0000-000000000004'
OLDER='00000000-0000-0000-0000-000000000005'


def root(run=RUN):
    return dict(WORKFLOW_ID=WF,EXECUTION_ID=run,WORKFLOW_RUN_ID=run,STATUS='SUCCESS',
                WORKFLOW_NAME='Saved workflow name',WORKFLOW_GROUP='LOAD',RUN_MODE='JOB_ONLY',
                REQUESTED_AT=datetime(2026,11,10,12,tzinfo=timezone.utc))


class HistoryReadTests(unittest.TestCase):
    def test_snapshot_names_job_order_and_exact_execution_ids_survive_catalog_changes(self):
        child=dict(WORKFLOW_ID=WF,WORKFLOW_RUN_ID=RUN,EXECUTION_ID=EXEC,JOB_ID=JOB,JOB_POSITION=2,
                   STATUS='SUCCESS',JOB_NAME='Saved DBT job',JOB_TYPE='DBT',JOB_LAYER='SDL',
                   PARENT_EXECUTION_ID=RUN,RUN_SELECTED=True)
        with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',side_effect=[[root()],[child]]) as read, \
             patch.object(sf,'execute_service') as write:
            result=history.read_history(history.filters({}))
        run=result['runs'][0]; job=run['jobs'][0]
        self.assertEqual(run['workflowName'],'Saved workflow name')
        self.assertEqual(job['name'],'Saved DBT job');self.assertEqual(job['position'],2)
        self.assertEqual(job['executionId'],EXEC);self.assertEqual(job['runId'],RUN)
        self.assertEqual(job['parentExecutionId'],RUN);self.assertTrue(job['selected'])
        self.assertIn('PARENT_EXECUTION_ID',read.call_args.args[0])
        self.assertEqual(read.call_args.args[1],{'run_0':RUN})
        for call in read.call_args_list:
            self.assertIn(config.T_ORCHESTRATION_EXECUTION_LOG,call.args[0])
            for old in ('WORKFLOW_HISTORY','WORKFLOW_RUN_QUEUE','JOIN '+config.T_ORCHESTRATION_WORKFLOW):
                self.assertNotIn(old,call.args[0])
        write.assert_not_called()

    def test_job_history_status_is_scoped_to_that_job_within_the_containing_run(self):
        values=history.filters(dict(workflowId=WF,jobId=JOB,status='FAILED',search="O'Reilly_100%"))
        with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=[]) as read:
            result=history.read_history(values)
        sql,params=read.call_args.args
        self.assertIn('j.WORKFLOW_ID=l.WORKFLOW_ID',sql);self.assertIn('j.WORKFLOW_RUN_ID=l.EXECUTION_ID',sql)
        self.assertIn('j.STATUS=%(status)s',sql);self.assertNotIn('l.STATUS=%(status)s',sql)
        self.assertEqual(params['workflow'],WF);self.assertEqual(params['job'],JOB)
        self.assertEqual(params['search'],"%O'Reilly!_100!%%")
        self.assertNotIn("O'Reilly",sql);self.assertEqual(result['runs'],[])
        read.assert_called_once()

    def test_cursor_pages_by_timestamp_and_id_without_loading_extra_jobs(self):
        with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',side_effect=[[root(),root(OLDER)],[]]) as read:
            result=history.read_history(history.filters(dict(limit=1)))
        self.assertEqual(read.call_args.args[1],{'run_0':RUN})
        timestamp,run_id=history.decode_cursor(result['nextCursor'])
        self.assertEqual(run_id,RUN);self.assertIsNone(timestamp.tzinfo)
        with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=[]) as read:
            history.read_history(history.filters(dict(cursor=result['nextCursor'],limit=1)))
        self.assertIn('l.EXECUTION_ID < %(beforeId)s',read.call_args.args[0])
        self.assertEqual(read.call_args.args[1]['beforeId'],RUN)

    def test_invalid_filters_fail_before_any_query(self):
        for values in [dict(cursor='bad cursor'),dict(workflowId='unsafe'),dict(jobId='bad'),dict(status='NOPE'),
                       dict(limit=101),dict(limit=0),dict(days=-1),dict(search='x'*201)]:
            with self.subTest(values=values),self.assertRaises(ValueError):history.filters(values)

    def test_timeline_bounds_are_normalized_and_overlap_long_running_executions(self):
        values=history.filters(dict(start='2026-11-11T13:00:00+01:00',end='2026-11-11T13:05:00+01:00'))
        self.assertEqual(values['start'],'2026-11-11T12:00:00+00:00')
        with patch.object(sf,'connection_scope'),patch.object(sf,'query_service',return_value=[]) as read:
            history.read_history(values)
        sql,params=read.call_args.args
        self.assertIn('COALESCE(l.STARTED_AT,l.REQUESTED_AT) <= %(rangeEnd)s',sql)
        self.assertIn('>= %(rangeStart)s',sql)
        self.assertIn('THEN SYSDATE()',sql)
        self.assertNotIn('DATEADD(day',sql)
        self.assertEqual(params['rangeStart'],datetime(2026,11,11,12))
        self.assertEqual(params['rangeEnd'],datetime(2026,11,11,12,5))
        self.assertNotIn('2026-11-11',sql)

    def test_timeline_rejects_partial_naive_reversed_and_unbounded_ranges(self):
        for bounds in [dict(start='2026-11-11T12:00:00Z'),dict(end='2026-11-11T12:00:00Z'),
                       dict(start='2026-11-11T12:00:00',end='2026-11-11T13:00:00'),
                       dict(start='2026-11-11T12:00:00Z',end='2026-11-11T11:00:00Z'),
                       dict(start='2026-11-11T12:00:00Z',end='2026-11-11T12:00:00Z'),
                       dict(start='2026-10-01T12:00:00Z',end='2026-11-11T12:00:00Z')]:
            with self.subTest(bounds=bounds),self.assertRaises(ValueError):history.filters(bounds)

    def test_identical_concurrent_readers_share_one_history_query_batch(self):
        cache=history.HistoryCache(); entered=threading.Event(); release=threading.Event(); results=[]
        def load(values):entered.set();release.wait(2);return dict(ok=True,runs=[])
        values=history.filters({})
        with patch.object(history,'read_history',side_effect=load) as read:
            threads=[threading.Thread(target=lambda:results.append(cache.get(values))) for _ in range(10)]
            for worker in threads:worker.start()
            self.assertTrue(entered.wait(1));release.set()
            for worker in threads:worker.join(2);self.assertFalse(worker.is_alive())
            cache.get(values)
        read.assert_called_once();self.assertEqual(len(results),10)


class HistoryApiTests(unittest.TestCase):
    def test_api_uses_new_history_cache_and_rejects_invalid_filters(self):
        with patch.object(config,'USE_MOCK',False),patch.object(sf,'is_configured',return_value=True), \
             patch.object(application,'_renew_activity_lease'),patch.object(history.history_cache,'get',return_value=dict(ok=True,runs=[])) as read:
            client=application.app.test_client()
            response=client.get(f'/api/orchestration/history?workflowId={WF}&jobId={JOB}&refresh=1')
            self.assertEqual(response.status_code,200,response.get_json())
            self.assertEqual(read.call_args.args[0]['jobId'],JOB);self.assertTrue(read.call_args.kwargs['refresh'])
            response=client.get('/api/orchestration/history?cursor=invalid')
            self.assertEqual(response.status_code,400);self.assertEqual(read.call_count,1)


if __name__=='__main__':unittest.main()
