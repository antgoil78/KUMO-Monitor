"""PostgreSQL projections for the maintained Monitor, History and Log views."""
from datetime import datetime,timedelta,timezone
import os
import uuid

from .catalog import CatalogConfiguration,local_catalog
from .store import TERMINAL
from .admission import execution_scope,outstanding,busy_message,blocker_message,relationship
from .recovery import recovery_stage

ACTIVE={'QUEUED','RUNNING','UNKNOWN'}


def now():return datetime.now(timezone.utc)



def task_state(value):
    return 'QUEUED' if value in ('CREATING','SUBMITTING','SUBMITTED','SCHEDULED') else value


class Monitor:
    def __init__(self,store):self.store=store

    def records(self,conn,filters=None,latest=False,started=False,node_id=None,workflow_id=None,planning=False):
        clauses=[];params=[];filters=filters or {}
        scheduled_sql="""EXISTS(SELECT 1 FROM kumo_task_engine.schedule_occurrences o JOIN kumo_task_engine.schedules s ON s.id=o.schedule_id
                WHERE o.cascade_id=c.id AND s.plan->>'workflowId'=c.snapshot->'workflowIds'->>n.node_key)"""
        if node_id:clauses.append('n.id=%s');params.append(uuid.UUID(str(node_id)))
        if workflow_id or filters.get('workflowId'):
            clauses.append("COALESCE(c.snapshot->'workflowIds'->>n.node_key,g.source->'workflows'->>n.node_key,n.node_key)=%s")
            params.append(workflow_id or filters['workflowId'])
        if started:clauses.append('r.started_at IS NOT NULL')
        if filters.get('start'):
            clauses.extend(['COALESCE(r.started_at,c.created_at)<= %s',"COALESCE(r.ended_at,CASE WHEN c.state IN ('QUEUED','RUNNING','UNKNOWN') THEN clock_timestamp() ELSE c.ended_at END)>= %s"])
            params.extend((datetime.fromisoformat(filters['end']),datetime.fromisoformat(filters['start'])))
        elif filters.get('days'):
            clauses.append("c.created_at>=clock_timestamp()-(%s*interval '1 day')");params.append(filters['days'])
        if filters.get('cursor'):
            from orchestration_history import decode_cursor
            stamp,identity=decode_cursor(filters['cursor'])
            clauses.append('(c.created_at,n.id)<(%s,%s)');params.extend((stamp.replace(tzinfo=timezone.utc),uuid.UUID(identity)))
        if filters.get('status'):
            if filters.get('jobId'):
                clauses.append('EXISTS(SELECT 1 FROM kumo_task_engine.attempts a WHERE a.run_id=n.run_id AND a.job_key=%s AND a.state=%s)');params.extend((filters['jobId'],filters['status']))
            else:
                clauses.append("""CASE WHEN r.state='RUNNING' AND r.started_at IS NULL THEN 'QUEUED'
                    WHEN r.state IS NULL AND n.state='ADMITTED' THEN 'QUEUED'
                    ELSE COALESCE(r.state,n.state) END=%s""");params.append(filters['status'])
        if filters.get('jobId'):
            clauses.append("EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot_node.value->'plan'->'jobs') j WHERE j->>'key'=%s)");params.append(filters['jobId'])
        if filters.get('search'):
            clauses.append("(snapshot_node.value->>'name' ILIKE %s OR c.requested_by ILIKE %s OR n.id::text ILIKE %s OR EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot_node.value->'plan'->'jobs') j WHERE j->>'name' ILIKE %s))")
            params.extend(['%'+filters['search']+'%']*4)
        sql=f'''SELECT n.*,c.snapshot,c.graph_revision,c.request_id,c.requested_by,c.created_at AS requested_at,
            c.ended_at AS cascade_ended_at,c.state AS cascade_state,c.cancel_requested,r.state AS actual_state,r.started_at,r.ended_at,
            work.state AS dispatch_state,work.attempts AS dispatch_attempts,work.error AS dispatch_error,
            g.source,CASE WHEN EXISTS(SELECT 1 FROM jsonb_array_elements(n.blockers) b
                WHERE b->>'parent' IS NOT NULL AND b->>'relationship' IS NULL)
                THEN jsonb_build_object('nodes',(SELECT jsonb_agg(jsonb_build_object('key',edge->>'key','parents',edge->'parents'))
                    FROM jsonb_array_elements(g.definition->'nodes') edge)) END AS current_definition,snapshot_node.value AS definition,
            {scheduled_sql} AS scheduled,
            COALESCE(c.snapshot->'workflowIds'->>n.node_key,g.source->'workflows'->>n.node_key,n.node_key) AS workflow_id
            FROM kumo_task_engine.cascade_nodes n JOIN kumo_task_engine.cascades c ON c.id=n.cascade_id
            JOIN kumo_task_engine.dependency_graphs g ON g.id=c.graph_id
            LEFT JOIN kumo_task_engine.dependency_work work ON work.cascade_id=c.id
            LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id
            JOIN LATERAL jsonb_array_elements(c.snapshot->'nodes') AS snapshot_node(value) ON snapshot_node.value->>'key'=n.node_key'''
        if clauses:sql+=' WHERE '+' AND '.join(clauses)
        if latest or started:
            sort="started_at DESC,id DESC" if started else "(cascade_state IN ('QUEUED','RUNNING','UNKNOWN')) DESC,COALESCE(actual_state IN ('QUEUED','RUNNING','UNKNOWN'),false) DESC,requested_at DESC,id DESC"
            if planning:sort="(cascade_state IN ('QUEUED','RUNNING','UNKNOWN') AND (COALESCE(actual_state IN ('QUEUED','RUNNING','UNKNOWN'),false) OR state='WAITING')) DESC,(state='WAITING') DESC,requested_at DESC,id DESC"
            sql='SELECT * FROM (SELECT v.*,row_number() OVER(PARTITION BY workflow_id ORDER BY '+sort+') AS rank FROM ('+sql+') v) ranked WHERE rank=1'
        else:sql+=' ORDER BY c.created_at DESC,n.id DESC LIMIT '+str(int(filters.get('limit',100))+1)
        rows=conn.execute(sql,params).fetchall()
        ids=[r['run_id'] for r in rows if r['run_id']]
        attempts=conn.execute('SELECT * FROM kumo_task_engine.attempts WHERE run_id=ANY(%s) ORDER BY position',(ids,)).fetchall() if ids else []
        group_ids={r['cascade_id'] for r in rows}
        group_ids.update(uuid.UUID(b['cascadeId']) for r in rows for b in r['blockers'] if b.get('cascadeId'))
        parent_ids={uuid.UUID(binding) for r in rows for binding in r['snapshot'].get('externalBindings',{}).values() if binding}
        siblings=conn.execute('''SELECT n.id,n.cascade_id,n.node_key,n.run_id,n.state,r.state AS actual_state,r.started_at
            FROM kumo_task_engine.cascade_nodes n LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id
            WHERE n.cascade_id=ANY(%s) OR n.id=ANY(%s)''',(list(group_ids),list(parent_ids))).fetchall() if rows else []
        by_id={str(n['id']):n for n in siblings}
        by_run={str(n['run_id']):n for n in siblings if n['run_id']}
        by_group={(str(n['cascade_id']),n['node_key']):n for n in siblings}
        for row in rows:
            row['attempts']=[a for a in attempts if a['run_id']==row['run_id']]
            row['siblings']={s['node_key']:str(s['id']) for s in siblings if s['cascade_id']==row['cascade_id']}
            row['blocker_nodes'],row['blocker_runs'],row['blocker_groups']=by_id,by_run,by_group
        return rows

    def run(self,row):
        definition=row['definition'];status=row['actual_state'] or row['state']
        if status=='ADMITTED':status='QUEUED'
        # An accepted dependency node is waiting until admitted to native execution.
        # Scheduling and reservations do not mean its prerequisites are satisfied.
        if status=='RUNNING' and not row['started_at']:status='QUEUED'
        root_id=str(row['id']);attempts={a['job_key']:a for a in row['attempts']}
        names=dict(row['snapshot'].get('workflowNames',{}),**{n['key']:n['name'] for n in row['snapshot']['nodes']})
        parent_ids=[row['siblings'][p['key']] for p in definition['parents'] if p['key'] in row['siblings']]
        parent_ids.extend(row['snapshot']['externalBindings'][p['key']] for p in row['snapshot'].get('externalParents',{}).get(row['node_key'],[]) if row['snapshot']['externalBindings'].get(p['key']))
        blockers=[]
        for saved in row['blockers']:
            blocker=dict(saved)
            if blocker.get('parent'):
                key=blocker.get('nodeKey',blocker['parent'])
                if key not in names:key=next((k for k,name in names.items() if name==key),key)
                if not blocker.get('relationship'):
                    relation=relationship(row['snapshot'],row['node_key'],key)
                    if relation=='RELATED':relation=relationship(row['current_definition'],row['node_key'],key)
                    blocker.update(relationship=relation,nodeKey=key)
                # Show current native progress, retaining the last admission decision.
                # Terminal decisions and freshness failures remain historical facts.
                if row['state'] in {'WAITING','BLOCKED'} and blocker.get('state')!='STALE_PARENT':
                    binding=(row['snapshot'].get('externalBindings',{}).get(key) or row['siblings'].get(key)) if saved.get('reason')!='Waiting for related execution to complete' else None
                    target=(row['blocker_runs'].get(blocker.get('runId')) or
                        row['blocker_groups'].get((blocker.get('cascadeId'),key)) or row['blocker_nodes'].get(binding))
                    if target:
                        state=target['actual_state'] or target['state']
                        if state=='RUNNING' and not target['started_at']:state='QUEUED'
                        blocker.update(state=state,runId=str(target['run_id']) if target['run_id'] else None)
                        if state in TERMINAL|{'BLOCKED'} and blocker.get('reason') in ('Waiting for parent completion','Waiting for related execution to complete','Parent outcome needs review'):
                            blocker['reason']='Plan stopped; parent status updated' if row['state']=='BLOCKED' else 'Awaiting dependency evaluation'
            blockers.append(blocker)
        message='; '.join(dict.fromkeys(blocker_message(b,names) for b in blockers))
        waiting_for=[dict(name=names.get(b['parent'],b['parent']),status=b.get('state'),
            relationship=b.get('relationship','RELATED'),reason=b.get('reason'),
            nodeKey=b.get('nodeKey',b['parent']),runId=b.get('runId'),cascadeId=b.get('cascadeId'))
            for b in blockers if b.get('parent')]
        if row['cancel_requested'] and row['cascade_state'] in ACTIVE:message='Cancellation requested; awaiting task confirmation'
        dispatch=dict(state=row['dispatch_state'],attempts=row['dispatch_attempts'] or 0,
            error=row['dispatch_error'],cascadeId=str(row['cascade_id']),
            canRetry=row['dispatch_state']=='ERROR' and row['cascade_state'] in ACTIVE)
        if dispatch['canRetry'] and row['state']=='WAITING':
            message='; '.join(filter(None,[
                f"Dependency dispatch paused after {dispatch['attempts']} attempts. "+(dispatch['error'] or 'Dependency evaluation unavailable'),message]))
        network=[a for a in row['attempts'] if a.get('recovery_kind')=='NETWORK']
        unknown=[a for a in row['attempts'] if a['state']=='UNKNOWN']
        recovery=None
        if network or unknown:
            due=[a['next_check_at'] for a in network if a['next_check_at']]
            recovery=dict(automatic=bool(network),canRecover=any(recovery_stage(a) for a in unknown),
                runId=str(row['run_id']),nextRetryAt=min(due) if due else None,
                attempts=max(a['recovery_checks'] for a in network or unknown))
            if network:
                detail='; '.join(dict.fromkeys((a['cleanup_error'] if a['cleanup_state']=='PENDING' else a['error']) or 'Snowflake connection unavailable' for a in network))
                recovery_message=detail+'; automatic recovery scheduled'
            else:recovery_message='Task recovery paused: '+'; '.join(dict.fromkeys(a['error'] or 'Native outcome needs review' for a in unknown))
            message='; '.join(filter(None,[message,recovery_message]))
        root=dict(runId=root_id,executionId=root_id,workflowId=row['workflow_id'],workflowName=definition['name'],workflowGroup=definition.get('group',''),
            status=status,start=row['started_at'],end=row['ended_at'] or (row['cascade_ended_at'] if status in TERMINAL|{'BLOCKED'} else None),requestedAt=row['requested_at'],
            requestedBy=row['requested_by'],requestId=row['request_id'],mode='WORKFLOW',triggerSource='SCHEDULED' if row['scheduled'] else row['snapshot'].get('trigger','POSTGRES'),source='POSTGRES',message=message,
            jobExecutionMode=definition['plan']['mode'],cascadeId=str(row['cascade_id']),graphRevision=row['graph_revision'],
            parentExecutionIds=parent_ids,parentExecutionId=next(iter(parent_ids),None),waitingFor=waiting_for,dispatch=dispatch,recovery=recovery,
            retry=dict(cascadeId=str(row['cascade_id']),nodeId=root_id) if row['actual_state']=='FAILED' and not row['cancel_requested'] else None,
            resumedFrom=row['snapshot'].get('resumedFrom',{}).get(row['node_key']),jobs=[])
        for index,job in enumerate(definition['plan']['jobs']):
            a=attempts.get(job['key']);enabled=job.get('enabled',True)
            state=task_state(a['state']) if a else 'SKIPPED' if not enabled or status in TERMINAL|{'BLOCKED'} else 'PENDING'
            previous=definition['plan']['jobs'][index-1]['key'] if index and definition['plan']['mode']=='SEQUENTIAL' else None
            parent=attempts.get(previous)
            root['jobs'].append(dict(runId=root_id,executionId=str(a['id']) if a else str(uuid.uuid5(row['id'],job['key'])),jobId=job['key'],
                name=job.get('name',job['key']),type=job.get('type','SQL'),layer=job.get('layer','GENERAL'),position=index+1,status=state,selected=enabled,
                start=a['native_started_at'] if a else None,end=a['ended_at'] if a else None,requestedAt=row['requested_at'],
                message=(a['error'] or a['cleanup_error'] or '') if a else 'Disabled in this execution' if not enabled else message,
                queryId=a['query_id'] if a else None,cleanupState=a['cleanup_state'] if a else 'NONE',
                parentExecutionId=str(parent['id']) if parent else root_id,source='POSTGRES'))
        return root

    def payload(self):
        configuration=CatalogConfiguration(self.store)
        with self.store.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            graph=configuration.graph(conn);catalog=configuration.payload(conn,graph)
            records=self.records(conn,latest=True);last=self.records(conn,started=True)
            plans=self.records(conn,latest=True,planning=True)
            active_nodes=outstanding(conn,graph['id'],list(graph['definition']['workflowKeys'].values()))
            schedules=conn.execute("SELECT * FROM kumo_task_engine.schedules WHERE plan->>'graphId'=%s",(str(graph['id']),)).fetchall()
            workers=conn.execute('SELECT *,EXTRACT(EPOCH FROM clock_timestamp()-last_seen) AS age FROM kumo_task_engine.dependency_workers ORDER BY last_seen DESC LIMIT 1').fetchone()
            # Read every active error group, including ones hidden behind an
            # older running instance in the latest-workflow projection.
            dispatch_errors=conn.execute('''SELECT c.id,c.graph_revision,c.created_at,work.attempts,work.error,
                (SELECT jsonb_agg(node.value->>'name') FROM jsonb_array_elements(c.snapshot->'nodes') node(value)) AS workflow_names
                FROM kumo_task_engine.cascades c JOIN kumo_task_engine.dependency_work work ON work.cascade_id=c.id
                WHERE c.graph_id=%s AND c.state IN ('QUEUED','RUNNING','UNKNOWN') AND work.state='ERROR'
                ORDER BY c.created_at,c.id''',(graph['id'],)).fetchall()
        current={r['workflow_id']:self.run(r) for r in records};previous={r['workflow_id']:self.run(r) for r in last}
        planned={r['workflow_id']:self.run(r) for r in plans}
        active={r['workflow_id']:str(r['cascade_id']) for r in records if r['cascade_state'] in ACTIVE}
        assignments=catalog['assignments'];rows=[]
        for wf in catalog['workflows']:
            identity=wf['WORKFLOW_ID'];scope=execution_scope(graph['definition'],identity);run=current.get(identity)
            own_scope=execution_scope(graph['definition'],identity,False)
            root=run or dict(status='NOT RUN',runId=None,executionId=None,start=None,end=None,requestedAt=None,message='',jobs=[])
            # A request may never execute. Keep its lifecycle separate from the
            # latest actual execution, including jobs, timing, progress and Log.
            execution=previous.get(identity) or dict(status='NO RUN',runId=None,executionId=None,start=None,end=None,requestedAt=None,message='',jobs=[])
            blockers=[n for n in active_nodes if n['node_key'] in scope['relatedKeys']]
            message='; '.join(own_scope['issues']) if own_scope['issues'] else busy_message(blockers,graph['definition'],graph['definition']['workflowKeys'][identity]) if blockers else 'Workflow is disabled' if not wf['IS_ENABLED'] else ''
            if not wf.get('ALLOW_MANUAL_TRIGGER',True):message='Manual execution is disabled'
            jobs=[];saved={j['jobId']:j for j in execution['jobs']}
            members=[a for a in assignments if a['WORKFLOW_ID']==identity]
            for job in members:
                state=saved.get(job['JOB_ID']) or dict(status='NOT SELECTED' if execution['runId'] else 'NO RUN',runId=execution['runId'],start=None,end=None,message='',selected=False)
                old=next((j for j in previous.get(identity,{}).get('jobs',[]) if j['jobId']==job['JOB_ID']),None)
                jobs.append(dict(job,run=state,lastRun=old,progress=None,runOnly=False,hasSqlCondition=bool(job.get('SQL_CONDITION'))))
            for job in execution['jobs']:
                if not any(j['JOB_ID']==job['jobId'] for j in jobs):jobs.append(dict(JOB_ID=job['jobId'],JOB_NAME=job['name'],JOB_TYPE=job['type'],JOB_LAYER=job['layer'],JOB_IS_ENABLED=False,run=job,lastRun=job,progress=None,runOnly=True))
            counts={s:sum(j['status']==s for j in execution['jobs']) for s in ('SUCCESS','FAILED','SKIPPED','CANCELLED')};completed=sum(counts.values());total=len(execution['jobs'])
            schedule=next((s for s in schedules if s['plan'].get('workflowId')==identity),None)
            schedule_fields=dict(IS_SCHEDULE_ENABLED=bool(schedule and schedule['enabled']),SCHEDULE=schedule['cron'] if schedule else wf.get('SCHEDULE'),SCHEDULE_TIMEZONE=schedule['timezone'] if schedule else wf.get('SCHEDULE_TIMEZONE'))
            own=[n for n in active_nodes if n['node_key']==graph['definition']['workflowKeys'][identity]]
            waiting_count=sum(n['state']=='WAITING' and not n['actual_state'] for n in own)
            queued_count=sum(n['actual_state']=='QUEUED' or (n['actual_state']=='RUNNING' and not n['started_at']) for n in own)
            plan=planned.get(identity,root)
            scheduled=bool(schedule and schedule['enabled'] and wf['IS_ENABLED'])
            state=('CANCELLING' if identity in active and next((r['cancel_requested'] for r in records if r['workflow_id']==identity),False)
                else 'BLOCKED' if plan.get('dispatch',{}).get('canRetry')
                else 'WAITING' if waiting_count else 'QUEUED' if queued_count
                else 'BLOCKED' if plan['status']=='BLOCKED'
                else 'NEEDS REVIEW' if plan['status']=='UNKNOWN'
                else 'EXECUTING' if identity in active and root['status']=='RUNNING'
                else 'SCHEDULED' if scheduled else 'NO PLAN')
            planning=dict(status=state,waitingCount=waiting_count,queuedCount=queued_count,
                cascadeId=plan.get('cascadeId'),requestId=plan.get('requestId'),requestedAt=plan.get('requestedAt'),
                message=plan.get('message',''),waitingFor=plan.get('waitingFor',[]),scheduled=scheduled,
                nextRun=schedule['next_fire_at'] if scheduled else None,
                stopped=plan['status']=='BLOCKED',dispatch=plan.get('dispatch'),recovery=root.get('recovery'))
            rows.append(dict(dict(wf,**schedule_fields),source='POSTGRES',run=root,execution=execution,planning=planning,lastRun=previous.get(identity),jobs=jobs,canRun=not message,runUnavailable=message,
                canRunWithChildren=not scope['issues'],runWithChildrenUnavailable='; '.join(scope['issues']),
                runScope=scope['workflows'],graphId=str(graph['id']),configurationRevision=graph['revision'],cascadeId=root.get('cascadeId'),
                dispatch=root.get('dispatch'),canCancel=identity in active,cancelRequested=next((r['cancel_requested'] for r in records if r['workflow_id']==identity),False),
                progress=dict(total=total,succeeded=counts['SUCCESS'],failed=counts['FAILED'],skipped=counts['SKIPPED'],cancelled=counts['CANCELLED'],completed=completed,percent=round(100*completed/total) if total else 0),
                queuedCount=queued_count,waitingCount=waiting_count,
                scheduleAvailable=not scope['issues'],scheduleUnavailable='; '.join(scope['issues']),
                scheduler=dict(state='STARTED' if schedule and schedule['enabled'] else 'SUSPENDED',nextRun=schedule['next_fire_at'] if schedule else None,
                    id=str(schedule['id']) if schedule else None,revision=schedule['revision'] if schedule else None)))
        assigned={a['JOB_ID'] for a in assignments};unassigned=[j for j in catalog['jobs'] if j['JOB_ID'] not in assigned]
        if unassigned:
            rows.append(dict(source='POSTGRES',isUnassigned=True,WORKFLOW_ID='unassigned',WORKFLOW_NAME='Unassigned jobs',WORKFLOW_GROUP='Unassigned jobs',IS_ENABLED=False,IS_SCHEDULE_ENABLED=False,
                execution=dict(status='NO RUN',runId=None,start=None,end=None,message='No workflow assignment'),planning=dict(status='NO PLAN',waitingCount=0,queuedCount=0,waitingFor=[],scheduled=False),
                JOB_EXECUTION_MODE='SEQUENTIAL',run=dict(status='NOT RUN',runId=None,start=None,end=None,message='Assign these jobs to a workflow before execution'),lastRun=None,
                jobs=[dict(j,JOB_IS_ENABLED=j['IS_ENABLED'],JOB_SEQUENCE=i+1,run=dict(status='NOT RUN',runId=None,start=None,end=None,message='No workflow assignment'),lastRun=None,progress=None,runOnly=True) for i,j in enumerate(unassigned)],
                canRun=False,runUnavailable='These jobs have no workflow assignment',canCancel=False,progress=dict(total=0,succeeded=0,failed=0,skipped=0,percent=0),queuedCount=0,waitingCount=0))
        health='HEALTHY' if workers and workers['age']<=10 and workers['execution_enabled'] else 'DELAYED' if workers and workers['age']<=30 else 'UNAVAILABLE'
        return dict(ok=True,source='POSTGRES',rows=rows,workflowsCount=len(catalog['workflows']),jobsCount=len(catalog['jobs']),unassignedJobs=len(unassigned),
            dispatchErrors=[dict(cascadeId=str(e['id']),graphRevision=e['graph_revision'],requestedAt=e['created_at'],
                workflowNames=e['workflow_names'],attempts=e['attempts'],error=e['error'],canRetry=True) for e in dispatch_errors],
            generatedAt=now(),configurationRevision=graph['revision'],refreshSeconds=2,runsEnabled=os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()=='true',warnings=catalog['warnings'],submissions=[],
            worker=dict(state=health,lastSeen=workers['last_seen'] if workers else None),scheduleMessage='Schedules are managed by the backend. Accepted workflows remain WAITING until dependencies permit execution; imported schedules stay paused until explicitly enabled.')

    def history(self,args):
        from orchestration_history import filters,encode_cursor
        values=filters(args)
        with self.store.connect() as conn:rows=self.records(conn,values)
        more=len(rows)>values['limit'];rows=rows[:values['limit']]
        return dict(ok=True,source='POSTGRES',runs=[self.run(r) for r in rows],generatedAt=now(),
            nextCursor=encode_cursor(dict(REQUESTED_AT=rows[-1]['requested_at'],EXECUTION_ID=str(rows[-1]['id']))) if more else None)

    def log(self,workflow_id,node_id,job_id=None):
        with self.store.connect() as conn:
            rows=self.records(conn,node_id=node_id,workflow_id=workflow_id)
            if not rows:raise LookupError('Execution does not belong to this workflow')
            row=rows[0];root=self.run(row)
            if job_id and not any(j['jobId']==job_id for j in root['jobs']):raise LookupError('Job is not in this execution snapshot')
            events=conn.execute('SELECT * FROM kumo_task_engine.events WHERE run_id=%s ORDER BY sequence',(row['run_id'],)).fetchall() if row['run_id'] else []
        jobs=[j for j in root['jobs'] if not job_id or j['jobId']==job_id]
        kinds={j['type'] for j in root['jobs']}
        workflow_type=next(iter(kinds)) if len(kinds)==1 else 'MIXED'
        return dict(ok=True,source='POSTGRES',history=dict(WORKFLOW_NAME=root['workflowName'],WORKFLOW_ID=workflow_id,WORKFLOW_TYPE=workflow_type,STATUS=root['status'],
            REQUESTED_BY=root['requestedBy'],REQUESTED_AT=root['requestedAt'],START_TIME=root['start'],END_TIME=root['end'],TRIGGER_SOURCE='POSTGRES',ERROR_MESSAGE=root['message']),
            jobExecutions=[dict(JOB_POSITION=j['position'],JOB_NAME=j['name'],STATUS=j['status'],STARTED_AT=j['start'],ENDED_AT=j['end'],ERROR_MESSAGE=j['message'],QUERY_ID=j['queryId'],TASK_CLEANUP=j['cleanupState']) for j in jobs],
            runLog=[dict(LOG_DTTM=e['created_at'],ORIGIN='PostgreSQL worker',TYPE=e['kind'],MESSAGE=e['facts']) for e in events],modelProgress=[],testProgress=[],warnings={})
