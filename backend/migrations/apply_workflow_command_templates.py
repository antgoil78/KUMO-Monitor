"""Deploy shared command resolution and convert DEV definitions to [VARIABLE].

Default: inspect only the two dispatchers and current workflow/job configuration.
--apply deploys the resolver/dispatchers and transactionally updates definitions.
Never executes workflow SQL, dbt, loaders, emails or tasks. Reads back only DDL
objects changed by this migration; configuration contents are not written to disk.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow_commands import validate_definition
from migration_support import ROOT, connect, procedure_parts
from snowflake.connector import DictCursor


APP = "KUMO_DEV.MONITOR_APP"
META = "KUMO_DEV.MONITOR_META"
RESOLVER = f"{APP}.SP_RESOLVE_WORKFLOW_COMMAND(VARIANT,VARCHAR)"
FIELDS = ("SQL_COMMAND", "DBT_COMMAND", "DBT_PROJECT_FQN", "DBT_TARGET", "DBT_WORKSPACE")
BEFORE_HASHES = {'1': 'd1d17e15539ed5473c2e2e7c8bb9fba67f47cf0d93a8d67540f89ee813e741ac', '2': '1eba56e821c43a6ec68a408d0dbacc1a6138e0fc251bf132aceafb5268b20325'}


def logical_body(ddl):
    body = procedure_parts(ddl)[1]
    return body.replace("\\\\", "\\") if re.search(r"\bAS\s+'", ddl, re.I) else body


def definition_only(ddl):
    return ddl[re.search(r"CREATE\s+OR\s+REPLACE", ddl, re.I).start():].strip().rstrip(";")


def template_value(field, value):
    if value is None:
        return None
    value = value.replace("#[run_id]#", "[run_id]")
    if field == "DBT_TARGET" and value == "kumo_DEV":
        return "[DBT_TARGET]"
    value = re.sub(r"(?i)(--target(?:=|\s+))kumo_DEV\b", r"\1[DBT_TARGET]", value)
    value = re.sub(r"(?i)\bKUMO_DEV\.DBT_APP\.KUMO_DEV\b", "[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]", value)
    for schema, variable in [("MONITOR_APP", "MONITOR_APP_SCHEMA"), ("MONITOR_META", "META_SCHEMA"),
                             ("META", "APP_SCHEMA"), ("RAW_LIM", "RAW_SCHEMA"), ("DBT_APP", "DBT_SCHEMA")]:
        value = re.sub(r'(?i)(?:"KUMO_DEV"|\bKUMO_DEV)\s*\.\s*(?:"' + schema + r'"|\b' + schema + r'\b)',
                       "[DATABASE].[" + variable + "]", value)
    return re.sub(r"(?i)\bKUMO_DEV\b", "[DATABASE]", value)


def configuration_plan(cursor):
    plans = []
    for table, primary_key, kind in [("WORKFLOWS", "WORKFLOW_ID", "WORKFLOW_TYPE"), ("JOB", "JOB_ID", "JOB_TYPE")]:
        fields = list(FIELDS)
        if table == "JOB":
            fields += ["COMMAND_LINE", "ON_SUCCESS_COMMAND_LINE", "ON_WARNING_COMMAND_LINE", "ON_ERROR_COMMAND_LINE"]
        cursor.execute(f"SELECT {primary_key}, {kind}, {', '.join(fields)} FROM {META}.{table}")
        for row in cursor.fetchall():
            updated = {field: template_value(field, row[field]) for field in fields}
            validate_definition({"sqlCommand": updated["SQL_COMMAND"], "dbtCommand": updated["DBT_COMMAND"],
                                 "dbtProjectFqn": updated["DBT_PROJECT_FQN"], "dbtTarget": updated["DBT_TARGET"]})
            changed = {field: value for field, value in updated.items() if value != row[field]}
            if changed:
                plans.append((table, primary_key, row, changed))
    return plans


def run(apply=False):
    changed_objects = []
    try:
        with connect() as conn, conn.cursor(DictCursor) as cursor:
            cursor.execute("ALTER SESSION SET QUERY_TAG = 'KUMO_WORKFLOW_COMMAND_TEMPLATES'")
            folder = ROOT / "snowflake/ddl/KUMO_DEV/MONITOR_APP/procedures"
            changes = []
            resolver_path = next(folder.glob("SP_RESOLVE_WORKFLOW_COMMAND__*.sql"))
            resolver_sql = definition_only(resolver_path.read_text())
            cursor.execute(f"SHOW PROCEDURES LIKE 'SP_RESOLVE_WORKFLOW_COMMAND' IN SCHEMA {APP}")
            if cursor.fetchall():
                cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (RESOLVER,))
                existing = cursor.fetchone()["DDL"]
                if logical_body(existing).strip() != logical_body(resolver_sql).strip():
                    raise ValueError("The existing command resolver differs; review it before replacement")
            else:
                changes.append((RESOLVER, resolver_sql))
            for path in sorted(folder.glob("SP_WORKFLOW_DISPATCH__*.sql")):
                desired = definition_only(path.read_text())
                count = 2 if '"P_RUN_ID"' in desired.split("RETURNS", 1)[0] else 1
                signature = f"{APP}.SP_WORKFLOW_DISPATCH(" + ",".join(["VARCHAR"] * count) + ")"
                cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
                existing = cursor.fetchone()["DDL"]
                if logical_body(existing).strip() == logical_body(desired).strip():
                    continue
                if hashlib.sha256(logical_body(existing).encode()).hexdigest() != BEFORE_HASHES[str(count)]:
                    raise ValueError("Dispatcher changed since preparation: " + signature)
                changes.append((signature, desired.replace("\nRETURNS ", "\nCOPY GRANTS\nRETURNS ", 1)))
            plans = configuration_plan(cursor)
            if not apply:
                return {"applied": False, "ddl_objects_to_update": [item[0] for item in changes],
                        "configuration_rows_to_update": len(plans)}
            for signature, sql in changes:
                cursor.execute(sql)
                changed_objects.append(signature)
                cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (signature,))
                if logical_body(cursor.fetchone()["DDL"]).strip() != logical_body(sql).strip():
                    raise RuntimeError("Deployed procedure body differs: " + signature)
            # Validate each distinct converted SQL/dbt definition before any row
            # changes. The resolver only reads parameters; returned SQL is never run.
            verified = set()
            for table, key, original, updates in plans:
                row = {**original, **updates}
                kind = row.get("WORKFLOW_TYPE") if table == "WORKFLOWS" else row.get("JOB_TYPE")
                if str(kind or "DBT").upper() not in ("SQL", "DBT"):
                    continue
                definition = {"workflowType": kind or "DBT", "sqlCommand": row["SQL_COMMAND"],
                              "dbtCommand": row["DBT_COMMAND"], "dbtProjectFqn": row["DBT_PROJECT_FQN"],
                              "dbtTarget": row["DBT_TARGET"]}
                encoded = json.dumps(definition, sort_keys=True)
                if encoded in verified:
                    continue
                cursor.execute(f"CALL {APP}.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(%s), NULL)", (encoded,))
                result = next(iter(cursor.fetchone().values()))
                if isinstance(result, str):
                    result = json.loads(result)
                if not result.get("ok"):
                    raise RuntimeError("Definition resolution failed before conversion: " + result.get("error", "unknown"))
                verified.add(encoded)
            print("Validated converted definitions:", len(verified), flush=True)
            cursor.execute("BEGIN TRANSACTION")
            try:
                for table, key, original, updates in plans:
                    sets, conditions, binds = [], [f"{key} = %(id)s"], {"id": original[key]}
                    for field, value in updates.items():
                        sets.append(f"{field} = %({field})s")
                        conditions.append(f"{field} IS NOT DISTINCT FROM %(old_{field})s")
                        binds[field], binds["old_" + field] = value, original[field]
                    cursor.execute(f"UPDATE {META}.{table} SET {', '.join(sets)}, UPDATED_AT = SYSDATE(), UPDATED_BY = CURRENT_USER() WHERE {' AND '.join(conditions)}", binds)
                    if cursor.rowcount != 1:
                        raise RuntimeError("A definition changed during migration; configuration transaction rolled back")
                if configuration_plan(cursor):
                    raise RuntimeError("Definition conversion was incomplete")
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
            # Both calls resolve text only; neither executes the returned command.
            for definition in [{"workflowType": "SQL", "sqlCommand": "SELECT '[DATABASE]' AS DB, '[run_id]' AS RUN_ID"},
                               {"workflowType": "DBT", "dbtCommand": "build --select tag:daily --vars '{\"probe\":true}' --target [DBT_TARGET]"}]:
                cursor.execute(f"CALL {APP}.SP_RESOLVE_WORKFLOW_COMMAND(PARSE_JSON(%s), NULL)", (json.dumps(definition),))
                result = next(iter(cursor.fetchone().values()))
                if isinstance(result, str):
                    result = json.loads(result)
                if not result.get("ok"):
                    raise RuntimeError("Command resolution check failed: " + result.get("error", "unknown"))
            return {"applied": True, "ddl_objects_updated": changed_objects,
                    "configuration_rows_updated": len(plans), "resolution_verified": True,
                    "distinct_definitions_validated": len(verified),
                    "workflow_commands_executed": False}
    finally:
        if changed_objects:
            command = [sys.executable, str(ROOT / "snowflake/export_ddl.py")]
            for signature in changed_objects:
                command += ["--object", "PROCEDURE", signature]
            subprocess.run(command, check=True, cwd=ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(run(parser.parse_args().apply), indent=2))
