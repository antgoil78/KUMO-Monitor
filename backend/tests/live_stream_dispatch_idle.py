"""Observe an idle DEV dispatcher without periodic warehouse reads during the window."""
import argparse
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend/migrations'))
from migration_support import connect
from snowflake.connector import DictCursor


def run(seconds=90,report_path=None):
    if seconds<75:raise ValueError('Observe at least 75 seconds, beyond the warehouse auto-suspend interval')
    with connect() as c,c.cursor(DictCursor) as cur:
        def q(sql,p=()):cur.execute(sql,p);return cur.fetchall()
        q('USE ROLE KUMO_ADMIN');q("ALTER SESSION SET QUERY_TAG='KUMO_DEV_STREAM_IDLE_OBSERVER',TIMEZONE='UTC'")
        assert not q("SELECT COUNT(*) N FROM KUMO_DEV.MONITOR_META.WORKFLOW_EXECUTION_LOG WHERE JOB_ID IS NULL AND STATUS IN ('QUEUED','CLAIMED','RUNNING')")[0]['N']
        assert not q("SELECT COUNT(*) N FROM KUMO_DEV.MONITOR_META.ORCH_DISPATCH_DELIVERIES WHERE STATE='READY' AND ATTEMPTS<3")[0]['N']
        assert q("SELECT SYSTEM$STREAM_HAS_DATA('KUMO_DEV.MONITOR_META.ORCH_DISPATCH_EVENTS_STREAM') HAS_DATA")[0]['HAS_DATA'] is False
        running=q("SELECT QUERY_ID FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME=>'TASK_ORCH_DISPATCH',RESULT_LIMIT=>50)) WHERE DATABASE_NAME='KUMO_DEV' AND SCHEMA_NAME='MONITOR_APP' AND STATE='EXECUTING' AND QUERY_ID IS NOT NULL")
        assert not any(c.is_still_running(c.get_query_status(r['QUERY_ID'])) for r in running),running
        before=q("SELECT CURRENT_TIMESTAMP() T")[0]['T']
        print('Idle window started; no queries will be submitted for '+str(seconds)+' seconds',flush=True)
        end=time.monotonic()+seconds
        while time.monotonic()<end:time.sleep(min(5,max(0,end-time.monotonic())))
        # SHOW is metadata only; observe before issuing any warehouse table read.
        warehouse=q("SHOW WAREHOUSES LIKE 'KUMO_ELT_GEN_1'")[0]['state']
        runs=q("SELECT QUERY_ID,STATE,SCHEDULED_TIME FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME=>'TASK_ORCH_DISPATCH',SCHEDULED_TIME_RANGE_START=>DATEADD('minute',-5,TO_TIMESTAMP_LTZ(%s)),RESULT_LIMIT=>1000)) WHERE DATABASE_NAME='KUMO_DEV' AND SCHEMA_NAME='MONITOR_APP' AND ((QUERY_ID IS NOT NULL AND COALESCE(COMPLETED_TIME,CURRENT_TIMESTAMP())>=TO_TIMESTAMP_LTZ(%s)) OR STATE='EXECUTING')",(str(before),str(before)))
        assert not runs,runs
        tasks=q("SHOW TASKS LIKE 'TASK_ORCH_%' IN SCHEMA KUMO_DEV.MONITOR_APP")
        maintained={t['name']:t for t in tasks if t['name'] in ('TASK_ORCH_DISPATCH','TASK_ORCH_MAINTENANCE')}
        assert maintained['TASK_ORCH_DISPATCH']['state']=='started' and not maintained['TASK_ORCH_DISPATCH']['schedule']
        assert maintained['TASK_ORCH_MAINTENANCE']['state']=='suspended' and not maintained['TASK_ORCH_MAINTENANCE']['schedule']
    report=dict(environment='DEV',idle_seconds=seconds,dispatcher_queries=0,maintenance_schedule=None,
                warehouse_state_at_end=warehouse,warehouse_shared_with_tst=True,
                note='No query was submitted during observation. Shared warehouse state can reflect other applications.',
                observed_from=str(before),tst_changed=False)
    if report_path:Path(report_path).write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true');p.add_argument('--seconds',type=int,default=90);p.add_argument('--report');a=p.parse_args()
    if a.run:print(json.dumps(run(a.seconds,a.report),indent=2))
    else:p.print_help()
