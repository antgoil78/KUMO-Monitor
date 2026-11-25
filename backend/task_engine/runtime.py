"""Supervise the maintained web app, dependency/native worker and scheduler."""
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)


def readiness(store):
    """Read PostgreSQL only; probes must never open a Snowflake connection."""
    with store.connect() as conn:
        version = conn.execute('SELECT MAX(version) AS n FROM kumo_task_engine.schema_version').fetchone()['n']
        master = conn.execute("SELECT id FROM kumo_task_engine.dependency_graphs WHERE key='dev-workflow-catalog'").fetchone()
        workers = {}
        for name, table in [('worker', 'dependency_workers'), ('scheduler', 'scheduler_workers')]:
            row = conn.execute(f'SELECT EXTRACT(EPOCH FROM clock_timestamp()-last_seen) AS age FROM kumo_task_engine.{table} ORDER BY last_seen DESC LIMIT 1').fetchone()
            workers[name] = bool(row and row['age'] <= 30)
    checks = dict(database=True, schema=bool(version and version >= 8), configuration=bool(master), **workers)
    return dict(ok=all(checks.values()), source='POSTGRES', checks=checks)


def child_commands():
    python = sys.executable
    return {
        'web': [python, '-m', 'gunicorn', '--bind', '0.0.0.0:5000', '--workers', '1',
                '--worker-class', 'gthread', '--threads', '64', '--timeout', '120',
                '--graceful-timeout', '20', os.getenv('KUMO_WSGI_APP', 'backend.app:app')],
        'worker': [python, '-m', 'flask', 'task-engine', 'work'],
        'scheduler': [python, '-m', 'flask', 'task-engine', 'schedule'],
    }


def worker_heartbeat(store, owner):
    """Keep readiness responsive while a native call waits on transport recovery."""
    enabled=(os.getenv('KUMO_TASK_ENGINE_WORKER_ENABLED','true').lower()=='true'
             and os.getenv('KUMO_ORCHESTRATION_RUNS_ENABLED','').lower()=='true')
    with store.connect() as conn:
        conn.execute('''INSERT INTO kumo_task_engine.dependency_workers(id,execution_enabled,last_seen)
            VALUES(%s,%s,clock_timestamp()) ON CONFLICT(id) DO UPDATE
            SET execution_enabled=excluded.execution_enabled,last_seen=excluded.last_seen''',(owner,enabled))


def supervise(commands, stop, *, popen=subprocess.Popen, grace_seconds=20):
    children = {}
    code = 0
    try:
        for name, command in commands.items():
            children[name] = popen(command)
            log.info('Started %s process', name)
        while not stop.wait(0.5):
            failed = next((name for name, process in children.items() if process.poll() is not None), None)
            if failed:
                log.error('%s process exited; stopping the service for recovery', failed)
                code = 1
                break
    finally:
        # The submission fence and leases recover interrupted native operations.
        for process in children.values():
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + grace_seconds
        for process in children.values():
            try:
                process.wait(timeout=max(0.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return code


def main():
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    db = Store(Settings.from_environment())
    import psycopg
    for attempt in range(60):
        try:
            db.initialize()
            from .catalog import CatalogConfiguration
            CatalogConfiguration(db).recompile('SPCS_RUNTIME')
            break
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            # Never log PostgreSQL connection strings or passwords.
            log.warning('Waiting for PostgreSQL: %s', type(exc).__name__)
            if stop.wait(5):
                return 0
    else:
        log.error('PostgreSQL startup timed out')
        return 1
    if stop.is_set():
        return 0
    return supervise(child_commands(), stop)


if __name__ == '__main__':
    sys.exit(main())
