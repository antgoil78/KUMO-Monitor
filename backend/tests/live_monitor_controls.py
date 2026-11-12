"""Check DEV controls with a disposable future-scheduled SELECT job; no jobs run."""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE='http://127.0.0.1:5001'


def api(path, body=None, method='GET'):
    request=Request(BASE+path, data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json'},method=method)
    try:
        with urlopen(request,timeout=120) as response:value=json.load(response)
    except HTTPError as exc:
        raise RuntimeError(str(exc.code)+' '+path+' '+exc.read().decode())
    assert value.get('ok'),value
    assert not value.get('warning'),value
    return value


def run(catalog_only=False):
    assert api('/api/health')['db']=='KUMO_DEV', 'This validation is scoped to DEV only'
    workflow=None;job=None
    prefix='MONITOR CONTROLS '+str(uuid.uuid4())[:8]
    future=(datetime.now(timezone.utc)+timedelta(days=3)).replace(second=0,microsecond=0)
    cron=f'{future.minute} {future.hour} {future.day} {future.month} *'
    try:
        job=api('/api/orchestration/jobs',dict(jobName=prefix+' SQL',jobType='SQL',sqlCommand='SELECT 1'),'POST')['jobId']
        workflow=api('/api/orchestration/workflows',dict(workflowName=prefix,workflowGroup='ENGINE_VALIDATION',entryJobId=job,schedule=cron,scheduleTimezone='UTC'),'POST')['workflowId']
        address=f'/api/orchestration/workflows/{workflow}'
        def monitor(predicate=lambda row: True):
            deadline=time.monotonic()+45
            api('/api/orchestration/monitor?refresh=1')
            while time.monotonic()<deadline:
                row=next((row for row in api('/api/orchestration/monitor')['rows'] if row['WORKFLOW_ID']==workflow),None)
                if row and predicate(row):return row
                time.sleep(1)
            raise AssertionError('Shared Monitor cache did not reach the expected state')
        assert monitor()['scheduler']['state']=='NOT SCHEDULED'
        if not catalog_only:
            for enabled in (False,True):
                api(f'/api/orchestration/jobs/{job}/state',dict(enabled=enabled,expectedEnabled=not enabled),'PATCH')
                assert monitor(lambda row: row['jobs'][0]['JOB_IS_ENABLED']==enabled)['jobs'][0]['JOB_IS_ENABLED']==enabled
            enabled=api(address+'/schedule',dict(enabled=True,expectedEnabled=False),'PATCH')
            assert enabled['scheduler']['state']=='STARTED',enabled
            end=time.monotonic()+45
            while True:
                row=monitor()
                if row['scheduler'].get('nextRun'):break
                if time.monotonic()>=end:raise AssertionError(row['scheduler'])
                time.sleep(2)
            assert datetime.fromisoformat(row['scheduler']['nextRun'])==future,row['scheduler']
            api(address+'/state',dict(enabled=False,expectedEnabled=True),'PATCH')
            paused=monitor(lambda row: row['scheduler']['state']=='SUSPENDED');assert paused['IS_SCHEDULE_ENABLED'] and paused['scheduler']['state']=='SUSPENDED' and paused['scheduler']['nextRun'] is None,paused['scheduler']
            api(address+'/state',dict(enabled=True,expectedEnabled=False),'PATCH')
            assert monitor(lambda row: row['scheduler']['state']=='STARTED')['scheduler']['state']=='STARTED'
            changed=future+timedelta(days=1)
            api(address+'/schedule',dict(enabled=True,expectedEnabled=True,cron=f'{changed.minute} {changed.hour} {changed.day} {changed.month} *',timezone='UTC'),'PATCH')
            api(address+'/schedule',dict(enabled=False,expectedEnabled=True),'PATCH')
            disabled=monitor(lambda row: not row['IS_SCHEDULE_ENABLED']);assert not disabled['IS_SCHEDULE_ENABLED'] and disabled['scheduler']['nextRun'] is None
        graph=api(address+'/dag');assert len(graph['nodes'])==2 and graph['run'] is None,graph
        assert monitor()['lastRun']['start'] is None
        print('PASS: configured DAG and missing Last Run' if catalog_only else 'PASS: native scheduler create/resume, real Next Run, workflow pause/resume, schedule edit/disable, global job toggle and configured DAG; no workload executed',flush=True)
    finally:
        if workflow:
            api(f'/api/orchestration/workflows/{workflow}/schedule',dict(enabled=False),'PATCH')
            revision=api('/api/orchestration/configuration')['revision']
            api('/api/orchestration/job-order',dict(revision=revision,orders=[dict(workflowId=workflow,jobIds=[])]),'PUT')
            api(f'/api/orchestration/workflows/{workflow}',method='DELETE')
        if job:api(f'/api/orchestration/jobs/{job}',method='DELETE')
        print('Disposable workflow/job and scheduler removed',flush=True)


if __name__=='__main__':
    import sys
    run(catalog_only='--catalog-only' in sys.argv)
