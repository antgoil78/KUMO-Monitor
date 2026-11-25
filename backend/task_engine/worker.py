"""One durable worker step. No browser lease, global task scan or idle SF poll."""
from datetime import datetime, timedelta, timezone
import logging
import uuid

from .snowflake_tasks import TaskDrift
from .store import Conflict, TERMINAL
from .recovery import connection_failure,retry_delay

log=logging.getLogger(__name__)


def now():return datetime.now(timezone.utc)


class Worker:
    def __init__(self,store,tasks,owner=None):
        self.store,self.tasks=store,tasks
        self.owner=owner or uuid.uuid4()

    def step(self):
        job=self.store.claim(self.owner)
        if not job:return False
        if hasattr(self.tasks,'batch'):
            with self.tasks.batch():self.process(job)
        else:self.process(job)
        return True

    def process(self,job):
        try:
            if job['cleanup_state']=='PENDING':
                self.cleanup(job)
            elif job['state']=='CREATING':
                if self.store.verify_lease(job,self.owner)['cancel_requested']:
                    self.store.update(job,self.owner,state='CANCELLED',ended_at=now(),next_check_at=None,cleanup_state='PENDING')
                    # A previous worker may already have created the task.
                    self.store.update(job,self.owner,next_check_at=now())
                else:
                    self.store.verify_lease(job,self.owner)
                    task_id=self.tasks.ensure(job)
                    self.store.update(job,self.owner,task_id=task_id,error=None,recovery_checks=0,recovery_kind=None,recovery_started_at=None)
                    if self.store.verify_lease(job,self.owner)['cancel_requested']:
                        self.store.update(job,self.owner,state='CANCELLED',ended_at=now(),cleanup_state='PENDING',next_check_at=now())
                    else:
                        # Commit ambiguity BEFORE the external action. Any crash
                        # or lost acknowledgement from here recovers via history.
                        self.store.update(job,self.owner,state='SUBMITTING',submit_started_at=now(),next_check_at=now())
                        self.store.verify_lease(job,self.owner)
                        self.tasks.execute(job)
                        self.store.update(job,self.owner,state='SUBMITTED',error=None,recovery_checks=0,recovery_kind=None,recovery_started_at=None,next_check_at=now())
            else:
                self.observe(job)
        except Conflict:
            log.warning('Lease lost for prototype attempt %s; recovery belongs to its next owner',job['id'])
        except TaskDrift as exc:
            if job['cleanup_state']=='PENDING':self.cleanup_failure(job,exc)
            else:self.store.update(job,self.owner,state='UNKNOWN',error=str(exc),next_check_at=None,recovery_kind=None,recovery_started_at=None)
        except Exception as exc:
            # If persistence is down, leave the lease/state intact and stop this
            # worker. Its replacement must recover, never reset to QUEUED.
            import psycopg
            if isinstance(exc,psycopg.Error):raise
            failure=connection_failure(exc)
            if failure:self.retry_connection(job,failure)
            elif job['cleanup_state']=='PENDING':self.cleanup_failure(job,exc)
            elif job['state']=='CREATING':
                checks=1 if job.get('recovery_kind')=='NETWORK' else job['recovery_checks']+1
                self.store.update(job,self.owner,recovery_checks=checks,error=type(exc).__name__+': task creation unavailable',
                                  recovery_kind=None,recovery_started_at=None,
                                  state='UNKNOWN' if checks>=self.store.settings.max_recovery_checks else 'CREATING',
                                  next_check_at=None if checks>=self.store.settings.max_recovery_checks else now()+timedelta(seconds=min(60,2**checks)))
            else:self.recover_later(job,'Submission/status unavailable: '+type(exc).__name__)
        finally:
            self.store.release(job,self.owner)

    def recover_later(self,job,error):
        count=1 if job.get('recovery_kind')=='NETWORK' else job['recovery_checks']+1
        exhausted=count>=self.store.settings.max_recovery_checks
        self.store.update(job,self.owner,recovery_checks=count,error=error,
                          recovery_kind=None,recovery_started_at=None,
                          state='UNKNOWN' if exhausted else job['state'],
                          next_check_at=None if exhausted else now()+timedelta(seconds=min(60,2**count)))

    def retry_connection(self,job,reason):
        count=job['recovery_checks']+1 if job.get('recovery_kind')=='NETWORK' else 1
        # Retain the exact task, submission fence and cancellation request.
        # CREATING is idempotent; every later phase only observes history.
        detail={'cleanup_error' if job['cleanup_state']=='PENDING' else 'error':'Snowflake connection unavailable: '+reason}
        self.store.update(job,self.owner,**detail,recovery_kind='NETWORK',
            recovery_started_at=job.get('recovery_started_at') or now(),recovery_checks=count,
            next_check_at=now()+timedelta(seconds=retry_delay(count)))

    def observe(self,job):
        self.store.verify_lease(job,self.owner)
        report=self.tasks.history(job)
        if not report:
            self.recover_later(job,'No native invocation is visible; execution must not be resubmitted')
            return
        raw=report['state']
        identifiers=dict(native_group_id=str(report['graph_run_group_id']),native_attempt=report['attempt_number'],query_id=report['query_id'],observed_at=now())
        if report.get('query_start_time'):
            identifiers['native_started_at']=report['query_start_time']
        if raw in ('SUCCEEDED','FAILED','CANCELLED','FAILED_AND_AUTO_SUSPENDED','SKIPPED'):
            state={'SUCCEEDED':'SUCCESS','FAILED_AND_AUTO_SUSPENDED':'FAILED'}.get(raw,raw)
            self.store.update(job,self.owner,**identifiers,state=state,error=report.get('error_message'),recovery_checks=0,
                              recovery_kind=None,recovery_started_at=None,
                              ended_at=report.get('completed_time') or now(),cleanup_state='PENDING',next_check_at=now())
            return
        if raw not in ('SCHEDULED','EXECUTING'):raise TaskDrift('Unrecognized native task state: '+str(raw))
        state='RUNNING' if raw=='EXECUTING' else 'SCHEDULED'
        self.store.update(job,self.owner,**identifiers,state=state,error=None,recovery_checks=0,
                          recovery_kind=None,recovery_started_at=None,
                          next_check_at=now()+timedelta(seconds=self.store.settings.poll_seconds))
        if self.store.verify_lease(job,self.owner)['cancel_requested']:
            self.tasks.cancel(job)

    def cleanup(self,job):
        if job['state'] not in TERMINAL:raise Conflict('Cannot clean up an unresolved execution')
        self.store.verify_lease(job,self.owner)
        self.tasks.drop(job)
        self.store.update(job,self.owner,cleanup_state='DONE',cleanup_error=None,
            recovery_kind=None,recovery_started_at=None,recovery_checks=0,next_check_at=None)

    def cleanup_failure(self,job,error):
        count=job['cleanup_attempts']+1
        exhausted=count>=3
        self.store.update(job,self.owner,cleanup_state='ERROR' if exhausted else 'PENDING',cleanup_attempts=count,
                          cleanup_error=str(error) if isinstance(error,TaskDrift) else type(error).__name__+': cleanup unavailable',
                          recovery_kind=None,recovery_started_at=None,recovery_checks=0,
                          next_check_at=None if exhausted else now()+timedelta(seconds=5*count))
