"""Register saved DEV catalog schedules without replacing PostgreSQL edits.

Preview by default. New schedules are paused; activation uses the normal
revision-checked schedule controls after migration. No Snowflake access.
"""
import argparse
import hashlib
import json
import uuid

from psycopg.types.json import Jsonb

from task_engine.catalog import CatalogConfiguration, compile_catalog
from task_engine.decisions import record
from task_engine.schedules import Schedules, config_values, next_fire
from task_engine.settings import Settings
from task_engine.store import Conflict, Store


def migrate(store, *, apply=False, expected_revision=None, actor='DEV_SCHEDULE_MIGRATION'):
    if store.settings.namespace != 'KUMO_DEV.MONITOR_APP':
        raise ValueError('This migration is DEV only')
    if apply and type(expected_revision) is not int:
        raise ValueError('Apply requires the reviewed catalog revision')
    with store.transaction() as conn:
        graph = CatalogConfiguration(store).graph(conn)
        if expected_revision is not None and graph['revision'] != expected_revision:
            raise Conflict('Catalog changed; preview the migration again')
        definition = compile_catalog(dict(key=graph['key'], name=graph['name'],
            enabled=graph['enabled'], catalog=graph['definition']['catalog']))
        catalog = definition['catalog']
        existing = conn.execute("SELECT * FROM kumo_task_engine.schedules WHERE plan->>'graphId'=%s",
                                (str(graph['id']),)).fetchall()
        by_workflow = {}
        for row in existing:
            identity = row['plan'].get('workflowId')
            if identity in by_workflow:
                raise Conflict('Duplicate workflow schedules must be resolved first')
            by_workflow[identity] = row
        now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
        missing, preserved = [], []
        for workflow in catalog['workflows']:
            identity = workflow['WORKFLOW_ID']
            if identity in by_workflow:
                preserved.append(str(by_workflow[identity]['id']))
                continue
            cron = workflow.get('SCHEDULE')
            if not cron:
                continue
            issues = definition['executionIssues'][identity]
            if issues:
                raise ValueError(workflow['WORKFLOW_NAME'] + ': ' + '; '.join(issues))
            values = config_values(dict(name=(workflow['WORKFLOW_NAME'] + ' schedule')[:80],
                cron=cron, timezone=workflow.get('SCHEDULE_TIMEZONE') or 'UTC', enabled=False))
            next_fire(values['cron'], values['timezone'], now)
            target = dict(graphId=str(graph['id']), workflowId=str(uuid.UUID(identity)))
            missing.append((workflow, values, target))
        result = dict(applied=apply, catalogRevision=graph['revision'],
            workflows=len(catalog['workflows']), jobs=len(catalog['jobs']),
            preservedScheduleIds=preserved, created=[],
            planned=[dict(workflowId=w['WORKFLOW_ID'], workflow=w['WORKFLOW_NAME'],
                workflowEnabled=bool(w['IS_ENABLED']), **values) for w, values, _ in missing])
        if apply:
            for workflow, values, target in missing:
                request_id = 'dev-workflow-schedule-migration:v1:' + workflow['WORKFLOW_ID']
                identity = uuid.uuid5(uuid.NAMESPACE_URL, request_id)
                creation_hash = hashlib.sha256(json.dumps(dict(values, plan=target), sort_keys=True).encode()).hexdigest()
                # Disabled workflows also retain their paused schedule intent.
                # The normal enable path validates catalog_target before activation.
                row = conn.execute('''INSERT INTO kumo_task_engine.schedules
                    (id,request_id,creation_hash,name,cron,timezone,plan,grace_seconds,enabled,next_fire_at,created_by,updated_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,false,NULL,%s,%s) RETURNING *''',
                    (identity,request_id,creation_hash,values['name'],values['cron'],values['timezone'],
                     Jsonb(target),values['graceSeconds'],actor,actor)).fetchone()
                Schedules.audit(conn,row,'CREATED',actor,definition=dict(values,plan=target),
                                migration='dev-workflow-schedules-v1',catalogRevision=graph['revision'])
                record(conn,'schedule','SCHEDULE_MIGRATED',graph_id=graph['id'],graph_revision=graph['revision'],
                       schedule_id=identity,actor=actor,workflowId=workflow['WORKFLOW_ID'],enabled=False,
                       sourceScheduleEnabled=bool(workflow.get('IS_SCHEDULE_ENABLED')))
                result['created'].append(str(identity))
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--expected-revision',type=int)
    args=parser.parse_args()
    print(json.dumps(migrate(Store(Settings.from_environment()),apply=args.apply,
        expected_revision=args.expected_revision),default=str),flush=True)


if __name__ == '__main__':
    main()
