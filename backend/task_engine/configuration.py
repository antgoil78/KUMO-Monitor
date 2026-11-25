"""The WF1 DEV graph is the master configuration, adapted to existing editors.

No Snowflake imports or calls belong here. Configuration and revision history
commit together under the same lock used by execution admission.
"""
from copy import deepcopy

from .dependencies import digest, graph_definition, record_revision
from .store import Conflict

TRIAL_KEY = 'wf1-sql-trial'


def rechain(node):
    previous = []
    for job in node['plan']['jobs']:
        job['dependsOn'] = previous[-1:] if node['plan']['mode']=='SEQUENTIAL' else []
        previous.append(job['key'])


class Configuration:
    def __init__(self, store):
        self.store = store

    def graph(self, conn):
        graph = conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE key=%s',(TRIAL_KEY,)).fetchone()
        if not graph:
            raise LookupError('Install the WF1 SQL trial before editing its PostgreSQL configuration')
        if not graph['configuration_managed']:
            raise Conflict('Apply the PostgreSQL master configuration migration first')
        if {n['key'] for n in graph['definition']['nodes']} != {'WF1','WF2','WF3','WF4'}:
            raise Conflict('The configuration editor is limited to the WF1–WF4 trial')
        return graph

    def read(self):
        with self.store.connect() as conn:
            return self.payload(conn,self.graph(conn))

    def payload(self, conn, graph):
        workflows=[];jobs={};assignments=[];rows=[]
        for node in graph['definition']['nodes']:
            key=node['key'];plan=node['plan']
            workflows.append(dict(WORKFLOW_ID=key,WORKFLOW_NAME=node['name'],WORKFLOW_GROUP=node.get('group','DEPENDENCY TEST'),
                DESCRIPTION=node.get('description',''),JOB_EXECUTION_MODE=plan['mode'],IS_ENABLED=node['enabled'],
                ALLOW_MANUAL_TRIGGER=True,ALLOW_EXTERNAL_TRIGGER=False,SCHEDULE='',SCHEDULE_TIMEZONE='UTC',IS_SCHEDULE_ENABLED=False))
            for index,rule in enumerate(node['parents'] or [None]):
                rows.append(dict(id=key if index==0 else key+':parent:'+rule['key'],workflowId=key,jobId=None,name=node['name'],
                    parentId=rule['key'] if rule else None,trigger='DEP' if rule else None,sql='',
                    success=rule['success'] if rule else None,warning=rule['warning'] if rule else None,error=rule['error'] if rule else None,
                    allow=rule['allow'] if rule else node['allow'],parentMustBeNewer=rule['parentMustBeNewer'] if rule else False))
            for index,job in enumerate(plan['jobs']):
                job_key=job['key'];name=job.get('name',node['name']+' SQL' if len(plan['jobs'])==1 else job_key)
                jobs[job_key]=dict(JOB_ID=job_key,JOB_NAME=name,DESCRIPTION=job.get('description',''),JOB_TYPE='SQL',
                    JOB_LAYER=job.get('layer','GENERAL'),SQL_COMMAND=job['sql'],TIMEOUT_SECONDS=job['timeoutSeconds'],IS_ENABLED=job.get('enabled',True),
                    CREATED_BY=graph['created_by'],CREATED_AT=graph['created_at'],UPDATED_BY=graph['updated_by'],UPDATED_AT=graph['updated_at'])
                parent=plan['jobs'][index-1]['key'] if index else None
                assignments.append(dict(WORKFLOW_ID=key,JOB_ID=job_key,JOB_SEQUENCE=index+1,
                    PARENT_WORKFLOW_ID=key if not parent else None,PARENT_JOB_ID=parent))
                rows.append(dict(id=key+':job:'+job_key,workflowId=key,jobId=job_key,name=name,
                    parentId=key+':job:'+parent if parent else key,trigger='DEP',success=None,warning=None,error=None,sql='',allow=True,parentMustBeNewer=False))
        history=conn.execute('SELECT revision,changed_by,changed_at FROM kumo_task_engine.graph_revisions WHERE graph_id=%s ORDER BY revision DESC LIMIT 30',(graph['id'],)).fetchall()
        return dict(ok=True,source='PostgreSQL · WF1–WF4 DEV trial',configurationStore='POSTGRES',graphId=graph['id'],revision=graph['revision'],
            workflows=workflows,jobs=list(jobs.values()),assignments=assignments,rows=rows,warnings=[],invalidWorkflows=[],revisions=history)

    def change(self, body, actor, mutate):
        from psycopg.types.json import Jsonb
        if not isinstance(body,dict) or isinstance(body.get('revision'),bool) or not isinstance(body.get('revision'),int):
            raise ValueError('Supply the configuration revision loaded by the editor')
        with self.store.transaction() as conn:
            graph=self.graph(conn)
            # Check before considering a no-op: a stale editor must refresh.
            if graph['revision']!=body['revision']:
                raise Conflict('Configuration changed in another editor. Close this dialog and refresh before saving again.')
            definition=deepcopy(graph['definition'])
            mutate(definition,self.payload(conn,graph))
            definition.pop('version',None)
            for node in definition['nodes']:
                node['plan'].pop('version',None)
            definition=graph_definition(definition);fingerprint=digest(definition)
            if fingerprint!=graph['definition_hash']:
                graph=conn.execute('''UPDATE kumo_task_engine.dependency_graphs SET definition=%s,definition_hash=%s,
                    revision=revision+1,updated_by=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *''',
                    (Jsonb(definition),fingerprint,str(actor)[:256],graph['id'])).fetchone()
                record_revision(conn,graph)
            return self.payload(conn,graph)

    def workflow(self, workflow_id, body, actor):
        fields={'revision','workflowName','workflowGroup','description','jobExecutionMode','enabled','schedule','scheduleTimezone','scheduleEnabled','allowManualTrigger','allowExternalTrigger','entryJobId'}
        def mutate(definition,_):
            if set(body)-fields:
                raise ValueError('Unsupported workflow configuration field')
            if body.get('schedule') or body.get('scheduleEnabled') or body.get('allowExternalTrigger') or body.get('entryJobId') or body.get('allowManualTrigger',True) is not True:
                raise ValueError('This trial uses full graph manual execution; schedule and external trigger changes are not supported yet')
            node=next((n for n in definition['nodes'] if n['key']==workflow_id),None)
            if node is None:
                raise LookupError('Workflow is outside the WF1–WF4 trial')
            mapping={'workflowName':'name','workflowGroup':'group','description':'description','enabled':'enabled'}
            for field,target in mapping.items():
                if field in body:node[target]=body[field]
            if 'jobExecutionMode' in body:
                node['plan']['mode']=body['jobExecutionMode'];rechain(node)
        return self.change(body,actor,mutate)

    def job(self, job_id, body, actor):
        fields={'revision','jobName','description','jobType','jobLayer','sqlCommand','timeoutSeconds','enabled','dbtCommand','commandLine','dbtTarget','dbtWorkspace','dbtProjectFqn'}
        def mutate(definition,_):
            if set(body)-fields or body.get('jobType','SQL')!='SQL' or body.get('dbtCommand') or body.get('commandLine'):
                raise ValueError('Only SQL job settings are supported by this trial')
            matches=[j for n in definition['nodes'] for j in n['plan']['jobs'] if j['key']==job_id]
            if not matches:
                raise LookupError('Job is outside the WF1–WF4 trial')
            for job in matches:
                for field,target in [('jobName','name'),('description','description'),('jobLayer','layer'),('sqlCommand','sql'),('enabled','enabled')]:
                    if field in body:job[target]=body[field]
                if 'timeoutSeconds' in body:
                    timeout=body['timeoutSeconds']
                    if isinstance(timeout,bool) or not isinstance(timeout,(str,int)) or not str(timeout).isdigit():
                        raise ValueError('Timeout must be an integer from 1 to 300 seconds')
                    job['timeoutSeconds']=int(timeout)
        return self.change(body,actor,mutate)

    def orders(self, body, actor):
        def mutate(definition,_):
            if set(body)-{'revision','orders'} or not isinstance(body.get('orders'),list) or not body['orders']:
                raise ValueError('Supply workflow job orders and a configuration revision')
            seen=set()
            for order in body['orders']:
                if not isinstance(order,dict) or set(order)!={'workflowId','jobIds'} or not isinstance(order['workflowId'],str) or order['workflowId'] in seen:
                    raise ValueError('Each workflow order must be distinct')
                seen.add(order['workflowId'])
                node=next((n for n in definition['nodes'] if n['key']==order['workflowId']),None)
                if node is None:raise LookupError('Workflow is outside the WF1–WF4 trial')
                jobs={j['key']:j for j in node['plan']['jobs']};ids=order['jobIds']
                if not isinstance(ids,list) or any(not isinstance(k,str) for k in ids) or len(ids)!=len(jobs) or set(ids)!=set(jobs):
                    raise ValueError('Reorder existing jobs only; adding or removing assignments is outside this trial')
                node['plan']['jobs']=[jobs[k] for k in ids];rechain(node)
        return self.change(body,actor,mutate)

    def dependencies(self, body, actor):
        def mutate(definition,payload):
            rows=body.get('rows')
            if set(body)-{'rows','revision'} or not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):
                raise ValueError('Supply dependency rows and the configuration revision')
            identities=[r.get('id') for r in rows]
            if any(not isinstance(i,str) or not i for i in identities) or len(set(identities))!=len(identities):
                raise ValueError('Dependency row identities must be unique')
            # The existing dependency screen carries assignments; it cannot edit them.
            if sorted((r for r in rows if r.get('jobId')),key=lambda r:r['id']) != sorted((r for r in payload['rows'] if r['jobId']),key=lambda r:r['id']):
                raise ValueError('Edit jobs and assignments on the Workflow page')
            workflow_rows=[r for r in rows if not r.get('jobId')]
            keys={n['key'] for n in definition['nodes']}
            if {r.get('workflowId') for r in workflow_rows}!=keys:
                raise ValueError('Keep exactly the WF1–WF4 workflows in this trial')
            for node in definition['nodes']:
                rules=[r for r in workflow_rows if r['workflowId']==node['key']]
                if not any(r['id']==node['key'] for r in rules) or any(r.get('name')!=node['name'] for r in rules):
                    raise ValueError('Workflow identity or name changed; refresh before editing dependencies')
                if any(r.get('sql') or r.get('trigger')!=('DEP' if r.get('parentId') else None) for r in rules):
                    raise ValueError('Only stream-free DEP rules are supported; SQL conditions and RUNNER polling are excluded')
                parents=[r for r in rules if r.get('parentId')]
                if len(parents)!=len(rules) and len(rules)!=1:
                    raise ValueError('Use either one root row or distinct parent rules')
                if not parents:
                    root=rules[0]
                    if any(root.get(f) is not None for f in ('success','warning','error')) or root.get('parentMustBeNewer'):
                        raise ValueError('Root workflows cannot have parent outcome or freshness rules')
                    if not isinstance(root.get('allow'),bool):raise ValueError('ALLOW must be boolean')
                    node['allow']=root['allow']
                else:
                    node['allow']=True
                node['parents']=[dict(key=r['parentId'],**{f:r.get(f) for f in ('success','warning','error','allow','parentMustBeNewer')}) for r in parents]
        return self.change(body,actor,mutate)
