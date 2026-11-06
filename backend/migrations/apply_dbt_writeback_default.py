"""Default dbt execution and command previews to WRITEBACK = FALSE.

Read-only preflight unless --apply is supplied. Updates only the shared command
resolver, preserves grants, and never executes dbt or changes schedules.
"""
import argparse
import json
import os

from migration_support import connect
from apply_environment_routing import identifier_path, load_environment, render_procedure
from apply_workflow_command_templates import logical_body
from snowflake.connector import DictCursor


RESOLVER = dict(name='KUMO_DEV.MONITOR_APP.SP_RESOLVE_WORKFLOW_COMMAND',
                signature='(VARIANT, VARCHAR)',
                file='ddl/KUMO_DEV/MONITOR_APP/procedures/SP_RESOLVE_WORKFLOW_COMMAND__741f064d0255.sql')


def needs_update(live, desired):
    actual, expected = logical_body(live).strip(), logical_body(desired).strip()
    if actual == expected:
        return False
    old = expected.replace("' WRITEBACK = FALSE\";", "'\";", 1)
    if old == expected or actual != old:
        raise ValueError('Review live command resolver drift before deployment')
    return True


def run(table, apply=False):
    table = identifier_path(table)
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_DBT_WRITEBACK_DEFAULT'")
        environment, missing_raw = load_environment(cursor, table)
        if missing_raw:
            raise ValueError('Provision RAW_SCHEMA before applying this targeted migration')
        signature, desired = render_procedure(RESOLVER, environment, table)
        cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
        change = needs_update(cursor.fetchone()['DDL'], desired)
        if not apply:
            return dict(applied=False, environment_table=table,
                        procedures_to_update=[signature] if change else [])
        if change:
            cursor.execute(desired.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
            cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
            if needs_update(cursor.fetchone()['DDL'], desired):
                raise RuntimeError('Command resolver readback differs after deployment')
        procedure = signature.split('(')[0]
        for kind in ('SQL', 'DBT'):
            definition = dict(workflowType=kind, sqlCommand='SELECT 1', dbtCommand='build --select tag:daily')
            cursor.execute(f'CALL {procedure}(PARSE_JSON(%s), NULL)', (json.dumps(definition),))
            result = next(iter(cursor.fetchone().values()))
            if isinstance(result, str):
                result = json.loads(result)
            expected = result.get('execSql', '').endswith("' WRITEBACK = FALSE") if kind == 'DBT' else result.get('execSql') == 'SELECT 1'
            if not result.get('ok') or not expected:
                raise RuntimeError('Command resolution verification failed for ' + kind)
        return dict(applied=True, environment_table=table,
                    updated_procedures=[signature] if change else [],
                    resolution_verified=True, workflows_executed=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment-table', default=os.getenv('KUMO_ENVIRONMENT_TABLE', 'KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.environment_table, args.apply), indent=2))
