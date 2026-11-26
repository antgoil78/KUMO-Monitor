"""Catalog execution scopes and overlap checks, using PostgreSQL only."""
from .store import TERMINAL


def workflow_rules(catalog):
    """Bypass disabled parents, retaining the nearest enabled ancestor's rules."""
    workflows={w['WORKFLOW_ID']:w for w in catalog['workflows']}
    headers={i:[] for i in workflows}
    for rule in catalog['raw_records']:
        if not rule.get('JOB_ID') and rule['WORKFLOW_ID'] in headers:
            headers[rule['WORKFLOW_ID']].append(rule)

    def expand(rule, path):
        parent=rule.get('PARENT_ID')
        if parent not in workflows or workflows[parent]['IS_ENABLED']:
            return [dict(rule)]
        if parent in path:
            raise ValueError('Disabled workflow dependencies contain a cycle')
        return [edge for upstream in headers[parent] if upstream.get('PARENT_ID')
                for edge in expand(upstream,path|{parent})]

    result={}
    for identity,workflow in workflows.items():
        if not workflow['IS_ENABLED']:
            result[identity]=headers[identity]
            continue
        rules={}
        for source in headers[identity]:
            for rule in expand(source,set()):
                parent=rule.get('PARENT_ID')
                rule.update(WORKFLOW_ID=identity,JOB_ID=None)
                if parent in rules:
                    # A shared ancestor must satisfy every enabled path to this join.
                    old=rules[parent]
                    old['ALLOW']=old.get('ALLOW') is not False and rule.get('ALLOW') is not False
                    old['PARENT_MUST_BE_NEWER']=bool(old.get('PARENT_MUST_BE_NEWER') or rule.get('PARENT_MUST_BE_NEWER'))
                    for field in ('SUCCESS','WARNING','ERROR'):
                        old[field]=False if old.get(field) is False or rule.get(field) is False else old.get(field) if old.get(field) is not None else rule.get(field)
                    old['SQL']=old.get('SQL') or rule.get('SQL')
                else:
                    rules[parent]=rule
        result[identity]=list(rules.values())
    return result


def execution_scope(definition, identity, include_children=True):
    workflows = {w['WORKFLOW_ID']: w for w in definition['catalog']['workflows']}
    if identity not in workflows:
        raise LookupError('Workflow is not in the PostgreSQL catalog')
    parents = {i: set() for i in workflows}
    children = {i: set() for i in workflows}
    original_parents={i:set() for i in workflows}
    original_children={i:set() for i in workflows}
    for rule in definition['catalog']['raw_records']:
        child, parent = rule.get('WORKFLOW_ID'), rule.get('PARENT_ID')
        if not rule.get('JOB_ID') and child in workflows and parent in workflows:
            original_parents[child].add(parent)
            original_children[parent].add(child)
    for child,rules in workflow_rules(definition['catalog']).items():
        if not workflows[child]['IS_ENABLED']:continue
        for rule in rules:
            parent=rule.get('PARENT_ID')
            if parent in workflows:
                parents[child].add(parent)
                children[parent].add(child)

    def reachable(start, edges):
        found, pending = {start}, [start]
        while pending:
            for other in edges[pending.pop()] - found:
                found.add(other)
                pending.append(other)
        return found

    descendants = reachable(identity, original_children)
    selected = {i for i in (descendants if include_children else {identity}) if workflows[i]['IS_ENABLED']}
    def neighbors(i):
        # Already accepted runs keep their snapshot, including work disabled later.
        disabled_neighbors={j for j in original_parents[i]|original_children[i] if not workflows[j]['IS_ENABLED']}
        return {i}|parents[i]|children[i]|disabled_neighbors
    related = neighbors(identity)
    keys = definition['workflowKeys']
    guards = {keys[i]: sorted(keys[j] for j in neighbors(i)) for i in selected}
    issues = [workflows[i]['WORKFLOW_NAME'] + ': ' + reason for i in sorted(selected)
              for reason in definition['executionIssues'].get(i, [])]
    selected_keys = {keys[i] for i in selected}
    relationships = {keys[i]: {keys[j]: 'SELF' if j == i else 'PARENT' if j in parents[i]|original_parents[i] else 'CHILD'
                    for j in neighbors(i)} for i in selected}
    return dict(keys=sorted(selected_keys), identities=sorted(selected), relatedKeys=sorted(keys[i] for i in related),relationships=relationships,
                guards=guards, issues=issues, workflows=[w['WORKFLOW_NAME'] for i,w in workflows.items() if i in selected],
                disabledKeys=sorted(keys[i] for i in (descendants if include_children else {identity}) if not workflows[i]['IS_ENABLED']))


def outstanding(conn, graph_id, keys, *, exclude=None):
    """Terminal nodes release their reservation even if siblings still execute."""
    return conn.execute('''SELECT n.*,c.snapshot,c.created_at,c.cancel_requested,r.state AS actual_state,r.started_at,
        COALESCE(d.value->>'name',n.node_key) AS name
        FROM kumo_task_engine.cascade_nodes n JOIN kumo_task_engine.cascades c ON c.id=n.cascade_id
        LEFT JOIN kumo_task_engine.runs r ON r.id=n.run_id
        LEFT JOIN LATERAL jsonb_array_elements(c.snapshot->'nodes') d(value) ON d.value->>'key'=n.node_key
        WHERE c.graph_id=%s AND c.state IN ('QUEUED','RUNNING','UNKNOWN') AND n.node_key=ANY(%s)
        AND COALESCE(r.state,n.state)<>ALL(%s)
        AND (%s::uuid IS NULL OR c.id<>%s::uuid)
        ORDER BY c.created_at,n.id''',
        (graph_id,keys,sorted(TERMINAL | {'BLOCKED'}),exclude,exclude)).fetchall()


def relationship(definition, node_key, other_key):
    """Describe direct edges; never infer a relationship through ancestors."""
    if node_key == other_key:
        return 'SELF'
    saved = definition.get('relationships', {}).get(node_key, {}).get(other_key)
    if saved:
        return saved
    nodes = {n['key']: n for n in definition['nodes']}
    parents = lambda key: nodes.get(key, {}).get('parents', []) + definition.get('externalParents', {}).get(key, [])
    if other_key in {p['key'] for p in parents(node_key)}:
        return 'PARENT'
    if node_key in {p['key'] for p in parents(other_key)}:
        return 'CHILD'
    return 'RELATED'


def blocker_message(blocker, names=None):
    names = names or {}
    name = names.get(blocker.get('parent'), blocker.get('parent', ''))
    relation = blocker.get('relationship')
    reason = blocker.get('reason', '')
    state = blocker.get('state')
    status = {'WAITING': 'waiting to start', 'ADMITTED': 'queued', 'UNKNOWN': 'needs review', 'STALE_PARENT': 'out of date'}.get(state, (state or '').lower())
    if relation:
        subject = ('another run of ' if relation == 'SELF' else relation.lower() + ' ') + name
        if status:
            subject += ' (' + status + ')'
        if reason == 'Waiting for related execution to complete':
            return 'Waiting for ' + subject + ' to complete'
        if reason == 'Waiting for parent completion':
            return 'Waiting for ' + subject + ' to complete'
        return subject[:1].upper() + subject[1:] + (': ' + reason if reason else '')
    return (name + ': ' if name else '') + reason


def busy_message(rows, definition=None, node_key=None):
    if definition is None:
        return 'Waiting for ' + ', '.join(dict.fromkeys(row['name'] for row in rows)) + ' (queued, running or needs review)'
    return '; '.join(dict.fromkeys(blocker_message(dict(parent=row['name'],
        relationship=relationship(definition, node_key, row['node_key']),
        state='QUEUED' if row['actual_state']=='RUNNING' and not row['started_at'] else row['actual_state'] or row['state'],
        reason='Waiting for related execution to complete')) for row in rows))
