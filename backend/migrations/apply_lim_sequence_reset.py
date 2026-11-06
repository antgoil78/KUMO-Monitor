"""Deploy only the two LIM sequence-reset corrections to one environment.

Read-only preflight unless --apply is supplied. Preserves procedure grants;
does not reset configuration, delete logs, or execute workflows.
"""
import argparse
import hashlib
import json
import os

from migration_support import ROOT, connect
from apply_environment_routing import identifier_path, load_environment, render_procedure
from apply_workflow_command_templates import logical_body
from snowflake.connector import DictCursor


BEFORE = {
    'SP_RAW_LIM_META_REFRESH': '18d46479308541f67cdaf6661b86166041932bf7eed1748a24dc9907c3b9373d',
    'SP_RAW_LIM_SET_READY': 'd905d6efb2bdfc510f1701bdba05e3ff71a05ed2914288f56fbeeac92fc3b4ba',
}


def definitions(environment, table):
    manifest = json.loads((ROOT / 'snowflake/manifest.json').read_text())
    for item in manifest['objects']:
        name = item['name'].rsplit('.', 1)[-1]
        if item['type'] == 'PROCEDURE' and name in BEFORE:
            signature, ddl = render_procedure(item, environment, table)
            yield name, signature, ddl


def normalized_hash(ddl, environment):
    helper = environment['DATABASE'] + '.' + environment['META_SCHEMA'] + '.SP_GET_ENVIRONMENT'
    body = logical_body(ddl).replace(helper, 'KUMO_DEV.MONITOR_META.SP_GET_ENVIRONMENT')
    return hashlib.sha256(body.strip().encode()).hexdigest()


def run(table, apply=False):
    table = identifier_path(table)
    with connect() as conn, conn.cursor(DictCursor) as cursor:
        cursor.execute("ALTER SESSION SET QUERY_TAG='KUMO_LIM_SEQUENCE_RESET_MIGRATION'")
        environment, missing_raw = load_environment(cursor, table)
        if missing_raw:
            raise ValueError('Provision RAW_SCHEMA before applying this targeted migration')
        changes = []
        for name, signature, desired in definitions(environment, table):
            cursor.execute("SELECT GET_DDL('PROCEDURE',%s,TRUE) AS DDL", (signature,))
            live = cursor.fetchone()['DDL']
            actual, expected = normalized_hash(live, environment), normalized_hash(desired, environment)
            if actual == expected:
                continue
            if actual != BEFORE[name]:
                raise ValueError('Review live procedure drift before deployment: ' + signature)
            changes.append((signature, desired, expected))
        metadata = environment['DATABASE'] + '.' + environment['META_SCHEMA']
        cursor.execute(f"SELECT COUNT(*) AS N FROM {metadata}.WORKFLOW_HISTORY WHERE UPPER(TRIM(STATUS)) IN ('RUNNING','STARTING','QUEUED','INITIATING','DISPATCHING')")
        if cursor.fetchone()['N']:
            raise ValueError('Wait for active or queued workflows before changing readiness procedures')
        if not apply:
            return dict(applied=False, environment_table=table,
                        procedures_to_update=[signature for signature, _, _ in changes])
        updated = []
        for signature, desired, expected in changes:
            cursor.execute(desired.replace('\nRETURNS ', '\nCOPY GRANTS\nRETURNS ', 1))
            cursor.execute("SELECT GET_DDL('PROCEDURE',%s,TRUE) AS DDL", (signature,))
            if normalized_hash(cursor.fetchone()['DDL'], environment) != expected:
                raise RuntimeError('Procedure readback differs after deployment: ' + signature)
            updated.append(signature)
            print('Updated ' + signature, flush=True)
        return dict(applied=True, environment_table=table, updated_procedures=updated,
                    configuration_changed=False, workflows_executed=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment-table', default=os.getenv('KUMO_ENVIRONMENT_TABLE', 'KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.environment_table, args.apply), indent=2))
