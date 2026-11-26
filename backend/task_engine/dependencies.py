"""Frozen workflow dependency trials. All admission and event work is PostgreSQL-only."""
from datetime import timedelta
import hashlib
import json
import re
import uuid

from .store import Conflict, TERMINAL, WorkflowBusy, normalize
from .decisions import record as record_decision

NODE_DONE = TERMINAL | {'BLOCKED'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def graph_definition(body):
    if isinstance(body,dict) and 'catalog' in body:
        from .catalog import compile_catalog
        return compile_catalog(body)
    if not isinstance(body, dict) or set(body)-{'key','name','enabled','nodes'}:
        raise ValueError('Use key, name, enabled and nodes for a dependency trial')
    key, name, enabled, nodes = body.get('key'), body.get('name'), body.get('enabled', True), body.get('nodes')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key):
        raise ValueError('Graph key must be a simple identifier of at most 64 characters')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or not isinstance(enabled, bool):
        raise ValueError('Supply a graph name and a boolean enabled flag')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 512:
        raise ValueError('Use 1–512 SQL-only workflow nodes')
    definitions = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node)-{'key','name','enabled','allow','plan','parents','description','group'}:
            raise ValueError('Nodes accept key, name, enabled, allow, plan and parents')
        node_key, node_name = node.get('key'), node.get('name', node.get('key'))
        if not isinstance(node_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,24}', node_key) or node_key in definitions:
            raise ValueError('Node keys must be unique simple identifiers of at most 24 characters')
        if not isinstance(node_name, str) or not 1 <= len(node_name.strip()) <= 100:
            raise ValueError('Invalid workflow node name')
        if not isinstance(node.get('enabled', True), bool) or not isinstance(node.get('allow', True), bool):
            raise ValueError('Workflow enabled/allow flags must be boolean')
        plan = node.get('plan')
        if not isinstance(plan, dict) or set(plan)-{'jobs','mode'}:
            raise ValueError('Node plans contain jobs and optional mode; business commands and conditions are excluded')
        jobs = plan.get('jobs')
        if not isinstance(jobs, list):
            raise ValueError('A node plan requires SQL jobs')
        metadata = []
        for job in jobs:
            if not isinstance(job, dict) or set(job)-{'key','sql','command','type','timeoutSeconds','dependsOn','name','description','layer','enabled'}:
                raise ValueError('Invalid SQL job configuration')
            if not isinstance(job.get('enabled', True), bool):
                raise ValueError('Job enabled must be boolean')
            details = {k:job[k] for k in ('name','description','layer','enabled') if k in job}
            for field, limit in [('name',100),('description',2000),('layer',20)]:
                if field in details and (not isinstance(details[field],str) or len(details[field])>limit or (field=='name' and not details[field].strip())):
                    raise ValueError('Invalid job '+field)
            if details.get('layer','GENERAL') not in ('RAW','SDL','EDV','BDL','GENERAL'):
                raise ValueError('Invalid job layer')
            metadata.append(details)
        _, frozen = normalize(dict(plan, jobs=[{k:v for k,v in job.items() if k not in ('name','description','layer','enabled')} for job in jobs], requestId='dependency-validation'))
        for job, details in zip(frozen['jobs'],metadata):
            job.update(details)
        parents = node.get('parents', [])
        if not isinstance(parents, list):
            raise ValueError('parents must be a list of workflow rules')
        rules = []; seen = set()
        for rule in parents:
            if not isinstance(rule, dict) or set(rule)-{'key','success','warning','error','allow','parentMustBeNewer'}:
                raise ValueError('Parent rules accept key, success, warning, error, allow and parentMustBeNewer')
            parent = rule.get('key')
            if not isinstance(parent, str) or parent == node_key or parent in seen:
                raise ValueError('Parent references must be distinct and cannot refer to themselves')
            value = dict(key=parent, success=rule.get('success', True), warning=rule.get('warning', False), error=rule.get('error', False),
                         allow=rule.get('allow', True), parentMustBeNewer=rule.get('parentMustBeNewer', False))
            if any(v is not None and not isinstance(v, bool) for k,v in value.items() if k in ('success','warning','error')):
                raise ValueError('Outcome rule flags must be true, false or null')
            if not isinstance(value['allow'], bool) or not isinstance(value['parentMustBeNewer'], bool):
                raise ValueError('Rule allow/freshness flags must be boolean')
            seen.add(parent); rules.append(value)
        extra = {k:node[k] for k in ('description','group') if k in node}
        if any(not isinstance(v,str) or len(v)>(2000 if k=='description' else 100) for k,v in extra.items()):
            raise ValueError('Invalid workflow description or group')
        definitions[node_key] = dict(key=node_key, name=node_name.strip(), enabled=node.get('enabled', True), allow=node.get('allow', True), plan=frozen, parents=sorted(rules, key=lambda r:r['key']), **extra)
    if len({n['name'].casefold() for n in definitions.values()}) != len(definitions):
        raise ValueError('Workflow names must be unique')
    ordered = []; visiting = set(); visited = set()
    def visit(node_key):
        if node_key not in definitions:
            raise ValueError('Dependency references a workflow outside this trial graph')
        if node_key in visiting:
            raise ValueError('Workflow dependencies contain a cycle')
        if node_key in visited:
            return
        visiting.add(node_key)
        for rule in definitions[node_key]['parents']:
            visit(rule['key'])
        visiting.remove(node_key); visited.add(node_key); ordered.append(definitions[node_key])
    for node_key in sorted(definitions):
        visit(node_key)
    return dict(key=key, name=name.strip(), enabled=enabled, nodes=ordered, version=1)


def accepts(state, rule):
    flag = {'SUCCESS':'success', 'WARNING':'warning', 'FAILED':'error'}.get(state)
    return bool(rule['allow'] and flag and rule[flag] is not False)


def enqueue(conn, cascade_id):
    conn.execute('''INSERT INTO kumo_task_engine.dependency_work(cascade_id) VALUES(%s)
        ON CONFLICT(cascade_id) DO UPDATE SET state='PENDING',next_check_at=clock_timestamp(),attempts=0,error=NULL''', (cascade_id,))


def execution_plan(plan):
    """Omit disabled jobs and retain their prerequisite chains for enabled jobs."""
    by_key = {j['key']:j for j in plan['jobs']}
    def prerequisites(key):
        job = by_key[key]
        return [key] if job.get('enabled',True) else [p for dep in job['dependsOn'] for p in prerequisites(dep)]
    return dict(mode=plan['mode'], jobs=[dict({k:j[k] for k in ('sql','command','type') if k in j},key=j['key'],timeoutSeconds=j['timeoutSeconds'],
        dependsOn=list(dict.fromkeys(p for dep in j['dependsOn'] for p in prerequisites(dep))))
        for j in plan['jobs'] if j.get('enabled',True)])


def record_revision(conn, graph):
    from psycopg.types.json import Jsonb
    conn.execute('''INSERT INTO kumo_task_engine.graph_revisions(graph_id,revision,definition,definition_hash,changed_by)
        VALUES(%s,%s,%s,%s,%s)''',
        (graph['id'],graph['revision'],Jsonb(graph['definition']),graph['definition_hash'],graph['updated_by']))


class Graphs:
    def __init__(self, store):
        self.store = store

    def install(self, body, actor, source=None, expected_revision=None):
        from psycopg.types.json import Jsonb
        definition = graph_definition(body); fingerprint = digest(definition)
        with self.store.transaction() as conn:
            previous = conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE key=%s', (definition['key'],)).fetchone()
            if previous:
                if previous['definition_hash'] == fingerprint:
                    return dict(ok=True, graph=previous, reused=True)
                if previous['configuration_managed']:
                    raise Conflict('PostgreSQL owns this configuration; edit it through the configuration screens instead of re-importing Snowflake')
                if previous['revision'] != expected_revision:
                    raise Conflict('Graph changed; supply its current expected revision before importing a new snapshot')
                row = conn.execute('''UPDATE kumo_task_engine.dependency_graphs SET name=%s,enabled=%s,definition=%s,
                    definition_hash=%s,source=%s,revision=revision+1,updated_by=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *''',
                    (definition['name'], definition['enabled'], Jsonb(definition), fingerprint, Jsonb(source or previous['source']), str(actor)[:256], previous['id'])).fetchone()
            else:
                row = conn.execute('''INSERT INTO kumo_task_engine.dependency_graphs(id,key,name,enabled,definition,definition_hash,source,created_by,updated_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                    (uuid.uuid4(), definition['key'], definition['name'], definition['enabled'], Jsonb(definition), fingerprint, Jsonb(source or {}), str(actor)[:256], str(actor)[:256])).fetchone()
                if definition['key']=='wf1-sql-trial':
                    row=conn.execute('UPDATE kumo_task_engine.dependency_graphs SET configuration_managed=true WHERE id=%s RETURNING *',(row['id'],)).fetchone()
            record_revision(conn,row)
        return dict(ok=True, graph=row, reused=False)

    def start(self, graph_id, body, actor):
        graph_id = uuid.UUID(str(graph_id))
        if not isinstance(body, dict) or set(body)-{'requestId','workflowId','includeChildren'} or not isinstance(body.get('requestId'), str) or not 1 <= len(body['requestId'].strip()) <= 128:
            raise ValueError('Start a workflow using a stable requestId')
        if 'workflowId' in body and (not isinstance(body['workflowId'],str) or len(body['workflowId'])>128):
            raise ValueError('Select a workflow using its catalog identity')
        if type(body.get('includeChildren',True)) is not bool:raise ValueError('includeChildren must be boolean')
        try:
            with self.store.transaction() as conn:
                return self.start_in_transaction(conn,graph_id,body,actor)
        except (Conflict, LookupError, ValueError) as exc:
            # A rejection rolls back admission. Keep its explanation in a fresh
            # transaction, without retaining any rolled-back acceptance records.
            with self.store.transaction() as conn:
                record_decision(conn,'admission','REQUEST_REJECTED',graph_id=graph_id,
                    request_id=body['requestId'].strip(),actor=actor,
                    workflowId=body.get('workflowId'),includeChildren=body.get('includeChildren',True),
                    reason=str(exc),errorType=type(exc).__name__)
            raise

    def start_in_transaction(self,conn,graph_id,body,actor,*,trigger='MANUAL'):
        from psycopg.types.json import Jsonb
        graph_id=uuid.UUID(str(graph_id))
        request_id=body['requestId'].strip()
        previous = conn.execute('SELECT * FROM kumo_task_engine.cascades WHERE request_id=%s', (request_id,)).fetchone()
        if previous:
            if previous['snapshot'].get('retryOf'):
                raise Conflict('requestId belongs to a workflow retry')
            if previous['graph_id'] != graph_id:
                raise Conflict('requestId belongs to another graph')
            if previous['snapshot'].get('requestedWorkflowId')!=body.get('workflowId'):
                raise Conflict('requestId belongs to another workflow selection')
            if previous['snapshot'].get('includeChildren',True)!=body.get('includeChildren',True):
                raise Conflict('requestId belongs to another execution scope')
            record_decision(conn,'admission','REQUEST_REUSED',graph_id=graph_id,
                graph_revision=previous['graph_revision'],cascade_id=previous['id'],
                request_id=request_id,actor=actor,reason='IDEMPOTENT_REQUEST')
            return dict(ok=True, cascadeId=str(previous['id']), reused=True)
        graph = conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE id=%s', (graph_id,)).fetchone()
        if not graph:
            raise LookupError('Dependency trial graph not found')
        if not graph['enabled']:
            raise Conflict('Dependency trial graph is disabled')
        snapshot=graph['definition'];scope='FULL'
        if 'catalog' in snapshot:
            from .admission import execution_scope,outstanding,busy_message
            selected=execution_scope(snapshot,body.get('workflowId'),body.get('includeChildren',True))
            if selected['issues']:raise Conflict('; '.join(selected['issues']))
            requested=next(w for w in snapshot['catalog']['workflows'] if w['WORKFLOW_ID']==body['workflowId'])
            if not requested['IS_ENABLED'] or (trigger=='MANUAL' and not requested.get('ALLOW_MANUAL_TRIGGER',True)):raise Conflict('Workflow is disabled or manual execution is not allowed')
            keys=selected['keys'];scope='manual:'+request_id
            related=outstanding(conn,graph_id,selected['relatedKeys'])
            if trigger=='MANUAL' and related:raise WorkflowBusy(busy_message(related,snapshot,snapshot['workflowKeys'][body['workflowId']]))
            if trigger=='SCHEDULED':
                requested_key=snapshot['workflowKeys'][body['workflowId']]
                for pending in related:
                    if pending['node_key']!=requested_key or not (pending['state']=='WAITING' or pending['actual_state']=='QUEUED'):
                        continue
                    existing=pending['snapshot']
                    if pending['cancel_requested']:
                        record_decision(conn,'admission','PENDING_CANCELLATION_EXCLUDED',
                            graph_id=graph_id,graph_revision=graph['revision'],cascade_id=pending['cascade_id'],
                            node_key=requested_key,request_id=request_id,actor=actor,trigger=trigger,
                            reason='CANCELLATION_REQUESTED',requestedKeys=keys,
                            pendingState=pending['actual_state'] or pending['state'])
                        continue
                    missing=sorted(set(keys)-{n['key'] for n in existing['nodes']})
                    same_revision=existing.get('catalogRevision')==graph['revision']
                    compatible=not missing and same_revision
                    record_decision(conn,'admission','REQUEST_REUSED' if compatible else 'PENDING_SCOPE_INCOMPATIBLE',
                        graph_id=graph_id,graph_revision=graph['revision'],cascade_id=pending['cascade_id'],
                        node_key=requested_key,request_id=request_id,actor=actor,trigger=trigger,
                        reason='COMPATIBLE_PENDING_SCOPE' if compatible else 'PENDING_SCOPE_DOES_NOT_SATISFY_REQUEST',
                        requestedKeys=keys,missingKeys=missing,pendingRevision=existing.get('catalogRevision'))
                    if compatible:return dict(ok=True,cascadeId=str(pending['cascade_id']),reused=True)
                # A running instance and one queued occurrence may coexist.
                scope='scheduled:'+request_id
            external={n['key']:[p for p in n['parents'] if p['key'] not in keys]
                for n in snapshot['nodes'] if n['key'] in keys}
            if trigger=='MANUAL':external[snapshot['workflowKeys'][body['workflowId']]]=[]
            bindings={}
            for parent in {p['key'] for rules in external.values() for p in rules}:
                row=conn.execute('''SELECT n.id FROM kumo_task_engine.cascade_nodes n JOIN kumo_task_engine.cascades c ON c.id=n.cascade_id
                    WHERE c.graph_id=%s AND n.node_key=%s ORDER BY c.created_at DESC,n.id DESC LIMIT 1''',(graph_id,parent)).fetchone()
                bindings[parent]=str(row['id']) if row else None
            nodes=[dict(n,parents=[p for p in n['parents'] if p['key'] in keys]) for n in snapshot['nodes'] if n['key'] in keys]
            snapshot=dict(key=snapshot['key'],name=snapshot['name'],enabled=True,version=1,
                nodes=nodes,catalogRevision=graph['revision'],guards=selected['guards'],relationships=selected['relationships'],externalParents=external,externalBindings=bindings,trigger=trigger,includeChildren=body.get('includeChildren',True),
                workflowNames={snapshot['workflowKeys'][w['WORKFLOW_ID']]:w['WORKFLOW_NAME'] for w in snapshot['catalog']['workflows']},
                workflowIds={key:identity for identity,key in snapshot['workflowKeys'].items() if key in keys},
                disabledWorkflowKeys=selected['disabledKeys'],requestedWorkflowId=body['workflowId'])
        elif body.get('workflowId') is not None:
            raise ValueError('Select a workflow through the complete PostgreSQL catalog')
        elif 'includeChildren' in body:
            raise ValueError('Execution scope selection requires a catalog workflow')
        elif graph['key']=='wf1-sql-trial' and conn.execute("SELECT id FROM kumo_task_engine.dependency_graphs WHERE key='dev-workflow-catalog'").fetchone():
            raise Conflict('Use the PostgreSQL Monitor; the WF1 trial has been promoted to the complete catalog')
        keys=[n['key'] for n in snapshot['nodes']]
        if 'guards' not in snapshot and conn.execute("""SELECT c.id FROM kumo_task_engine.cascades c JOIN kumo_task_engine.cascade_nodes n ON n.cascade_id=c.id
            WHERE c.graph_id=%s AND c.state IN ('QUEUED','RUNNING','UNKNOWN') AND n.node_key=ANY(%s) LIMIT 1""",(graph_id,keys)).fetchone():
            raise WorkflowBusy('This workflow group already has an outstanding execution; finish or review it first')
        cascade_id = uuid.uuid4()
        conn.execute('''INSERT INTO kumo_task_engine.cascades(id,graph_id,request_id,graph_revision,snapshot,requested_by,state,scope_key)
            VALUES(%s,%s,%s,%s,%s,%s,'QUEUED',%s)''', (cascade_id, graph_id, request_id, graph['revision'], Jsonb(snapshot), str(actor)[:256],scope))
        for node in snapshot['nodes']:
            conn.execute("INSERT INTO kumo_task_engine.cascade_nodes(cascade_id,node_key,workflow_key,state) VALUES(%s,%s,%s,'WAITING')",
                         (cascade_id, node['key'], 'dep-'+graph_id.hex+'-'+node['key']))
        enqueue(conn, cascade_id)
        record_decision(conn,'admission','REQUEST_ACCEPTED',graph_id=graph_id,
            graph_revision=graph['revision'],cascade_id=cascade_id,request_id=request_id,actor=actor,
            trigger=trigger,workflowId=body.get('workflowId'),includeChildren=body.get('includeChildren',True),
            selectedKeys=keys,guards=snapshot.get('guards',{}),
            externalParents=snapshot.get('externalParents',{}),externalBindings=snapshot.get('externalBindings',{}),
            ignoredDisabledWorkflowKeys=snapshot.get('disabledWorkflowKeys',[]),
            manualEntryParentsBypassed=trigger=='MANUAL' and 'guards' in snapshot)
        return dict(ok=True, cascadeId=str(cascade_id), reused=False)

    def read(self, cascade_id):
        cascade_id = uuid.UUID(str(cascade_id))
        with self.store.connect() as conn:
            cascade = conn.execute('SELECT * FROM kumo_task_engine.cascades WHERE id=%s', (cascade_id,)).fetchone()
            if not cascade:
                raise LookupError('Dependency trial execution not found')
            nodes = conn.execute('''SELECT n.*,r.state AS run_state,r.started_at,r.ended_at,
                (SELECT count(*) FROM kumo_task_engine.attempts a WHERE a.run_id=n.run_id AND a.cleanup_state='PENDING') AS cleanup_pending
                FROM kumo_task_engine.cascade_nodes n LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id WHERE n.cascade_id=%s ORDER BY n.node_key''', (cascade_id,)).fetchall()
            work = conn.execute('SELECT state,attempts,error FROM kumo_task_engine.dependency_work WHERE cascade_id=%s', (cascade_id,)).fetchone()
        return dict(ok=True, cascade=cascade, nodes=nodes, dispatch=work)

    def status(self):
        with self.store.connect() as conn:
            graphs = conn.execute('''SELECT g.*,(SELECT jsonb_build_object('id',c.id,'state',c.state) FROM kumo_task_engine.cascades c
                WHERE c.graph_id=g.id AND c.state IN ('QUEUED','RUNNING','UNKNOWN') LIMIT 1) AS active_cascade
                FROM kumo_task_engine.dependency_graphs g ORDER BY g.name''').fetchall()
            cascades = conn.execute('SELECT id,graph_id,graph_revision,request_id,state,created_at,ended_at FROM kumo_task_engine.cascades ORDER BY created_at DESC LIMIT 30').fetchall()
            workers = conn.execute('SELECT *,EXTRACT(EPOCH FROM clock_timestamp()-last_seen) AS age FROM kumo_task_engine.dependency_workers ORDER BY last_seen DESC').fetchall()
        live = [w for w in workers if w['age'] <= 10 and w['execution_enabled']]
        health = 'HEALTHY' if live else 'EXECUTION_DISABLED' if any(w['age']<=10 for w in workers) else 'DELAYED' if workers and workers[0]['age']<=30 else 'UNAVAILABLE' if workers else 'NOT_STARTED'
        return dict(ok=True, graphs=graphs, cascades=cascades, dispatcher=dict(state=health, liveWorkers=len(live), lastSeen=workers[0]['last_seen'] if workers else None))

    def cancel(self, cascade_id, actor=None):
        cascade_id = uuid.UUID(str(cascade_id))
        with self.store.transaction() as conn:
            row = conn.execute('SELECT * FROM kumo_task_engine.cascades WHERE id=%s', (cascade_id,)).fetchone()
            if not row:
                raise LookupError('Dependency trial execution not found')
            if row['state'] in TERMINAL:
                record_decision(conn,'dependency','CANCEL_IGNORED',graph_id=row['graph_id'],
                    cascade_id=cascade_id,actor=actor,state=row['state'],reason='GROUP_ALREADY_TERMINAL')
                return dict(ok=True, cascadeId=str(cascade_id))
            conn.execute('UPDATE kumo_task_engine.cascades SET cancel_requested=true WHERE id=%s', (cascade_id,))
            for run in conn.execute('''SELECT n.run_id FROM kumo_task_engine.cascade_nodes n JOIN kumo_task_engine.runs r ON r.id=n.run_id
                WHERE n.cascade_id=%s AND r.state IN ('QUEUED','RUNNING','UNKNOWN')''', (cascade_id,)).fetchall():
                self.store.cancel_in_transaction(conn, run['run_id'])
            enqueue(conn, cascade_id)
            record_decision(conn,'dependency','CANCEL_REQUESTED',graph_id=row['graph_id'],
                graph_revision=row['graph_revision'],cascade_id=cascade_id,actor=actor,state=row['state'])
        return dict(ok=True, cascadeId=str(cascade_id))

    def retry_dispatch(self, cascade_id, actor=None):
        cascade_id = uuid.UUID(str(cascade_id))
        with self.store.transaction() as conn:
            if not conn.execute('SELECT id FROM kumo_task_engine.cascades WHERE id=%s', (cascade_id,)).fetchone():
                raise LookupError('Dependency trial execution not found')
            enqueue(conn, cascade_id)
            record_decision(conn,'dependency','DISPATCH_RETRY_REQUESTED',cascade_id=cascade_id,actor=actor)
        return dict(ok=True, cascadeId=str(cascade_id))

    def retry(self, cascade_id, node_id, actor):
        from .retries import retry
        cascade_id,node_id=uuid.UUID(str(cascade_id)),uuid.UUID(str(node_id))
        try:
            return retry(self.store,cascade_id,node_id,actor)
        except (Conflict,LookupError,ValueError) as exc:
            with self.store.transaction() as conn:
                record_decision(conn,'dependency-retry','WORKFLOW_RETRY_REJECTED',
                    cascade_id=cascade_id,actor=actor,sourceNodeId=node_id,reason=str(exc))
            raise


class DependencyWorker:
    def __init__(self, store, owner=None):
        self.store = store
        self.owner = owner or uuid.uuid4()

    def step(self, execution_enabled=True):
        cascade_id = None
        evaluation_id = uuid.uuid4()
        try:
            with self.store.transaction() as conn:
                conn.execute('''INSERT INTO kumo_task_engine.dependency_workers(id,execution_enabled) VALUES(%s,%s)
                    ON CONFLICT(id) DO UPDATE SET execution_enabled=excluded.execution_enabled,last_seen=clock_timestamp()''', (self.owner, execution_enabled))
                if not execution_enabled:
                    return False
                work = conn.execute("SELECT * FROM kumo_task_engine.dependency_work WHERE state='PENDING' AND next_check_at<=clock_timestamp() ORDER BY next_check_at LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
                if not work:
                    return False
                cascade_id = work['cascade_id']
                self.evaluate(conn, cascade_id, evaluation_id=evaluation_id)
                conn.execute("UPDATE kumo_task_engine.dependency_work SET state='DONE',next_check_at=NULL,error=NULL,attempts=0 WHERE cascade_id=%s", (cascade_id,))
            return True
        except Exception as exc:
            if cascade_id is None:
                raise
            # Outcome persistence has already committed independently. Rollback
            # leaves this event pending, and this separate transaction bounds retry.
            with self.store.transaction() as conn:
                row = conn.execute('SELECT attempts FROM kumo_task_engine.dependency_work WHERE cascade_id=%s', (cascade_id,)).fetchone()
                count = row['attempts']+1
                conn.execute("UPDATE kumo_task_engine.dependency_work SET state=%s,attempts=%s,error=%s,next_check_at=CASE WHEN %s THEN NULL ELSE clock_timestamp()+(%s*interval '1 second') END WHERE cascade_id=%s",
                             ('ERROR' if count>=6 else 'PENDING', count, 'Dependency evaluation unavailable: '+type(exc).__name__, count>=6, min(60,2**count), cascade_id))
                record_decision(conn,'dependency','DISPATCH_ERROR' if count>=6 else 'DISPATCH_RETRY_SCHEDULED',
                    cascade_id=cascade_id,evaluation_id=evaluation_id,actor=self.owner,
                    attempt=count,errorType=type(exc).__name__,evaluationRolledBack=True,
                    retrySeconds=None if count>=6 else min(60,2**count))
            return True

    def evaluate(self, conn, cascade_id, evaluation_id=None):
        from psycopg.types.json import Jsonb
        cascade = conn.execute('SELECT * FROM kumo_task_engine.cascades WHERE id=%s', (cascade_id,)).fetchone()
        current_graph=conn.execute('SELECT definition FROM kumo_task_engine.dependency_graphs WHERE id=%s',(cascade['graph_id'],)).fetchone()['definition']
        rows = conn.execute('''SELECT n.*,r.state AS actual_state,r.started_at,r.ended_at FROM kumo_task_engine.cascade_nodes n
            LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id WHERE n.cascade_id=%s''', (cascade_id,)).fetchall()
        nodes = {row['node_key']:row for row in rows}
        prior_states = {key:node['state'] for key,node in nodes.items()}
        evaluation_id = evaluation_id or uuid.uuid4()
        def decide(decision, *, node_key=None, run_id=None, **facts):
            record_decision(conn,'dependency',decision,graph_id=cascade['graph_id'],
                graph_revision=cascade['graph_revision'],cascade_id=cascade_id,
                node_key=node_key,run_id=run_id,evaluation_id=evaluation_id,
                request_id=cascade['request_id'],actor=self.owner,**facts)
        decide('EVALUATION_STARTED',previousState=cascade['state'],cancelRequested=cascade['cancel_requested'])
        for row in rows:
            if row['run_id']:
                row['state'] = row['actual_state'] if row['actual_state'] in TERMINAL|{'UNKNOWN'} else 'ADMITTED'
                conn.execute('UPDATE kumo_task_engine.cascade_nodes SET state=%s WHERE cascade_id=%s AND node_key=%s', (row['state'], cascade_id, row['node_key']))
        for definition in cascade['snapshot']['nodes']:
            node = nodes[definition['key']]
            if node['state'] != 'WAITING':
                decide('NODE_OUTCOME_OBSERVED' if node['state']!=prior_states[definition['key']] else 'NODE_RETAINED',
                    node_key=definition['key'],run_id=node['run_id'],workflowName=definition['name'],
                    previousState=prior_states[definition['key']],state=node['state'],actualState=node['actual_state'],
                    reason='EXISTING_NATIVE_RUN' if node['run_id'] else 'NODE_ALREADY_RESOLVED')
                continue
            state, blockers, parents = 'WAITING', [], {}
            parent_checks, related_checks, guards = [], [], []
            if cascade['cancel_requested']:
                state='CANCELLED';blockers=[dict(reason='Trial cancelled before admission')]
            elif not definition['enabled'] or not definition['allow'] or not execution_plan(definition['plan'])['jobs']:
                state='SKIPPED';blockers=[dict(reason='Workflow is disabled, disallowed or has no enabled jobs in this snapshot')]
            else:
                cutoff = conn.execute('SELECT MAX(started_at) AS cutoff FROM kumo_task_engine.runs WHERE workflow_key=%s', (node['workflow_key'],)).fetchone()['cutoff']
                guards=cascade['snapshot'].get('guards',{}).get(definition['key'])
                identity=cascade['snapshot'].get('workflowIds',{}).get(definition['key'])
                if guards and 'catalog' in current_graph and identity in current_graph['workflowKeys']:
                    from .admission import execution_scope
                    current_scope=execution_scope(current_graph,identity,False)
                    guards=sorted(set(guards)|set(current_scope['relatedKeys']))
                if guards:
                    from .admission import outstanding,relationship
                    for other in outstanding(conn,cascade['graph_id'],guards,exclude=cascade_id):
                        # Older adjacent queued work keeps its reservation;
                        # unrelated levels do not block this workflow.
                        blocks=bool(other['run_id'] or other['state']=='UNKNOWN' or (other['created_at'],str(other['cascade_id']))<(cascade['created_at'],str(cascade_id)))
                        relation=relationship(cascade['snapshot'],definition['key'],other['node_key'])
                        if relation=='RELATED':relation=relationship(current_graph,definition['key'],other['node_key'])
                        related_checks.append(dict(cascadeId=other['cascade_id'],nodeKey=other['node_key'],
                            runId=other['run_id'],state=other['actual_state'] or other['state'],
                            relationship=relation,
                            decision='WAIT_FOR_RELATED_EXECUTION' if blocks else 'NEWER_RESERVATION_YIELDS'))
                        if blocks:
                            blockers.append(dict(parent=other['name'],nodeKey=other['node_key'],relationship=related_checks[-1]['relationship'],
                                cascadeId=str(other['cascade_id']),runId=str(other['run_id']) if other['run_id'] else None,
                                state=other['actual_state'] or other['state'],reason='Waiting for related execution to complete'))
                for rule in cascade['snapshot'].get('externalParents',{}).get(definition['key'],[]):
                    binding=cascade['snapshot']['externalBindings'].get(rule['key'])
                    parent=conn.execute('''SELECT n.*,r.state AS actual_state,r.ended_at FROM kumo_task_engine.cascade_nodes n
                        LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id WHERE n.id=%s''',(binding,)).fetchone() if binding else None
                    actual=(parent['actual_state'] or parent['state']) if parent else None
                    check=dict(parent=rule['key'],external=True,binding=binding,rule=rule,
                        runId=parent['run_id'] if parent else None,state=actual,
                        endedAt=parent['ended_at'] if parent else None,childLastStartedAt=cutoff,decision='ACCEPTED')
                    parent_checks.append(check)
                    if parent and parent['run_id']:parents[rule['key']]=str(parent['run_id'])
                    if parent and actual not in NODE_DONE:
                        check['decision']='WAIT_FOR_PARENT'
                        blockers.append(dict(parent=rule['key'],relationship='PARENT',state=actual,reason='Parent outcome needs review' if actual=='UNKNOWN' else 'Waiting for parent completion'))
                    elif not parent or not accepts(actual,rule):
                        check['decision']='MISSING_PARENT' if not parent else 'OUTCOME_REJECTED'
                        state='BLOCKED';blockers.append(dict(parent=rule['key'],relationship='PARENT',state=actual,reason='No parent execution was available when this run was requested' if not parent else 'Parent outcome or ALLOW rule does not permit execution'))
                    elif rule['parentMustBeNewer'] and cutoff and (parent['ended_at'] is None or parent['ended_at']<=cutoff):
                        check['decision']='STALE_PARENT'
                        state='BLOCKED';blockers.append(dict(parent=rule['key'],relationship='PARENT',state='STALE_PARENT',reason='Parent completed before the child last started'))
                for rule in definition['parents']:
                    parent = nodes[rule['key']]
                    if parent['run_id']:
                        parents[rule['key']]=str(parent['run_id'])
                    actual = parent['actual_state'] or parent['state']
                    check=dict(parent=rule['key'],external=False,rule=rule,runId=parent['run_id'],
                        state=actual,endedAt=parent['ended_at'],childLastStartedAt=cutoff,decision='ACCEPTED')
                    parent_checks.append(check)
                    if parent['state'] in ('WAITING','ADMITTED','UNKNOWN'):
                        check['decision']='PARENT_NEEDS_REVIEW' if parent['state']=='UNKNOWN' else 'WAIT_FOR_PARENT'
                        blockers.append(dict(parent=rule['key'],relationship='PARENT',state=actual,reason='Parent outcome needs review' if parent['state']=='UNKNOWN' else 'Waiting for parent completion'))
                    elif not accepts(parent['state'],rule):
                        check['decision']='OUTCOME_REJECTED'
                        state='BLOCKED';blockers.append(dict(parent=rule['key'],relationship='PARENT',state=actual,reason='Parent outcome or ALLOW rule does not permit execution'))
                    elif rule['parentMustBeNewer'] and cutoff and (parent['ended_at'] is None or parent['ended_at']<=cutoff):
                        check['decision']='STALE_PARENT'
                        state='BLOCKED';blockers.append(dict(parent=rule['key'],relationship='PARENT',state='STALE_PARENT',reason='Parent completed before the child last started'))
                if not blockers:
                    plan = execution_plan(definition['plan'])
                    run = self.store.submit_in_transaction(conn, dict(plan,workflowKey=node['workflow_key'],requestId='dep:'+str(cascade_id)+':'+definition['key']),
                                                           cascade['requested_by'], dependency_admission=True)
                    node['run_id']=uuid.UUID(run['runId']);state='ADMITTED'
                    self.store.event(conn,node['run_id'],'DEPENDENCY_ADMITTED',cascadeId=str(cascade_id),node=definition['key'],parentRuns=parents,graphRevision=cascade['graph_revision'])
            node['state']=state
            conn.execute('UPDATE kumo_task_engine.cascade_nodes SET state=%s,run_id=%s,blockers=%s,parent_runs=%s WHERE cascade_id=%s AND node_key=%s',
                         (state,node['run_id'],Jsonb(blockers),Jsonb(parents),cascade_id,definition['key']))
            decide('NODE_'+state,node_key=definition['key'],run_id=node['run_id'],
                workflowName=definition['name'],previousState=prior_states[definition['key']],state=state,
                enabled=definition['enabled'],allow=definition['allow'],
                selectedJobKeys=[j['key'] for j in definition['plan']['jobs'] if j.get('enabled',True)],
                guards=guards,blockers=blockers,parentRuns=parents,parentChecks=parent_checks,relatedChecks=related_checks)
        states = [n['state'] for n in nodes.values()]
        complete = all(s in NODE_DONE for s in states)
        if complete:
            state='FAILED' if any(s in ('FAILED','BLOCKED') for s in states) else 'CANCELLED' if cascade['cancel_requested'] or 'CANCELLED' in states else 'SKIPPED' if all(s=='SKIPPED' for s in states) else 'SUCCESS'
        else:
            state='UNKNOWN' if 'UNKNOWN' in states else 'RUNNING' if 'ADMITTED' in states else 'QUEUED'
        conn.execute('UPDATE kumo_task_engine.cascades SET state=%s,ended_at=CASE WHEN %s THEN COALESCE(ended_at,clock_timestamp()) ELSE NULL END WHERE id=%s', (state,complete,cascade_id))
        decide('GROUP_EVALUATED',previousState=cascade['state'],state=state,complete=complete,
            nodeStates={key:node['state'] for key,node in nodes.items()})
        if complete and cascade['state'] not in TERMINAL:
            # Cancellation/blocked admission can release reservations without
            # producing any native run outcome. Wake other groups atomically.
            awakened=conn.execute('''INSERT INTO kumo_task_engine.dependency_work(cascade_id)
                SELECT id FROM kumo_task_engine.cascades WHERE graph_id=%s AND id<>%s AND state IN ('QUEUED','RUNNING','UNKNOWN')
                ON CONFLICT(cascade_id) DO UPDATE SET state='PENDING',next_check_at=clock_timestamp(),attempts=0,error=NULL
                RETURNING cascade_id''', (cascade['graph_id'],cascade_id)).fetchall()
            for other in awakened:
                record_decision(conn,'dependency','EVALUATION_ENQUEUED',graph_id=cascade['graph_id'],
                    cascade_id=other['cascade_id'],evaluation_id=evaluation_id,actor=self.owner,
                    reason='RELATED_GROUP_COMPLETED',sourceCascadeId=cascade_id,sourceState=state)
