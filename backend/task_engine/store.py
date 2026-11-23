"""Durable admission, short claims, dependency release and outcome-before-drop.

This deliberately small DEV prototype serializes state transitions with one
PostgreSQL transaction lock. No Snowflake call happens inside a transaction.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import uuid

TERMINAL = {'SUCCESS', 'FAILED', 'CANCELLED', 'SKIPPED'}
NATIVE_ACTIVE = {'SUBMITTING', 'SUBMITTED', 'SCHEDULED', 'RUNNING'}


class Conflict(ValueError):
    pass


def normalize(body):
    if not isinstance(body, dict) or set(body)-{'requestId','jobs','mode'}:
        raise ValueError('Use requestId, jobs and optional SEQUENTIAL/PARALLEL mode')
    request_id = body.get('requestId')
    jobs, mode = body.get('jobs'), body.get('mode', 'SEQUENTIAL')
    if not isinstance(request_id,str) or not 1 <= len(request_id.strip()) <= 128:
        raise ValueError('A stable requestId of at most 128 characters is required')
    if mode not in ('SEQUENTIAL','PARALLEL') or not isinstance(jobs,list) or not 1 <= len(jobs) <= 16:
        raise ValueError('Use SEQUENTIAL/PARALLEL mode with 1–16 disposable SQL jobs')
    keys=[];normalized=[]
    for index,job in enumerate(jobs):
        if not isinstance(job,dict) or set(job)-{'key','sql','timeoutSeconds','dependsOn'}:
            raise ValueError('Jobs accept key, sql, timeoutSeconds and dependsOn')
        key=job.get('key',str(index+1));sql=job.get('sql');timeout=job.get('timeoutSeconds',60)
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',key) or key in keys:
            raise ValueError('Job keys must be unique simple identifiers')
        # First prototype executes SELECT expressions only, never business tables,
        # CALLs, dbt or DML. One statement, without object/session references.
        if (not isinstance(sql,str) or len(sql)>4000 or not re.match(r'^\s*SELECT\s+',sql,re.I)
                or re.search(r';|--|/\*|\*/|\b(FROM|JOIN|INTO|CALL|EXECUTE|USE|ALTER|CREATE|DROP|INSERT|UPDATE|DELETE|COPY|PUT|GET)\b|\$\$',sql,re.I)
                or re.search(r'\b(?:SYSTEM\$(?!WAIT\b)[A-Z_]+|[A-Z_][A-Z0-9_$]*\s*\.)',sql,re.I)):
            raise ValueError('Prototype jobs must be single SELECT expressions without table references; SYSTEM$WAIT is allowed')
        functions=re.findall(r'\b([A-Za-z_][A-Za-z0-9_$]*)\s*\(',sql)
        if any(f.upper() not in {'SYSTEM$WAIT','ABS','TO_NUMBER','TRY_TO_NUMBER','TO_VARCHAR','CURRENT_TIMESTAMP'} for f in functions):
            raise ValueError('This function is outside the disposable SQL prototype allowlist')
        if isinstance(timeout,bool) or not isinstance(timeout,int) or not 1 <= timeout <= 300:
            raise ValueError('Prototype timeouts must be 1–300 seconds')
        deps=job.get('dependsOn',keys[-1:] if mode=='SEQUENTIAL' else [])
        if not isinstance(deps,list) or any(not isinstance(d,str) or d not in keys for d in deps) or len(set(deps))!=len(deps):
            raise ValueError('Dependencies must reference distinct preceding job keys')
        normalized.append(dict(key=key,sql=sql.strip(),timeoutSeconds=timeout,dependsOn=deps))
        keys.append(key)
    return request_id.strip(),dict(mode=mode,jobs=normalized,version=1)


class Store:
    def __init__(self,settings):
        self.settings=settings

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.settings.dsn,row_factory=dict_row,connect_timeout=5,
                               options='-c statement_timeout=10000 -c timezone=UTC')

    def initialize(self):
        with self.connect() as conn:
            conn.execute(Path(__file__).with_name('schema.sql').read_text())

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(671928341)')
            yield conn

    @staticmethod
    def event(conn,run_id,kind,attempt_id=None,**facts):
        from psycopg.types.json import Jsonb
        conn.execute('INSERT INTO kumo_task_engine.events(run_id,attempt_id,kind,facts) VALUES(%s,%s,%s,%s)',
                     (run_id,attempt_id,kind,Jsonb(facts)))

    def submit(self,body,actor):
        from psycopg.types.json import Jsonb
        request_id,definition=normalize(body)
        digest=hashlib.sha256(json.dumps(definition,sort_keys=True).encode()).hexdigest()
        with self.transaction() as conn:
            previous=conn.execute('SELECT * FROM kumo_task_engine.runs WHERE request_id=%s',(request_id,)).fetchone()
            if previous:
                if previous['definition_hash']!=digest:raise Conflict('requestId already belongs to a different frozen plan')
                return dict(ok=True,runId=str(previous['id']),status=previous['state'],reused=True)
            run_id=uuid.uuid4()
            conn.execute("INSERT INTO kumo_task_engine.runs(id,request_id,definition,definition_hash,requested_by,state) VALUES(%s,%s,%s,%s,%s,'QUEUED')",
                         (run_id,request_id,Jsonb(definition),digest,str(actor)[:256]))
            for position,job in enumerate(definition['jobs']):
                attempt=uuid.uuid4();address=self.settings.namespace+'.TMP_PG_ORCH_'+attempt.hex.upper()
                conn.execute("INSERT INTO kumo_task_engine.attempts(id,run_id,job_key,position,snapshot,depends_on,state,task_fqn) VALUES(%s,%s,%s,%s,%s,%s,'QUEUED',%s)",
                             (attempt,run_id,job['key'],position,Jsonb(job),job['dependsOn'],address))
            self.event(conn,run_id,'ADMITTED')
        return dict(ok=True,runId=str(run_id),status='QUEUED',reused=False)

    def read(self,run_id):
        run_id=uuid.UUID(str(run_id))
        with self.connect() as conn:
            run=conn.execute('SELECT * FROM kumo_task_engine.runs WHERE id=%s',(run_id,)).fetchone()
            if not run:raise LookupError('Prototype run not found')
            jobs=conn.execute('SELECT * FROM kumo_task_engine.attempts WHERE run_id=%s ORDER BY position',(run_id,)).fetchall()
            events=conn.execute('SELECT sequence,kind,attempt_id,facts,created_at FROM kumo_task_engine.events WHERE run_id=%s ORDER BY sequence',(run_id,)).fetchall()
        return dict(ok=True,run=run,jobs=jobs,events=events)

    def status(self):
        with self.connect() as conn:
            counts=conn.execute('SELECT state,count(*) AS count FROM kumo_task_engine.attempts GROUP BY state').fetchall()
            cleanup=conn.execute("SELECT count(*) AS count FROM kumo_task_engine.attempts WHERE cleanup_state IN ('PENDING','ERROR')").fetchone()['count']
            retrying=conn.execute("SELECT count(*) AS count FROM kumo_task_engine.attempts WHERE cleanup_state='PENDING'").fetchone()['count']
            runs=conn.execute('SELECT id,state,request_id,created_at,ended_at FROM kumo_task_engine.runs ORDER BY created_at DESC LIMIT 30').fetchall()
        return dict(ok=True,mode='POSTGRES_TASK_PROTOTYPE',environment='DEV',counts={r['state']:r['count'] for r in counts},cleanupPending=cleanup,cleanupRetrying=retrying,runs=runs)

    def cancel(self,run_id):
        run_id=uuid.UUID(str(run_id))
        with self.transaction() as conn:
            if not conn.execute('SELECT id FROM kumo_task_engine.runs WHERE id=%s',(run_id,)).fetchone():raise LookupError('Prototype run not found')
            conn.execute('UPDATE kumo_task_engine.runs SET cancel_requested=true WHERE id=%s',(run_id,))
            conn.execute("UPDATE kumo_task_engine.attempts SET state='CANCELLED',ended_at=clock_timestamp(),next_check_at=NULL WHERE run_id=%s AND state='QUEUED'",(run_id,))
            conn.execute("UPDATE kumo_task_engine.attempts SET next_check_at=clock_timestamp() WHERE run_id=%s AND state IN ('SUBMITTING','SUBMITTED','SCHEDULED','RUNNING')",(run_id,))
            self.event(conn,run_id,'CANCEL_REQUESTED')
            self.refresh(conn,run_id)
        return self.read(run_id)

    def refresh(self,conn,run_id):
        run=conn.execute('SELECT * FROM kumo_task_engine.runs WHERE id=%s',(run_id,)).fetchone()
        states=[r['state'] for r in conn.execute('SELECT state FROM kumo_task_engine.attempts WHERE run_id=%s',(run_id,)).fetchall()]
        if all(s in TERMINAL for s in states):
            state='FAILED' if 'FAILED' in states else 'CANCELLED' if 'CANCELLED' in states else 'SKIPPED' if all(s=='SKIPPED' for s in states) else 'SUCCESS'
            conn.execute('UPDATE kumo_task_engine.runs SET state=%s,ended_at=COALESCE(ended_at,clock_timestamp()) WHERE id=%s',(state,run_id))
            if not run['terminal_event_written']:
                self.event(conn,run_id,'FINISHED',state=state)
                conn.execute('UPDATE kumo_task_engine.runs SET terminal_event_written=true WHERE id=%s',(run_id,))
        else:
            state='UNKNOWN' if 'UNKNOWN' in states else 'QUEUED' if all(s=='QUEUED' for s in states) else 'RUNNING'
            conn.execute('UPDATE kumo_task_engine.runs SET state=%s WHERE id=%s',(state,run_id))

    def claim(self,owner):
        with self.transaction() as conn:
            # Cascade failures through this small immutable DAG without querying Snowflake.
            for _ in range(16):
                skipped=conn.execute("UPDATE kumo_task_engine.attempts a SET state='SKIPPED',ended_at=clock_timestamp(),next_check_at=NULL,error='Dependency did not succeed' WHERE a.state='QUEUED' AND EXISTS (SELECT 1 FROM kumo_task_engine.attempts p WHERE p.run_id=a.run_id AND p.job_key=ANY(a.depends_on) AND p.state IN ('FAILED','CANCELLED','SKIPPED')) RETURNING run_id").fetchall()
                for run in {r['run_id'] for r in skipped}:self.refresh(conn,run)
                if not skipped:break
            job=conn.execute("""SELECT a.*,r.cancel_requested FROM kumo_task_engine.attempts a
                JOIN kumo_task_engine.runs r ON r.id=a.run_id
                WHERE (a.lease_until IS NULL OR a.lease_until<clock_timestamp())
                AND a.next_check_at<=clock_timestamp()
                AND ((a.state='QUEUED' AND NOT r.cancel_requested AND NOT EXISTS (
                    SELECT 1 FROM kumo_task_engine.attempts p WHERE p.run_id=a.run_id AND p.job_key=ANY(a.depends_on) AND p.state<>'SUCCESS'))
                    OR a.state IN ('CREATING','SUBMITTING','SUBMITTED','SCHEDULED','RUNNING')
                    OR a.cleanup_state='PENDING')
                ORDER BY (r.cancel_requested AND a.state IN ('SUBMITTING','SUBMITTED','SCHEDULED','RUNNING')) DESC,
                    (a.cleanup_state='PENDING') DESC,a.next_check_at,a.position
                LIMIT 1 FOR UPDATE OF a SKIP LOCKED""").fetchone()
            if not job:return None
            state='CREATING' if job['state']=='QUEUED' else job['state']
            conn.execute("UPDATE kumo_task_engine.attempts SET state=%s,lease_owner=%s,lease_until=clock_timestamp()+(%s*interval '1 second') WHERE id=%s",(state,owner,self.settings.lease_seconds,job['id']))
            job.update(state=state,lease_owner=owner)
            self.refresh(conn,job['run_id'])
            return job

    def update(self,job,owner,**changes):
        allowed={'state','task_id','native_group_id','native_attempt','query_id','submit_started_at','observed_at','ended_at','error','recovery_checks','next_check_at','cleanup_state','cleanup_attempts','cleanup_error'}
        if not changes or set(changes)-allowed:raise ValueError('Invalid attempt update')
        with self.transaction() as conn:
            row=conn.execute('SELECT * FROM kumo_task_engine.attempts WHERE id=%s AND lease_owner=%s AND lease_until>clock_timestamp() FOR UPDATE',(job['id'],owner)).fetchone()
            if not row:raise Conflict('Worker lease was lost; stop acting on this attempt')
            if row['state'] in TERMINAL and changes.get('state',row['state'])!=row['state']:raise Conflict('A terminal outcome cannot change')
            sql='UPDATE kumo_task_engine.attempts SET '+','.join(k+'=%s' for k in changes)+' WHERE id=%s'
            conn.execute(sql,(*changes.values(),job['id']))
            if 'state' in changes and changes['state']!=row['state']:
                self.event(conn,row['run_id'],'ATTEMPT_STATE',job['id'],state=changes['state'])
            self.refresh(conn,row['run_id'])
        job.update(changes)

    def verify_lease(self,job,owner):
        with self.connect() as conn:
            row=conn.execute('SELECT r.cancel_requested FROM kumo_task_engine.attempts a JOIN kumo_task_engine.runs r ON r.id=a.run_id WHERE a.id=%s AND a.lease_owner=%s AND a.lease_until>clock_timestamp()',(job['id'],owner)).fetchone()
            if not row:raise Conflict('Worker lease expired')
            return row

    def release(self,job,owner):
        with self.connect() as conn:
            conn.execute('UPDATE kumo_task_engine.attempts SET lease_owner=NULL,lease_until=NULL WHERE id=%s AND lease_owner=%s',(job['id'],owner))
