"""PostgreSQL schedules for disposable trial plans; no Snowflake access."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import Conflict, WorkflowBusy, normalize
from .decisions import record as record_decision

UTC = timezone.utc


def calendar(cron, zone):
    if not isinstance(cron, str) or len(cron) > 120:
        raise ValueError('Enter a five-field numeric cron expression')
    cron = ' '.join(cron.split())
    if len(cron.split()) != 5 or not re.fullmatch(r'[0-9*/ ,\-]+', cron):
        raise ValueError('Use five numeric cron fields with *, lists, ranges or steps')
    if not isinstance(zone, str) or len(zone) > 100:
        raise ValueError('Enter an IANA timezone such as Europe/Stockholm')
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Unknown schedule timezone') from None
    from croniter import croniter
    if not croniter.is_valid(cron):
        raise ValueError('Invalid cron field or range')
    return cron, tz


def next_fire(cron, zone, after):
    """Strictly after an aware instant; gaps skip, repeated wall times run once.

    croniter matches wall-clock fields in a fictional UTC calendar. We apply
    zoneinfo ourselves so DST policy does not depend on library heuristics.
    A repeated local time uses its first occurrence (fold=0) only.
    """
    cron, tz = calendar(cron, zone)
    if after.tzinfo is None:
        raise ValueError('Schedule calculation requires an aware timestamp')
    from croniter import croniter, CroniterBadDateError
    wall = after.astimezone(tz).replace(tzinfo=UTC)
    iterator = croniter(cron, wall, day_or=True, max_years_between_matches=8)
    try:
        for _ in range(2000):  # Includes rare zones with an entire skipped day.
            candidate = iterator.get_next(datetime).replace(tzinfo=None)
            local = candidate.replace(tzinfo=tz, fold=0)
            instant = local.astimezone(UTC)
            if instant > after and instant.astimezone(tz).replace(tzinfo=None) == candidate:
                return instant
    except CroniterBadDateError:
        pass
    raise ValueError('Schedule has no supported future occurrence within eight years')


def config_values(body):
    name, grace, enabled = body.get('name'), body.get('graceSeconds', 60), body.get('enabled', False)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError('Schedule name must contain 1–80 characters')
    if isinstance(grace, bool) or not isinstance(grace, int) or not 0 <= grace <= 3600:
        raise ValueError('Grace period must be 0–3600 seconds')
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be true or false')
    cron, _ = calendar(body.get('cron'), body.get('timezone', 'Europe/Stockholm'))
    return dict(name=name.strip(), cron=cron, timezone=body.get('timezone', 'Europe/Stockholm'),
                graceSeconds=grace, enabled=enabled)


def catalog_target(conn,target):
    from .admission import execution_scope
    if not isinstance(target,dict) or set(target)!={'graphId','workflowId'}:
        raise ValueError('A workflow schedule needs graphId and workflowId')
    graph=conn.execute('SELECT * FROM kumo_task_engine.dependency_graphs WHERE id=%s',(uuid.UUID(str(target['graphId'])),)).fetchone()
    if not graph or 'catalog' not in graph['definition']:raise ValueError('Select a catalog workflow for scheduling')
    scope=execution_scope(graph['definition'],target['workflowId'])
    workflow=next(w for w in graph['definition']['catalog']['workflows'] if w['WORKFLOW_ID']==target['workflowId'])
    if scope['issues']:raise ValueError('; '.join(scope['issues']))
    if not graph['enabled'] or not workflow['IS_ENABLED']:raise ValueError('The scheduled workflow is disabled')


class Schedules:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def audit(conn, row, action, actor, **facts):
        from psycopg.types.json import Jsonb
        conn.execute('INSERT INTO kumo_task_engine.schedule_events(schedule_id,revision,action,actor,facts) VALUES(%s,%s,%s,%s,%s)',
                     (row['id'], row['revision'], action, str(actor)[:256], Jsonb(facts)))

    def create(self, body, actor):
        from psycopg.types.json import Jsonb
        if not isinstance(body, dict) or set(body) - {'requestId', 'name', 'cron', 'timezone', 'graceSeconds', 'enabled', 'plan','target'}:
            raise ValueError('Use schedule settings and either a workflow target or a disposable plan')
        values = config_values(body)
        target=body.get('target')
        if target is not None:
            if 'plan' in body:raise ValueError('Choose either a workflow target or a disposable plan')
            if not isinstance(target,dict) or set(target)!={'graphId','workflowId'}:raise ValueError('A workflow schedule needs graphId and workflowId')
            target=dict(graphId=str(uuid.UUID(str(target['graphId']))),workflowId=str(uuid.UUID(str(target['workflowId']))))
            request_id=body.get('requestId')
            if not isinstance(request_id,str) or not 1<=len(request_id.strip())<=128:raise ValueError('A stable requestId is required')
            request_id=request_id.strip();frozen=target
        else:
            plan = body.get('plan')
            if not isinstance(plan, dict) or not plan.get('workflowKey') or 'requestId' in plan:
                raise ValueError('A disposable plan needs a stable workflowKey, jobs and optional mode')
            request_id, frozen = normalize(dict(plan, requestId=body.get('requestId')))
        digest = hashlib.sha256(json.dumps(dict(values, plan=frozen), sort_keys=True).encode()).hexdigest()
        with self.store.transaction() as conn:
            old = conn.execute('SELECT * FROM kumo_task_engine.schedules WHERE request_id=%s', (request_id,)).fetchone()
            if old:
                if old['creation_hash'] != digest:
                    raise Conflict('Schedule requestId already belongs to a different definition')
                return dict(ok=True, schedule=old, reused=True)
            if target is not None:
                catalog_target(conn,target)
                if conn.execute("SELECT id FROM kumo_task_engine.schedules WHERE plan->>'graphId'=%s AND plan->>'workflowId'=%s",(str(target['graphId']),target['workflowId'])).fetchone():
                    raise Conflict('This workflow already has a schedule; refresh before editing it')
            now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            future = next_fire(values['cron'], values['timezone'], now)  # Validate paused schedules too.
            row = conn.execute('''INSERT INTO kumo_task_engine.schedules
                (id,request_id,creation_hash,name,cron,timezone,plan,grace_seconds,enabled,next_fire_at,created_by,updated_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (uuid.uuid4(), request_id, digest, values['name'], values['cron'], values['timezone'], Jsonb(frozen),
                 values['graceSeconds'], values['enabled'], future if values['enabled'] else None, str(actor)[:256], str(actor)[:256])).fetchone()
            self.audit(conn, row, 'CREATED', actor, definition=dict(values, plan=frozen))
        return dict(ok=True, schedule=row, reused=False)

    def update(self, schedule_id, body, actor):
        schedule_id = uuid.UUID(str(schedule_id))
        if (not isinstance(body, dict) or set(body) - {'expectedRevision', 'name', 'cron', 'timezone', 'graceSeconds', 'enabled'}
                or isinstance(body.get('expectedRevision'), bool) or not isinstance(body.get('expectedRevision'), int)):
            raise ValueError('Use expectedRevision and schedule configuration fields; plans are immutable')
        with self.store.transaction() as conn:
            row = conn.execute('SELECT * FROM kumo_task_engine.schedules WHERE id=%s FOR UPDATE', (schedule_id,)).fetchone()
            if not row:
                raise LookupError('Trial schedule not found')
            if row['revision'] != body['expectedRevision']:
                raise Conflict('Schedule changed; refresh before saving')
            values = config_values(dict(dict(name=row['name'], cron=row['cron'], timezone=row['timezone'], graceSeconds=row['grace_seconds'], enabled=row['enabled']), **{k:v for k,v in body.items() if k != 'expectedRevision'}))
            if values['enabled'] and 'graphId' in row['plan']:catalog_target(conn,row['plan'])
            now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            future = next_fire(values['cron'], values['timezone'], now)
            changed_clock = values['cron'] != row['cron'] or values['timezone'] != row['timezone'] or not row['enabled']
            due = (future if changed_clock else row['next_fire_at']) if values['enabled'] else None
            action = 'RESUMED' if values['enabled'] and not row['enabled'] else 'PAUSED' if row['enabled'] and not values['enabled'] else 'UPDATED'
            row = conn.execute('''UPDATE kumo_task_engine.schedules SET name=%s,cron=%s,timezone=%s,grace_seconds=%s,
                enabled=%s,next_fire_at=%s,validation_error=NULL,revision=revision+1,updated_by=%s,updated_at=clock_timestamp()
                WHERE id=%s RETURNING *''',
                (values['name'], values['cron'], values['timezone'], values['graceSeconds'], values['enabled'], due, str(actor)[:256], schedule_id)).fetchone()
            self.audit(conn, row, action, actor, definition=values)
        return dict(ok=True, schedule=row)

    def status(self):
        with self.store.connect() as conn:
            now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            rows = conn.execute('''SELECT s.*,
                CASE WHEN s.validation_error IS NOT NULL THEN 'INVALID' WHEN s.enabled THEN 'ENABLED' ELSE 'PAUSED' END AS state,
                (SELECT jsonb_build_object('state',o.state,'scheduledFor',o.scheduled_for,'windowEnd',o.window_end,
                    'observedAt',o.observed_at,'reason',o.reason,'runId',o.run_id,'cascadeId',o.cascade_id,
                    'runState',COALESCE(r.state,nr.state,CASE n.state WHEN 'WAITING' THEN 'QUEUED' WHEN 'ADMITTED' THEN 'QUEUED' ELSE n.state END,c.state))
                 FROM kumo_task_engine.schedule_occurrences o LEFT JOIN kumo_task_engine.runs r ON r.id=o.run_id
                 LEFT JOIN kumo_task_engine.cascades c ON c.id=o.cascade_id
                 LEFT JOIN kumo_task_engine.cascade_nodes n ON n.cascade_id=c.id AND c.snapshot->'workflowIds'->>n.node_key=s.plan->>'workflowId'
                 LEFT JOIN kumo_task_engine.runs nr ON nr.id=n.run_id
                 WHERE o.schedule_id=s.id ORDER BY o.scheduled_for DESC LIMIT 1) AS last_occurrence,
                (SELECT jsonb_build_object('id',r.id,'state',r.state) FROM kumo_task_engine.runs r
                 WHERE r.workflow_key=s.plan->>'workflowKey' AND r.state IN ('QUEUED','RUNNING','UNKNOWN') LIMIT 1) AS active_run
                FROM kumo_task_engine.schedules s ORDER BY s.created_at DESC LIMIT 100''').fetchall()
            occurrences = conn.execute('''SELECT o.id,o.schedule_id,o.revision,o.scheduled_for,o.window_end,o.state,o.reason,o.run_id,o.cascade_id,o.observed_at,
                COALESCE(r.state,nr.state,CASE n.state WHEN 'WAITING' THEN 'QUEUED' WHEN 'ADMITTED' THEN 'QUEUED' ELSE n.state END,c.state) AS run_state
                FROM kumo_task_engine.schedule_occurrences o LEFT JOIN kumo_task_engine.runs r ON r.id=o.run_id
                JOIN kumo_task_engine.schedules s ON s.id=o.schedule_id
                LEFT JOIN kumo_task_engine.cascades c ON c.id=o.cascade_id
                LEFT JOIN kumo_task_engine.cascade_nodes n ON n.cascade_id=c.id AND c.snapshot->'workflowIds'->>n.node_key=s.plan->>'workflowId'
                LEFT JOIN kumo_task_engine.runs nr ON nr.id=n.run_id
                ORDER BY o.observed_at DESC,o.scheduled_for DESC LIMIT 100''').fetchall()
            workers = conn.execute('SELECT * FROM kumo_task_engine.scheduler_workers ORDER BY last_seen DESC').fetchall()
            enabled = conn.execute('SELECT count(*) AS n FROM kumo_task_engine.schedules WHERE enabled').fetchone()['n']
            overdue = conn.execute("SELECT count(*) AS n FROM kumo_task_engine.schedules WHERE enabled AND next_fire_at+(grace_seconds*interval '1 second')<clock_timestamp()").fetchone()['n']
        ready = [w for w in workers if (now-w['last_seen']).total_seconds() <= 10]
        live = [w for w in ready if w['execution_enabled']]
        state = ('DELAYED' if overdue else 'HEALTHY') if live else 'EXECUTION_DISABLED' if ready else 'DELAYED' if workers and (now-workers[0]['last_seen']).total_seconds() <= 30 else 'UNAVAILABLE' if workers else 'NOT_STARTED'
        return dict(ok=True, schedules=rows, occurrences=occurrences, enabledCount=enabled, generatedAt=now,
                    scheduler=dict(state=state, liveWorkers=len(live), lastSeen=workers[0]['last_seen'] if workers else None, overdueSchedules=overdue))


class Scheduler:
    def __init__(self, store, owner=None):
        self.store = store
        self.owner = owner or uuid.uuid4()

    @staticmethod
    def record(conn, row, due, state, reason=None, window_end=None):
        from psycopg.types.json import Jsonb
        snapshot = {k:row[k] for k in ('name', 'cron', 'timezone', 'grace_seconds', 'plan')}
        occurrence=conn.execute('''INSERT INTO kumo_task_engine.schedule_occurrences
            (id,schedule_id,revision,scheduled_for,window_end,state,reason,snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(schedule_id,scheduled_for) DO NOTHING RETURNING id''',
            (uuid.uuid4(), row['id'], row['revision'], due, window_end, state, reason, Jsonb(snapshot))).fetchone()
        record_decision(conn,'schedule','OCCURRENCE_MISSED' if state=='MISSED' else 'OCCURRENCE_DUE' if occurrence else 'OCCURRENCE_ALREADY_RECORDED',
            schedule_id=row['id'],occurrence_id=occurrence['id'] if occurrence else None,
            scheduledFor=due,scheduleRevision=row['revision'],windowEnd=window_end,reason=reason)
        return occurrence

    def step(self, execution_enabled=True):
        context={}
        try:
            return self._step(execution_enabled,context)
        except Exception as exc:
            # Committed decisions from the failed evaluation have rolled back.
            # Keep only the error, using its type rather than raw SQL/DSN text.
            if context:
                with self.store.transaction() as conn:
                    record_decision(conn,'schedule','SCHEDULE_EVALUATION_ERROR',actor=self.owner,
                        errorType=type(exc).__name__,evaluationRolledBack=True,**context)
            raise

    def _step(self, execution_enabled, context):
        # This lock also serializes manual admission and schedule edits. Both the
        # occurrence and run admission commit with the next due time, or none do.
        with self.store.transaction() as conn:
            now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            conn.execute('''INSERT INTO kumo_task_engine.scheduler_workers(id,execution_enabled,last_seen) VALUES(%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET execution_enabled=excluded.execution_enabled,last_seen=excluded.last_seen''',
                (self.owner, execution_enabled, now))
            if not execution_enabled:
                return False
            row = conn.execute('''SELECT * FROM kumo_task_engine.schedules WHERE enabled AND next_fire_at<=%s
                ORDER BY next_fire_at LIMIT 1 FOR UPDATE SKIP LOCKED''', (now,)).fetchone()
            if not row:
                return False
            context['schedule_id']=row['id']
            conn.execute('SAVEPOINT schedule_evaluation')
            try:
                original = row['next_fire_at']
                cutoff = now - timedelta(seconds=row['grace_seconds'])
                due = next_fire(row['cron'], row['timezone'], cutoff-timedelta(microseconds=1)) if original < cutoff else original
                if due > now:
                    self.record(conn, row, original, 'MISSED', 'Outside the grace period; no catch-up execution', window_end=cutoff)
                    future = due
                else:
                    future = next_fire(row['cron'], row['timezone'], due)
                    while future <= now:  # At most 61 minute occurrences inside the bounded grace period.
                        due, future = future, next_fire(row['cron'], row['timezone'], future)
                    if original < due:
                        self.record(conn, row, original, 'MISSED', 'Older occurrences coalesced; only the latest due occurrence is eligible', window_end=due)
                    occurrence = self.record(conn, row, due, 'ADMITTED')
                    if occurrence:
                        plan = {k:v for k,v in row['plan'].items() if k != 'version'}
                        if 'graphId' in plan:
                            from .dependencies import Graphs
                            catalog_target(conn,plan)
                            result=Graphs(self.store).start_in_transaction(conn,plan['graphId'],
                                dict(workflowId=plan['workflowId'],requestId='schedule-'+str(row['id'])+'-'+due.astimezone(UTC).isoformat()),
                                'SCHEDULE:'+str(row['id']),trigger='SCHEDULED')
                            conn.execute('UPDATE kumo_task_engine.schedule_occurrences SET cascade_id=%s WHERE id=%s',(uuid.UUID(result['cascadeId']),occurrence['id']))
                            record_decision(conn,'schedule','OCCURRENCE_ADMITTED',
                                graph_id=plan['graphId'],cascade_id=result['cascadeId'],
                                schedule_id=row['id'],occurrence_id=occurrence['id'],actor=self.owner,
                                workflowId=plan['workflowId'],scheduledFor=due,
                                scheduleRevision=row['revision'],reused=result['reused'])
                        else:
                            self.admit_trial(conn,row,plan,due,occurrence)
                conn.execute('UPDATE kumo_task_engine.schedules SET next_fire_at=%s WHERE id=%s', (future, row['id']))
                record_decision(conn,'schedule','SCHEDULE_ADVANCED',schedule_id=row['id'],actor=self.owner,
                    previousFireAt=original,nextFireAt=future,scheduleRevision=row['revision'])
            except ValueError as exc:
                # Validation errors stop this schedule; database/admission errors
                # propagate and roll back its entire transaction for recovery.
                if isinstance(exc, Conflict):
                    raise
                conn.execute('ROLLBACK TO SAVEPOINT schedule_evaluation')
                conn.execute('UPDATE kumo_task_engine.schedules SET enabled=false,next_fire_at=NULL,validation_error=%s WHERE id=%s', (str(exc), row['id']))
                Schedules.audit(conn, row, 'INVALID', 'SCHEDULER', reason=str(exc))
                record_decision(conn,'schedule','SCHEDULE_DISABLED_INVALID',schedule_id=row['id'],actor=self.owner,
                    reason=str(exc),scheduleRevision=row['revision'])
            return True

    def admit_trial(self,conn,row,plan,due,occurrence):
        try:
            run = self.store.submit_in_transaction(conn, dict(plan, requestId='schedule-'+str(row['id'])+'-'+due.astimezone(UTC).isoformat()), 'SCHEDULE:'+str(row['id']))
        except WorkflowBusy:
            conn.execute("UPDATE kumo_task_engine.schedule_occurrences SET state='SKIPPED_OVERLAP',reason='An execution for this trial workflow is still outstanding' WHERE id=%s", (occurrence['id'],))
            record_decision(conn,'schedule','OCCURRENCE_SKIPPED_OVERLAP',schedule_id=row['id'],
                occurrence_id=occurrence['id'],actor=self.owner,scheduledFor=due,workflowKey=plan['workflowKey'])
        else:
            conn.execute('UPDATE kumo_task_engine.schedule_occurrences SET run_id=%s WHERE id=%s', (uuid.UUID(run['runId']), occurrence['id']))
            self.store.event(conn, uuid.UUID(run['runId']), 'SCHEDULED', scheduleId=str(row['id']), revision=row['revision'], scheduledFor=due.isoformat())
            record_decision(conn,'schedule','OCCURRENCE_ADMITTED',schedule_id=row['id'],
                occurrence_id=occurrence['id'],run_id=run['runId'],actor=self.owner,
                scheduledFor=due,scheduleRevision=row['revision'],reused=run['reused'])
