"""Prepare/create the ten workflow procedure definitions in DEV only.

All external data targets are required arguments. Building SQL is offline;
--apply creates new procedures, never calls them, and refreshes the DDL export.
Existing DEV procedures are never replaced. No tables or tasks are created.
"""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "KUMO_ADMIN.WORKFLOW_MANAGER"
APP = "KUMO_DEV.MONITOR_APP"
META = "KUMO_DEV.MONITOR_META"
PROCEDURES = {
    "SP_WORKFLOW_REQUEST_RUN", "SP_RUN_WORKFLOW", "SP_WORKFLOW_DISPATCH",
    "SP_DBT_COMPLETE", "SP_SQL_COMPLETE", "SP_SEND_NOTIFICATION",
    "SP_SEND_WORKFLOW_EMAIL", "SP_RULE_BASED_WORKFLOW_REQUEST_RUN",
}
TABLES = {
    "WORKFLOWS", "WORKFLOW_TASKS", "WORKFLOW_HISTORY", "WORKFLOW_RUN_QUEUE",
    "WORKFLOW_RUN_LOGS", "APP_WORKFLOW_RUN_LOCKS", "WORKFLOW_TMP_TASKS",
    "WORKFLOW_NOTIFICATIONS", "EMAIL_GROUPS",
}


def identifier(value, parts):
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_$]*(?:\.[A-Z_][A-Z0-9_$]*){" + str(parts - 1) + "}", value):
        raise ValueError(f"Expected an unquoted {parts}-part identifier: {value}")
    return value


@dataclass(frozen=True)
class Targets:
    workflow_status_table: str
    model_status_table: str
    execution_namespace: str
    execution_warehouse: str

    def __post_init__(self):
        for name, parts in (("workflow_status_table", 3), ("model_status_table", 3),
                            ("execution_namespace", 2), ("execution_warehouse", 1)):
            object.__setattr__(self, name, identifier(getattr(self, name), parts))
        # Monitoring may read TST, but these new procedures must never target
        # the existing monitor's own tables or executable namespace.
        if any(getattr(self, name).startswith("KUMO_ADMIN.") for name in
               ("workflow_status_table", "model_status_table", "execution_namespace")):
            raise ValueError("DEV data targets cannot point to KUMO_ADMIN")


def definition_only(ddl):
    match = re.search(r"CREATE OR REPLACE PROCEDURE\b", ddl, re.I)
    if not match:
        raise ValueError("Expected a complete CREATE OR REPLACE PROCEDURE definition")
    return ddl[match.start():].strip().rstrip(";").rstrip()


def replace_exact(ddl, old, new, count=1):
    if ddl.count(old) != count:
        raise ValueError(f"Source definition drift: expected {count} occurrences of {old}")
    return ddl.replace(old, new)


def transform(ddl, targets):
    ddl = definition_only(ddl)
    if "SP_RULE_BASED_WORKFLOW_REQUEST_RUN(\"" in ddl.splitlines()[0]:
        ddl = replace_exact(ddl, "let monitorSchema := ''KUMO_ADMIN.WORKFLOW_MANAGER'';",
                            f"let monitorSchema := ''{META}'';\nlet appSchema := ''{APP}'';")
        ddl = replace_exact(ddl, "let metaSchema := ''META'';",
                            f"let ruleTable := ''{META}.WORKFLOW_DEP_RULE'';\n"
                            f"let workflowStatusTable := ''{targets.workflow_status_table}'';\n"
                            f"let modelStatusTable := ''{targets.model_status_table}'';")
        ddl = replace_exact(ddl, "procName varchar := monitorSchema", "procName varchar := appSchema")
        ddl = replace_exact(ddl, "'' || metaSchema || ''.workflow_dep_rule", "'' || ruleTable || ''")
        ddl = replace_exact(ddl, "'' || metaSchema || ''.workflow_status", "'' || workflowStatusTable || ''", count=2)
        ddl = replace_exact(ddl, "'' || metaSchema || ''.model_status", "'' || modelStatusTable || ''")

    def internal_reference(match):
        name = match.group(1)
        if name in PROCEDURES or name == "TMP_WF_":
            return APP + "." + name
        if name in TABLES:
            return META + "." + name
        raise ValueError("Unmapped monitor reference: " + match.group(0))

    ddl = re.sub(re.escape(SOURCE) + r"\.([A-Z0-9_]+)", internal_reference, ddl)
    ddl = ddl.replace("KUMO_TST.META.WORKFLOW_STATUS", targets.workflow_status_table)
    database, schema = targets.execution_namespace.split(".")
    ddl = ddl.replace("USE DATABASE KUMO_TST; USE SCHEMA RAW_LIM;",
                      f"USE DATABASE {database}; USE SCHEMA {schema};")
    ddl = ddl.replace("WAREHOUSE = KUMO_ELT_GEN_1", "WAREHOUSE = " + targets.execution_warehouse)
    if SOURCE in ddl or "metaSchema" in ddl:
        raise ValueError("An implicit or legacy monitor namespace remains")
    # Create-only DDL deliberately refuses to overwrite an existing object.
    return ddl.replace("CREATE OR REPLACE PROCEDURE", "CREATE PROCEDURE", 1) + ";\n"


def build(targets):
    manifest = json.loads((ROOT / "snowflake/manifest.json").read_text())
    result = []
    for item in manifest["objects"]:
        if item["type"] != "PROCEDURE" or not item["name"].startswith(SOURCE + "."):
            continue
        if item["name"].split(".")[-1] not in PROCEDURES:
            continue
        source = (ROOT / "snowflake" / item["file"]).read_text()
        result.append({"source": item["name"] + item["signature"],
                       "target": item["name"].replace(SOURCE, APP, 1) + item["signature"],
                       "original": definition_only(source), "sql": transform(source, targets)})
    if len(result) != 10:
        raise ValueError(f"Expected 10 source definitions, found {len(result)}")
    return result


def connect():
    from dotenv import load_dotenv
    import snowflake.connector
    load_dotenv(ROOT / ".env")
    settings = {key: os.environ["SNOWFLAKE_" + key.upper()] for key in ("account", "user", "password")}
    settings.update(login_timeout=20, network_timeout=90,
                    session_parameters={"QUERY_TAG": "KUMO_CREATE_DEV_MONITOR_PROCEDURES", "STATEMENT_TIMEOUT_IN_SECONDS": 120})
    for key in ("role", "warehouse", "passcode"):
        if os.getenv("SNOWFLAKE_" + key.upper()):
            settings[key] = os.environ["SNOWFLAKE_" + key.upper()]
    settings["passcode_in_password"] = os.getenv("SNOWFLAKE_PASSCODE_IN_PASSWORD", "false").lower() == "true"
    return snowflake.connector.connect(**settings)


def apply(definitions):
    from snowflake.connector import DictCursor
    created = []
    try:
        with connect() as connection, connection.cursor(DictCursor) as cursor:
            # Validate every source and destination before the first CREATE.
            cursor.execute("SHOW PROCEDURES IN SCHEMA " + APP)
            if any(row["name"] in PROCEDURES for row in cursor.fetchall() if row.get("schema_name")):
                raise RuntimeError("A requested DEV procedure already exists; no definitions were replaced")
            cursor.execute("SHOW TABLES IN SCHEMA " + META)
            cursor.fetchall()  # Verify that the existing metadata namespace is accessible.
            for definition in definitions:
                cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (definition["source"],))
                if definition_only(cursor.fetchone()["DDL"]) != definition["original"]:
                    raise RuntimeError("Source has drifted; refresh the DDL snapshot first: " + definition["source"])
            for definition in definitions:
                cursor.execute(definition["sql"])
                created.append(definition["target"])
                print("Created:", definition["target"], flush=True)
                cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (definition["target"],))
                expected = definition["sql"].replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1)
                if definition_only(cursor.fetchone()["DDL"]) != definition_only(expected):
                    raise RuntimeError("Created definition differs from the prepared definition: " + definition["target"])
    finally:
        if created:
            # Capture even a partial deployment; never remove created objects.
            scope_path = ROOT / "snowflake/export_scope.json"
            scope = json.loads(scope_path.read_text())
            for namespace in (APP, META):
                if namespace not in scope["schemas"]:
                    scope["schemas"].append(namespace)
            scope_path.write_text(json.dumps(scope, indent=2) + "\n")
            subprocess.run([sys.executable, str(ROOT / "snowflake/export_ddl.py")], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-status-table", required=True, help="Status target used for both reads and writes")
    parser.add_argument("--model-status-table", required=True, help="Existing dbt status table, read only")
    parser.add_argument("--execution-namespace", required=True, help="database.schema for generated task execution")
    parser.add_argument("--execution-warehouse", required=True)
    parser.add_argument("--build-sql", type=Path, help="Write a review bundle without connecting")
    parser.add_argument("--apply", action="store_true", help="Create new DEV procedures and refresh saved DDL")
    args = parser.parse_args()
    targets = Targets(args.workflow_status_table, args.model_status_table,
                      args.execution_namespace, args.execution_warehouse)
    definitions = build(targets)
    if args.build_sql:
        args.build_sql.write_text("-- DEV procedure definitions only. Do not CALL during migration.\n"
                                  + "\n".join(definition["sql"] for definition in definitions))
        print("Prepared:", args.build_sql)
    if args.apply:
        apply(definitions)
    else:
        print("Prepared 10 definitions; no Snowflake objects were changed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = str(error)
        for key in ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PASSCODE"):
            secret = os.getenv(key)
            if secret:
                message = message.replace(secret, "[redacted]")
        print(message, file=sys.stderr)
        sys.exit(1)
