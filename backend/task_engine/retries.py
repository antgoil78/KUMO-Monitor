"""Retry a failed workflow and resume only its unexecuted blocked descendants."""
from copy import deepcopy
import uuid

from psycopg.types.json import Jsonb

from .admission import execution_scope, outstanding, busy_message
from .decisions import record
from .dependencies import enqueue
from .store import Conflict, WorkflowBusy


def retry(store, cascade_id, node_id, actor):
    cascade_id, node_id = uuid.UUID(str(cascade_id)), uuid.UUID(str(node_id))
    request_id = 'retry:' + str(node_id)
    with store.transaction() as conn:
        previous = conn.execute('SELECT * FROM kumo_task_engine.cascades WHERE request_id=%s', (request_id,)).fetchone()
        if previous:
            if previous['snapshot'].get('retryOf') != dict(cascadeId=str(cascade_id), nodeId=str(node_id)):
                raise Conflict('Retry identity belongs to another execution')
            record(conn, 'dependency-retry', 'WORKFLOW_RETRY_REUSED', cascade_id=previous['id'], request_id=request_id, actor=actor)
            return dict(ok=True, cascadeId=str(previous['id']), reused=True)
        source = conn.execute('''SELECT n.*,r.state AS actual_state,c.graph_id,c.cancel_requested
            FROM kumo_task_engine.cascade_nodes n JOIN kumo_task_engine.cascades c ON c.id=n.cascade_id
            LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id WHERE n.id=%s AND c.id=%s''', (node_id, cascade_id)).fetchone()
        if not source:
            raise LookupError('Workflow execution not found in this group')
        if source['actual_state'] != 'FAILED' or source['cancel_requested']:
            raise Conflict('Only a confirmed failed workflow in a non-cancelled group can be retried')
        graph = conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE id=%s', (source['graph_id'],)).fetchone()
        current = graph['definition']
        if not graph['enabled'] or 'catalog' not in current:
            raise Conflict('Retry requires the enabled PostgreSQL workflow catalog')
        identity = next((i for i,k in current['workflowKeys'].items() if k==source['node_key']), None)
        if identity is None:
            raise Conflict('This workflow is no longer in the current configuration')
        scope = execution_scope(current, identity, False)
        workflow = next(w for w in current['catalog']['workflows'] if w['WORKFLOW_ID']==identity)
        if scope['issues'] or not workflow['IS_ENABLED'] or not workflow.get('ALLOW_MANUAL_TRIGGER', True):
            raise Conflict('; '.join(scope['issues']) or 'Workflow is disabled or manual execution is not allowed')
        # Only the latest accepted generation of each workflow may be resumed.
        # Older blocked history must not be revived after newer work supersedes it.
        latest = conn.execute('''SELECT DISTINCT ON (n.node_key) n.*,c.snapshot,c.graph_revision,c.cancel_requested
            FROM kumo_task_engine.cascade_nodes n JOIN kumo_task_engine.cascades c ON c.id=n.cascade_id
            WHERE c.graph_id=%s ORDER BY n.node_key,c.created_at DESC,n.id DESC''', (graph['id'],)).fetchall()
        by_key = {n['node_key']:n for n in latest}
        if by_key[source['node_key']]['id'] != node_id:
            raise Conflict('A newer execution already exists; refresh the Monitor')
        related = outstanding(conn, graph['id'], scope['relatedKeys'])
        if related:
            raise WorkflowBusy(busy_message(related, current, source['node_key']))
        group_ids = list({n['cascade_id'] for n in latest})
        group_nodes = conn.execute('SELECT id,cascade_id,node_key FROM kumo_task_engine.cascade_nodes WHERE cascade_id=ANY(%s)', (group_ids,)).fetchall()
        siblings = {(n['cascade_id'],n['node_key']):str(n['id']) for n in group_nodes}

        def definition(node):
            return next(d for d in node['snapshot']['nodes'] if d['key']==node['node_key'])

        def parents(node):
            snapshot = node['snapshot']
            result = [(rule, siblings[(node['cascade_id'],rule['key'])]) for rule in definition(node)['parents']]
            result.extend((rule, snapshot.get('externalBindings',{}).get(rule['key']))
                for rule in snapshot.get('externalParents',{}).get(node['node_key'],[]))
            return result

        selected = {str(node_id):by_key[source['node_key']]}
        changed = True
        while changed:
            changed = False
            for node in latest:
                if str(node['id']) in selected or node['state']!='BLOCKED' or node['run_id'] or node['cancel_requested']:
                    continue
                if any(binding in selected for _,binding in parents(node)):
                    selected[str(node['id'])] = node
                    changed = True
        # Build a new frozen recovery group. Original failures, blocked records
        # and successful native runs remain unchanged and visible in History.
        definitions, external, bindings, guards, relationships, resumed = {}, {}, {}, {}, {}, {}
        identities = {k:i for i,k in current['workflowKeys'].items()}
        for source_id,node in selected.items():
            key = node['node_key']
            if key not in identities:
                raise Conflict('A blocked workflow was removed from configuration; start a new run')
            item = deepcopy(next(d for d in current['nodes'] if d['key']==key) if source_id==str(node_id) else definition(node))
            item['parents'] = []
            external[key] = []
            if source_id != str(node_id):
                for rule,binding in parents(node):
                    if binding in selected:
                        item['parents'].append(deepcopy(rule))
                    else:
                        external[key].append(deepcopy(rule))
                        if rule['key'] in bindings and bindings[rule['key']] != binding:
                            raise Conflict('Blocked branches use different parent generations; retry them separately')
                        bindings[rule['key']] = binding
            definitions[key] = item
            frozen = current if source_id==str(node_id) else node['snapshot']
            current_scope = execution_scope(current,identities[key],False)
            guards[key] = frozen.get('guards',{}).get(key) or current_scope['relatedKeys']
            relationships[key] = frozen.get('relationships',{}).get(key) or current_scope['relationships'][key]
            resumed[key] = dict(cascadeId=str(node['cascade_id']), nodeId=source_id, graphRevision=node['graph_revision'])
        ordered, visiting = [], set()
        def visit(key):
            if key in visiting:
                raise Conflict('Recovery combines incompatible dependency snapshots; start a new run')
            if key in {d['key'] for d in ordered}:
                return
            visiting.add(key)
            for rule in definitions[key]['parents']:
                visit(rule['key'])
            visiting.remove(key)
            ordered.append(definitions[key])
        for key in definitions:
            visit(key)
        names = {current['workflowKeys'][w['WORKFLOW_ID']]:w['WORKFLOW_NAME'] for w in current['catalog']['workflows']}
        names.update({d['key']:d['name'] for d in ordered})
        snapshot = dict(key=current['key'],name=current['name'],enabled=True,version=1,nodes=ordered,
            catalogRevision=graph['revision'],guards=guards,relationships=relationships,
            externalParents=external,externalBindings=bindings,trigger='MANUAL',includeChildren=True,
            workflowNames=names,workflowIds={k:i for i,k in current['workflowKeys'].items() if k in definitions},
            requestedWorkflowId=identity,retryOf=dict(cascadeId=str(cascade_id),nodeId=str(node_id)),resumedFrom=resumed)
        recovery_id = uuid.uuid4()
        conn.execute('''INSERT INTO kumo_task_engine.cascades(id,graph_id,request_id,graph_revision,snapshot,requested_by,state,scope_key)
            VALUES(%s,%s,%s,%s,%s,%s,'QUEUED',%s)''',
            (recovery_id,graph['id'],request_id,graph['revision'],Jsonb(snapshot),str(actor)[:256],'manual:'+request_id))
        for item in ordered:
            key = item['key']
            waiting = [] if key==source['node_key'] else [dict(parent=p['key'],relationship='PARENT',state='WAITING',reason='Waiting for parent completion') for p in item['parents']]
            conn.execute('''INSERT INTO kumo_task_engine.cascade_nodes(cascade_id,node_key,workflow_key,state,blockers)
                VALUES(%s,%s,%s,'WAITING',%s)''', (recovery_id,key,'dep-'+graph['id'].hex+'-'+key,Jsonb(waiting)))
            record(conn,'dependency-retry','WORKFLOW_RETRY_REQUESTED' if key==source['node_key'] else 'BLOCKED_WORKFLOW_RESUMED',
                graph_id=graph['id'],graph_revision=graph['revision'],cascade_id=recovery_id,node_key=key,
                request_id=request_id,actor=actor,source=resumed[key],workflowName=item['name'])
        enqueue(conn,recovery_id)
        store.event(conn,source['run_id'],'WORKFLOW_RETRY_REQUESTED',cascadeId=str(recovery_id),actor=str(actor)[:256])
        return dict(ok=True,cascadeId=str(recovery_id),reused=False,resumedWorkflows=[d['name'] for d in ordered])
