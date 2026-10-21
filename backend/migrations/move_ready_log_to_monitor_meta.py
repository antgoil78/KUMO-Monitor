"""Move DEV's current readiness log beside its history and update its consumers.

Read-only preflight by default. --apply preserves the table and its data by
renaming it, preserves procedure grants and master state, and leaves a read-only
compatibility view for the existing app image. Only the changed app DDL is saved.
TST is deliberately outside this migration.
"""
import argparse
import json
import subprocess
import sys

from migration_support import ROOT, connect, procedure_parts
from apply_workflow_command_templates import logical_body
from snowflake.connector import DictCursor

OLD = 'KUMO_DEV.META._TMP_SET_READY_LOG'
NEW = 'KUMO_DEV.MONITOR_META._TMP_SET_READY_LOG'
MASTER = 'KUMO_DEV.MONITOR_APP.TASK_WF_MASTER_DISPATCHER'
OLD_REF = "V_ENV:APP_SCHEMA::VARCHAR || '._TMP_SET_READY_LOG'"
NEW_REF = "V_ENV:META_SCHEMA::VARCHAR || '._TMP_SET_READY_LOG'"
TARGETS = (
    ('MONITOR_APP', 'SP_RAW_LIM_META_REFRESH', '(VARCHAR,VARCHAR)'),
    ('MONITOR_APP', 'SP_RAW_LIM_SET_READY', '(VARCHAR,BOOLEAN,BOOLEAN,VARCHAR)'),
    ('MONITOR_META', 'LOAD_RAW_LIM_FROM_STAGE', '(VARCHAR,DATE,DATE,BOOLEAN,BOOLEAN,BOOLEAN,VARCHAR)'),
)


def with_grants(ddl):
    header, _ = procedure_parts(ddl)
    return header.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1) + '\nAS $$' + logical_body(ddl) + '$$'


def fingerprint(cursor, name):
    cursor.execute('SELECT COUNT(*) AS N, HASH_AGG(*) AS HASH FROM ' + name)
    return cursor.fetchone()


def run(apply=False):
    changed = []
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_MOVE_CURRENT_READY_LOG'")
        cursor.execute('USE ROLE KUMO_ADMIN')
        cursor.execute("SELECT TABLE_SCHEMA,TABLE_TYPE FROM KUMO_DEV.INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='_TMP_SET_READY_LOG' AND TABLE_SCHEMA IN ('META','MONITOR_META')")
        locations = {row['TABLE_SCHEMA']: row['TABLE_TYPE'] for row in cursor.fetchall()}
        needs_move = locations == {'META': 'BASE TABLE'}
        already_moved = locations == {'MONITOR_META': 'BASE TABLE', 'META': 'VIEW'}
        if not needs_move and not already_moved:
            raise ValueError('Review unexpected readiness log locations before moving: ' + str(locations))
        plans = []
        for schema, name, args in TARGETS:
            path = next((ROOT / f'snowflake/ddl/KUMO_DEV/{schema}/procedures').glob(name + '__*.sql'))
            desired = path.read_text()
            desired_body = logical_body(desired).strip()
            if desired_body.count(NEW_REF) != 1 or OLD_REF in desired_body:
                raise ValueError('Saved definition must use META_SCHEMA for the current readiness log: ' + name)
            signature = f'KUMO_DEV.{schema}.{name}{args}'
            cursor.execute("SELECT GET_DDL('PROCEDURE',%s,TRUE) AS DDL", (signature,))
            live = cursor.fetchone()['DDL']
            live_body = logical_body(live).strip()
            if live_body == desired_body:
                continue
            if live_body != desired_body.replace(NEW_REF, OLD_REF):
                raise ValueError('Review live procedure drift before migration: ' + signature)
            plans.append((signature, with_grants(desired), with_grants(live)))
        cursor.execute("SHOW TASKS LIKE 'TASK_WF_MASTER_DISPATCHER' IN SCHEMA KUMO_DEV.MONITOR_APP")
        task = cursor.fetchone()
        if not task:
            raise ValueError('Expected the DEV master dispatcher')
        before = fingerprint(cursor, OLD if needs_move else NEW)
        cursor.execute('SHOW GRANTS ON TABLE ' + (OLD if needs_move else NEW))
        grants_before = cursor.fetchall()
        if not apply:
            return dict(applied=False, move_table=needs_move, procedures_to_update=[sig for sig, _, _ in plans],
                        current_rows=before['N'], master_state=task['state'], compatibility_view=OLD)
        resume = False
        moved = False
        made_view = False
        replaced = []
        try:
            if needs_move or plans:
                if task['state'] == 'started':
                    cursor.execute('ALTER TASK ' + MASTER + ' SUSPEND')
                    resume = True
                cursor.execute("SELECT COUNT(*) AS N FROM KUMO_DEV.MONITOR_META.WORKFLOW_HISTORY WHERE UPPER(TRIM(STATUS)) IN ('RUNNING','STARTING')")
                if cursor.fetchone()['N']:
                    raise ValueError('Wait for active workflow runs to finish before moving the readiness log')
                # Fingerprint after pausing dispatch, immediately before the move.
                before = fingerprint(cursor, OLD if needs_move else NEW)
            if needs_move:
                cursor.execute('ALTER TABLE ' + OLD + ' RENAME TO ' + NEW)
                moved = True
                cursor.execute('CREATE VIEW ' + OLD + ' AS SELECT * FROM ' + NEW)
                made_view = True
                for grant in grants_before:
                    if grant['privilege'] == 'SELECT' and grant['granted_to'] in ('ROLE', 'DATABASE_ROLE'):
                        grantee = '.'.join('"' + part.replace('"', '""') + '"' for part in grant['grantee_name'].split('.'))
                        cursor.execute(f"GRANT SELECT ON VIEW {OLD} TO {grant['granted_to'].replace('_',' ')} {grantee}")
            for signature, desired, original in plans:
                cursor.execute(desired)
                replaced.append((signature, original))
                cursor.execute("SELECT GET_DDL('PROCEDURE',%s,TRUE) AS DDL", (signature,))
                if logical_body(cursor.fetchone()['DDL']).strip() != logical_body(desired).strip():
                    raise RuntimeError('Updated procedure readback differs: ' + signature)
            after = fingerprint(cursor, NEW)
            if before != after or fingerprint(cursor, OLD) != after:
                raise RuntimeError('Readiness log data fingerprint changed during migration')
            cursor.execute('SHOW GRANTS ON TABLE ' + NEW)
            grant_key = lambda row: (row['privilege'], row['granted_to'], row['grantee_name'], row['grant_option'])
            if not {grant_key(row) for row in grants_before}.issubset({grant_key(row) for row in cursor.fetchall()}):
                raise RuntimeError('The moved table lost an existing grant')
            # Exercise the app's actual latest/history read paths, without mutation.
            cursor.execute(f"SELECT COUNT(*) AS N FROM (SELECT * FROM {NEW} UNION ALL SELECT * FROM KUMO_DEV.MONITOR_META._SET_READY_LOG) WHERE STATUS IN ('UPDATED','PREVIOUSLY_LOADED')")
            cursor.fetchone()
            if moved:
                changed.append(('TABLE', NEW))
            changed.extend(('PROCEDURE', sig) for sig, _ in replaced)
        except Exception:
            for signature, original in reversed(replaced):
                cursor.execute(original)
            if made_view:
                cursor.execute('DROP VIEW ' + OLD)
            if moved:
                cursor.execute('ALTER TABLE ' + NEW + ' RENAME TO ' + OLD)
            raise
        finally:
            if resume:
                cursor.execute('ALTER TASK ' + MASTER + ' RESUME')
    if changed:
        command = [sys.executable, str(ROOT / 'snowflake/export_ddl.py')]
        for kind, name in changed:
            command += ['--object', kind, name]
        subprocess.run(command, check=True, cwd=ROOT)
    return dict(applied=True, table=NEW, rows_preserved=after['N'], data_fingerprint_preserved=True,
                grants_preserved=True, procedures_updated=[sig for sig, _ in replaced],
                master_state=task['state'], compatibility_view=OLD, changed_ddl_objects=len(changed))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), indent=2))
