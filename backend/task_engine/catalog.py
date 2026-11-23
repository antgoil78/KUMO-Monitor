"""The complete DEV catalog in PostgreSQL, with derived execution capabilities.

Import is explicit. Reads and edits never access Snowflake. Compiled graph nodes
are derived from the master catalog; incompatible definitions remain visible.
"""
from copy import deepcopy
import hashlib
import json
import os
import uuid

from .dependencies import digest,graph_definition,record_revision
from .store import Conflict,normalize

CATALOG_KEY='dev-workflow-catalog'


def local_catalog(raw):
    import orchestration_configuration as schema
    data=deepcopy(raw)
    data.update(schema.normalize_dependencies(data))
    return data


def workflow_key(identity):
    return 'w'+hashlib.sha256(identity.encode()).hexdigest()[:20]


def compile_catalog(body):
    """Keep every definition, compile only supported SQL, explain every exclusion."""
    if set(body)-{'key','name','enabled','catalog','version','nodes','workflowKeys','executionIssues'}:
        raise ValueError('Invalid catalog graph fields')
    raw=body['catalog']
    if not isinstance(raw,dict) or set(raw)!={'workflows','jobs','raw_records'}:
        raise ValueError('A catalog contains workflows, jobs and raw_records')
    data=local_catalog(raw)
    workflows={w['WORKFLOW_ID']:w for w in data['workflows']};jobs={j['JOB_ID']:j for j in data['jobs']}
    if len(workflows)!=len(data['workflows']) or len(jobs)!=len(data['jobs']):
        raise ValueError('Catalog identities must be unique')
    for rows,field in [(data['workflows'],'WORKFLOW_NAME'),(data['jobs'],'JOB_NAME')]:
        if any(not isinstance(r.get(field),str) or not r[field].strip() for r in rows) or len({r[field].strip().casefold() for r in rows})!=len(rows):
            raise ValueError('Workflow and job names must be nonempty and unique')
    keys={identity:workflow_key(identity) for identity in workflows};issues={identity:[] for identity in workflows};nodes=[]
    for identity,wf in workflows.items():
        reason=issues[identity];rules=[r for r in data['raw_records'] if r.get('WORKFLOW_ID')==identity]
        headers=[r for r in rules if not r.get('JOB_ID')];members=[r for r in rules if r.get('JOB_ID')]
        if identity in data['invalidWorkflows']:reason.append('Resolve invalid or duplicate dependency references')
        if not members:reason.append('No jobs assigned')
        if not headers:reason.append('Workflow trigger configuration is missing')
        if wf.get('WAREHOUSE_NAME') and wf['WAREHOUSE_NAME'].upper()!=os.getenv('KUMO_PG_TASK_WAREHOUSE','KUMO_ELT_GEN_1').upper():
            reason.append('The configured warehouse differs from this worker\'s warehouse')
        parents=[]
        if len(headers)>1 and any(not r.get('PARENT_ID') for r in headers):reason.append('Mixed root and parent rules')
        for rule in headers:
            parent=rule.get('PARENT_ID')
            if rule.get('SQL'):reason.append('SQL dependency conditions are not implemented in this worker')
            if rule.get('TRIGGER_TYPE')!=('DEP' if parent else None):reason.append('This trigger type is not implemented in the PostgreSQL worker')
            if parent:
                if parent not in keys:reason.append('Parent workflow is missing')
                else:parents.append(dict(key=keys[parent],success=rule.get('SUCCESS'),warning=rule.get('WARNING'),error=rule.get('ERROR'),allow=rule.get('ALLOW') is not False,parentMustBeNewer=bool(rule.get('PARENT_MUST_BE_NEWER'))))
        ordered=[];remaining=list(members);parent=identity
        while remaining:
            candidates=[r for r in remaining if r.get('PARENT_ID')==parent]
            if len(candidates)!=1:
                reason.append('Job assignments must form one ordered chain');break
            rule=candidates[0];job=jobs.get(rule['JOB_ID'])
            if job is None:reason.append('Assigned job is missing');break
            if job.get('JOB_TYPE')!='SQL':reason.append(str(job.get('JOB_TYPE') or 'Unknown')+' execution is not enabled in this worker')
            if rule.get('ALLOW') is False or rule.get('SQL') or any(rule.get(f) is not None for f in ('SUCCESS','WARNING','ERROR')):
                reason.append('Job dependency gates require additional execution support')
            if any(job.get(f) for f in ('ON_SUCCESS_COMMAND_LINE','ON_WARNING_COMMAND_LINE','ON_ERROR_COMMAND_LINE')):
                reason.append('Job outcome commands require additional execution support')
            ordered.append(dict(key=job['JOB_ID'],name=job['JOB_NAME'],description=job.get('DESCRIPTION') or '',layer=job.get('JOB_LAYER') or 'GENERAL',
                enabled=bool(job.get('IS_ENABLED')),sql=job.get('SQL_COMMAND'),timeoutSeconds=int(job.get('TIMEOUT_SECONDS') or 60)))
            remaining.remove(rule);parent=job['JOB_ID']
        node=dict(key=keys[identity],name=wf['WORKFLOW_NAME'],group=wf.get('WORKFLOW_GROUP') or '',description=wf.get('DESCRIPTION') or '',
            enabled=bool(wf.get('IS_ENABLED')),allow=not headers or bool(headers[0].get('PARENT_ID')) or headers[0].get('ALLOW') is not False,
            plan=dict(mode=wf.get('JOB_EXECUTION_MODE') or 'SEQUENTIAL',jobs=ordered),parents=parents)
        if not reason:
            try:graph_definition(dict(key='validation',name='Validation',nodes=[dict(node,parents=[])]))
            except (ValueError,TypeError) as exc:reason.append(str(exc))
        if not reason:nodes.append(node)
    # Unsupported ancestors cannot be silently omitted from a runnable graph.
    changed=True
    while changed:
        changed=False
        for identity in workflows:
            for r in data['raw_records']:
                if r.get('WORKFLOW_ID')==identity and not r.get('JOB_ID') and r.get('PARENT_ID') in issues and issues[r['PARENT_ID']] and not issues[identity]:
                    issues[identity].append('Parent workflow requires unsupported execution features');changed=True
    supported={keys[i] for i in workflows if not issues[i]}
    compiled=graph_definition(dict(key=body['key'],name=body['name'],enabled=body.get('enabled',True),nodes=[n for n in nodes if n['key'] in supported])) if supported else dict(key=body['key'],name=body['name'],enabled=body.get('enabled',True),nodes=[],version=1)
    return dict(compiled,catalog=deepcopy(raw),workflowKeys=keys,executionIssues={k:list(dict.fromkeys(v)) for k,v in issues.items()})


def read_source():
    import config
    import snowflake_client as sf
    import orchestration_configuration as schema
    if config.ORCHESTRATION_NAMESPACE!='KUMO_DEV.MONITOR_META':raise ValueError('Import is DEV only')
    with sf.connection_scope(force_service=True):data=schema.read_catalog()
    return {key:data[key] for key in ('workflows','jobs','raw_records')}


def master(store):
    with store.connect() as conn:
        return conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE key=%s',(CATALOG_KEY,)).fetchone()


def import_catalog(store,raw,actor):
    """Initial promotion only; retain existing PostgreSQL WF1 trial edits."""
    from psycopg.types.json import Jsonb
    raw=json.loads(json.dumps(raw,default=str))
    with store.transaction() as conn:
        if conn.execute('SELECT id FROM kumo_task_engine.dependency_graphs WHERE key=%s',(CATALOG_KEY,)).fetchone():
            raise Conflict('The PostgreSQL catalog is already installed; edit the master instead of re-importing Snowflake')
        trial=conn.execute("SELECT * FROM kumo_task_engine.dependency_graphs WHERE key='wf1-sql-trial'").fetchone()
        if trial:
            if conn.execute("SELECT id FROM kumo_task_engine.cascades WHERE graph_id=%s AND state IN ('QUEUED','RUNNING','UNKNOWN')",(trial['id'],)).fetchone():
                raise Conflict('Finish the active WF1 trial before promoting the complete catalog')
            ids=trial['source'].get('workflows',{})
            for node in trial['definition']['nodes']:
                identity=ids.get(node['key'])
                workflow=next((w for w in raw['workflows'] if w['WORKFLOW_ID']==identity),None)
                if workflow is None:raise ValueError('The WF1 master workflow is missing from the imported catalog')
                workflow.update(WORKFLOW_NAME=node['name'],IS_ENABLED=node['enabled'],JOB_EXECUTION_MODE=node['plan']['mode'])
                for field,target in [('group','WORKFLOW_GROUP'),('description','DESCRIPTION')]:
                    if field in node:workflow[target]=node[field]
                for job in node['plan']['jobs']:
                    target=next((j for j in raw['jobs'] if j['JOB_ID']==job['key']),None)
                    if not target:raise ValueError('The WF1 master SQL job is missing from the imported catalog')
                    target.update(SQL_COMMAND=job['sql'],TIMEOUT_SECONDS=job['timeoutSeconds'])
                    for field,key in [('name','JOB_NAME'),('description','DESCRIPTION'),('layer','JOB_LAYER'),('enabled','IS_ENABLED')]:
                        if field in job:target[key]=job[field]
                raw['raw_records']=[r for r in raw['raw_records'] if r['WORKFLOW_ID']!=identity]
                for rule in node['parents'] or [None]:
                    raw['raw_records'].append(dict(WORKFLOW_ID=identity,JOB_ID=None,PARENT_ID=ids[rule['key']] if rule else None,TRIGGER_TYPE='DEP' if rule else None,
                        SUCCESS=rule['success'] if rule else None,WARNING=rule['warning'] if rule else None,ERROR=rule['error'] if rule else None,SQL=None,
                        ALLOW=rule['allow'] if rule else node['allow'],PARENT_MUST_BE_NEWER=rule['parentMustBeNewer'] if rule else False))
                for index,job in enumerate(node['plan']['jobs']):
                    raw['raw_records'].append(dict(WORKFLOW_ID=identity,JOB_ID=job['key'],PARENT_ID=node['plan']['jobs'][index-1]['key'] if index else identity,
                        TRIGGER_TYPE='DEP',SUCCESS=None,WARNING=None,ERROR=None,SQL=None,ALLOW=True,PARENT_MUST_BE_NEWER=False))
        definition=compile_catalog(dict(key=CATALOG_KEY,name='DEV workflow catalog',enabled=True,catalog=raw))
        row=conn.execute('''INSERT INTO kumo_task_engine.dependency_graphs(id,key,name,enabled,definition,definition_hash,source,created_by,updated_by,configuration_managed)
            VALUES(%s,%s,%s,true,%s,%s,%s,%s,%s,true) RETURNING *''',(uuid.uuid4(),CATALOG_KEY,definition['name'],Jsonb(definition),digest(definition),
            Jsonb(dict(namespace='KUMO_DEV.MONITOR_META',scope='Complete workflow and job configuration; schedules copied as paused PostgreSQL intent')),actor,actor)).fetchone()
        record_revision(conn,row)
    return row


class CatalogConfiguration:
    def __init__(self,store):self.store=store

    def graph(self,conn):
        row=conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE key=%s',(CATALOG_KEY,)).fetchone()
        if not row:raise LookupError('Import the DEV workflow catalog first')
        return row

    def payload(self,conn,graph):
        import orchestration_configuration as schema
        result=schema.payload(local_catalog(graph['definition']['catalog']))
        # Source schedules are retained, but never implicitly enabled on import.
        for workflow in result['workflows']:
            workflow['SOURCE_SCHEDULE_ENABLED']=bool(workflow.get('IS_SCHEDULE_ENABLED'))
            workflow['IS_SCHEDULE_ENABLED']=False
        history=conn.execute('SELECT revision,changed_by,changed_at FROM kumo_task_engine.graph_revisions WHERE graph_id=%s ORDER BY revision DESC LIMIT 30',(graph['id'],)).fetchall()
        return dict(result,source='PostgreSQL · DEV workflow catalog',configurationStore='POSTGRES',catalogScope='ALL',graphId=graph['id'],revision=graph['revision'],revisions=history,
            executionIssues=graph['definition']['executionIssues'])

    def read(self):
        with self.store.connect() as conn:return self.payload(conn,self.graph(conn))

    def change(self,body,actor,mutate):
        from psycopg.types.json import Jsonb
        if not isinstance(body,dict) or type(body.get('revision')) is not int:raise ValueError('Supply the loaded configuration revision')
        with self.store.transaction() as conn:
            graph=self.graph(conn)
            if graph['revision']!=body['revision']:raise Conflict('Configuration changed in another editor. Close this dialog and refresh before saving again.')
            raw=deepcopy(graph['definition']['catalog']);mutate(raw)
            definition=compile_catalog(dict(key=CATALOG_KEY,name=graph['name'],enabled=graph['enabled'],catalog=raw))
            if digest(definition)!=graph['definition_hash']:
                graph=conn.execute('''UPDATE kumo_task_engine.dependency_graphs SET definition=%s,definition_hash=%s,revision=revision+1,
                    updated_by=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *''',(Jsonb(definition),digest(definition),str(actor)[:256],graph['id'])).fetchone()
                record_revision(conn,graph)
            return self.payload(conn,graph)

    def workflow(self,identity,body,actor):
        def mutate(raw):
            row=next((w for w in raw['workflows'] if w['WORKFLOW_ID']==identity),None)
            if not row:raise LookupError('Workflow not found')
            if body.get('scheduleEnabled') or ('allowExternalTrigger' in body and body['allowExternalTrigger']!=bool(row.get('ALLOW_EXTERNAL_TRIGGER'))) or ('allowManualTrigger' in body and body['allowManualTrigger']!=bool(row.get('ALLOW_MANUAL_TRIGGER',True))):
                raise ValueError('Graph schedules and external/partial triggers are not enabled yet')
            fields={'workflowName':'WORKFLOW_NAME','workflowGroup':'WORKFLOW_GROUP','description':'DESCRIPTION','enabled':'IS_ENABLED','jobExecutionMode':'JOB_EXECUTION_MODE'}
            for key,target in fields.items():
                if key in body:row[target]=body[key]
            if type(row['IS_ENABLED']) is not bool or row['JOB_EXECUTION_MODE'] not in ('SEQUENTIAL','PARALLEL'):raise ValueError('Invalid workflow enabled flag or execution mode')
        return self.change(body,actor,mutate)

    def job(self,identity,body,actor):
        def mutate(raw):
            row=next((j for j in raw['jobs'] if j['JOB_ID']==identity),None)
            if not row:raise LookupError('Job not found')
            restricted=False
            if row.get('JOB_TYPE')=='SQL':
                try:
                    normalize(dict(requestId='catalog-validation',jobs=[dict(key=identity,sql=row.get('SQL_COMMAND'),timeoutSeconds=int(row.get('TIMEOUT_SECONDS') or 60))]))
                    restricted=True
                except (ValueError,TypeError):pass
            fields={'jobName':'JOB_NAME','description':'DESCRIPTION','enabled':'IS_ENABLED','jobLayer':'JOB_LAYER','sqlCommand':'SQL_COMMAND','jobType':'JOB_TYPE',
                    'dbtCommand':'DBT_COMMAND','dbtTarget':'DBT_TARGET','dbtWorkspace':'DBT_WORKSPACE','dbtProjectFqn':'DBT_PROJECT_FQN','commandLine':'COMMAND_LINE'}
            for key,target in fields.items():
                if key in body:row[target]=body[key]
            if type(row['IS_ENABLED']) is not bool or row['JOB_TYPE'] not in ('SQL','DBT','COMMAND'):raise ValueError('Invalid job type or enabled flag')
            if 'timeoutSeconds' in body:
                value=body['timeoutSeconds']
                if isinstance(value,bool) or not str(value).isdigit() or not 1<=int(value)<=86400:raise ValueError('Timeout must be an integer from 1 to 86400 seconds')
                row['TIMEOUT_SECONDS']=int(value)
            if restricted:
                if row['JOB_TYPE']!='SQL':raise ValueError('Changing a validated SQL job to another execution type requires a later migration')
                normalize(dict(requestId='catalog-validation',jobs=[dict(key=identity,sql=row.get('SQL_COMMAND'),timeoutSeconds=int(row.get('TIMEOUT_SECONDS') or 60))]))
            row['UPDATED_BY']=str(actor)[:256]
        return self.change(body,actor,mutate)

    def dependencies(self,body,actor):
        def mutate(raw):
            import orchestration_configuration as schema
            catalog=local_catalog(raw);before=schema.configuration_rows(catalog)
            rows=body.get('rows')
            if not isinstance(rows,list):raise ValueError('Supply dependency rows')
            if sorted((r for r in rows if r.get('jobId')),key=lambda r:r['id'])!=sorted((r for r in before if r['jobId']),key=lambda r:r['id']):raise ValueError('Edit job assignments on the Workflow page')
            normalized=schema.validate_rows(rows,catalog);by_id={r['id']:r for r in normalized}
            raw['raw_records']=[dict(WORKFLOW_ID=r['workflowId'],JOB_ID=r['jobId'],
                PARENT_ID=by_id[r['parentId']]['jobId'] or by_id[r['parentId']]['workflowId'] if r['parentId'] else None,
                TRIGGER_TYPE=r['trigger'],SUCCESS=r['success'],WARNING=r['warning'],ERROR=r['error'],SQL=r['sql'] or None,ALLOW=r['allow'],PARENT_MUST_BE_NEWER=r['parentMustBeNewer']) for r in normalized]
        return self.change(body,actor,mutate)

    def orders(self,body,actor):
        def mutate(raw):
            import orchestration_configuration as schema
            catalog=local_catalog(raw);existing=schema.assignments(catalog);seen=set()
            for order in body.get('orders',[]):
                identity=order['workflowId'];ids=order['jobIds'];current=[j['JOB_ID'] for j in existing if j['WORKFLOW_ID']==identity]
                if identity in seen or len(ids)!=len(current) or set(ids)!=set(current):raise ValueError('Reorder existing jobs only')
                seen.add(identity)
                for index,job in enumerate(ids):
                    row=next(r for r in raw['raw_records'] if r['WORKFLOW_ID']==identity and r['JOB_ID']==job)
                    row['PARENT_ID']=ids[index-1] if index else identity
        return self.change(body,actor,mutate)
