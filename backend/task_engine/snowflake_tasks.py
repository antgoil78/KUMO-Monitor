"""Metadata-only task lifecycle adapter. No workflow tables or stored executors."""
from contextlib import contextmanager
import re


class TaskDrift(RuntimeError):
    pass


def body_for(job):
    # The prototype validator accepts one SELECT expression only. A native SQL
    # error propagates to FAILED; no wrapper can hide it behind SUCCEEDED.
    return job['snapshot']['sql']


class SnowflakeTasks:
    def __init__(self,settings,connection_factory=None):
        self.settings=settings
        self.connection_factory=connection_factory
        self.calls=0
        self._batch_connection=None
        self._batch_mode=False
        self._batch_manager=None

    @contextmanager
    def batch(self):
        # Called only after PostgreSQL grants real work. Reuse authentication
        # during a lifecycle step; an idle worker never opens this connection.
        self._batch_mode=True
        try:yield
        finally:
            try:
                if self._batch_connection is not None:self._batch_manager.__exit__(None,None,None)
            finally:
                self._batch_connection=None
                self._batch_manager=None
                self._batch_mode=False

    def address(self,job):
        from uuid import UUID
        expected=self.settings.namespace+'.TMP_PG_ORCH_'+UUID(str(job['id'])).hex.upper()
        if job['task_fqn']!=expected:raise TaskDrift('Task address is outside the recorded prototype attempt')
        return expected

    @contextmanager
    def connection(self):
        if self.connection_factory:
            with self.connection_factory() as conn:yield conn
        else:
            import snowflake_client as sf
            with sf.connection(use_warehouse=False,include_context=False,force_service=True) as conn:yield conn

    def query(self,sql,params=()):
        from snowflake.connector import DictCursor
        self.calls+=1
        from contextlib import nullcontext
        if self._batch_mode and self._batch_connection is None:
            self._batch_manager=self.connection()
            self._batch_connection=self._batch_manager.__enter__()
        scope=nullcontext(self._batch_connection) if self._batch_connection is not None else self.connection()
        with scope as conn:
            cur=conn.cursor(DictCursor)
            try:
                cur.execute('USE ROLE '+self.settings.role)
                cur.execute("ALTER SESSION SET QUERY_TAG='KUMO_DEV_POSTGRES_TASK_PROTOTYPE',STATEMENT_TIMEOUT_IN_SECONDS=45")
                cur.execute(sql,params)
                return [{str(k).lower():v for k,v in row.items()} for row in cur.fetchall()]
            finally:
                cur.close()

    def inspect(self,job,missing_ok=False):
        address=self.address(job);name=address.rsplit('.',1)[1]
        tasks=self.query("SHOW TASKS LIKE '"+name+"' IN SCHEMA "+self.settings.namespace)
        tasks=[t for t in tasks if t['name']==name]
        if not tasks:
            if missing_ok:return None
            raise TaskDrift('Recorded task is missing; do not recreate or replay a submitted attempt')
        if len(tasks)!=1:raise TaskDrift('Ambiguous task identity')
        task=tasks[0]
        canonical=lambda s:re.sub(r'\s+',' ',str(s)).strip().rstrip(';')
        if (task.get('owner')!=self.settings.role or task.get('schedule') or task.get('condition')
                or str(task.get('warehouse')).upper()!=self.settings.warehouse
                or canonical(task['definition'])!=canonical(body_for(job))
                or task.get('comment')!='KUMO PostgreSQL prototype attempt '+str(job['id'])
                or (job.get('task_id') and str(task['id'])!=job['task_id'])):
            raise TaskDrift('Task definition/ownership changed; operator review is required')
        return task

    def ensure(self,job):
        address=self.address(job)
        self.query('CREATE TASK IF NOT EXISTS '+address+' WAREHOUSE='+self.settings.warehouse+
                   ' USER_TASK_TIMEOUT_MS='+str(job['snapshot']['timeoutSeconds']*1000)+
                   " TASK_AUTO_RETRY_ATTEMPTS=0 COMMENT='KUMO PostgreSQL prototype attempt "+str(job['id'])+"' AS "+body_for(job))
        return str(self.inspect(job)['id'])

    def execute(self,job):
        self.inspect(job)
        self.query('EXECUTE TASK '+self.address(job))

    def history(self,job):
        # Exact unique task name, namespace and bounded submission time. Preserve
        # SCHEDULED rows with no query ID; they are real outstanding submissions.
        address=self.address(job)
        rows=self.query("SELECT STATE,QUERY_ID,GRAPH_RUN_GROUP_ID,ATTEMPT_NUMBER,ERROR_CODE,ERROR_MESSAGE,QUERY_START_TIME,COMPLETED_TIME "
                        "FROM TABLE(KUMO_DEV.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME=>%s,"
                        "SCHEDULED_TIME_RANGE_START=>TO_TIMESTAMP_LTZ(%s),RESULT_LIMIT=>100)) "
                        "WHERE DATABASE_NAME='KUMO_DEV' AND SCHEMA_NAME='MONITOR_APP' ORDER BY SCHEDULED_TIME",
                        (address.rsplit('.',1)[1],str(job['submit_started_at'])))
        if len({str(r['graph_run_group_id']) for r in rows})>1:
            raise TaskDrift('Multiple native invocations exist for one attempt; do not choose or replay one automatically')
        if job.get('native_group_id') and any(str(r['graph_run_group_id'])!=job['native_group_id'] for r in rows):
            raise TaskDrift('Native run group changed')
        return rows[-1] if rows else None

    def cancel(self,job):
        self.inspect(job)
        self.query('SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS(%s)',(self.address(job),))

    def drop(self,job):
        # A terminal outcome is already durable before this method is called.
        # Missing tasks make cleanup idempotent; a different identity blocks DROP.
        if self.inspect(job,missing_ok=True):self.query('DROP TASK IF EXISTS '+self.address(job))
