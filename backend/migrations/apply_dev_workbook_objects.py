"""Create the missing DEV objects listed in the reviewed workbook plan.

Reads source definitions only; never copies table rows or invokes procedures.
Existing DEV objects are skipped. The master dispatcher is created suspended.
All persistent mutations are restricted to KUMO_DEV; external stage parameters
are copied with CLONE so no cloud credentials enter a migration or snapshot.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
PLAN_FILE = Path(__file__).with_name("dev_workbook_object_plan.json")
SQL_FILE = Path(__file__).with_name("create_dev_workbook_objects.sql")
SOURCE_NAMESPACE = "KUMO_ADMIN.WORKFLOW_MANAGER"
APP = "MONITOR_APP"
META = "MONITOR_META"
SHOW = {"TABLE": "TABLES", "PROCEDURE": "PROCEDURES", "STREAM": "STREAMS",
        "TASK": "TASKS", "STAGE": "STAGES"}


def load_plan():
    plan = json.loads(PLAN_FILE.read_text())
    if len(plan["objects"]) != 34:
        raise ValueError("The reviewed plan must contain exactly 34 objects")
    for item in plan["objects"]:
        if not re.fullmatch(r"KUMO_DEV\.(?:MONITOR_APP|MONITOR_META|META)\.[A-Z_][A-Z0-9_$]*", item["target"]):
            raise ValueError("Target outside the reviewed DEV namespaces")
        if item["type"] not in SHOW:
            raise ValueError("Unsupported object type")
    return plan


def connect():
    from dotenv import load_dotenv
    import snowflake.connector
    load_dotenv(ROOT / ".env")
    settings = {key: os.environ["SNOWFLAKE_" + key.upper()] for key in ("account", "user", "password")}
    settings.update(login_timeout=20, network_timeout=90,
                    session_parameters={"QUERY_TAG": "KUMO_DEV_REVIEWED_WORKBOOK_MIGRATION", "STATEMENT_TIMEOUT_IN_SECONDS": 180})
    for key in ("role", "warehouse", "passcode"):
        if os.getenv("SNOWFLAKE_" + key.upper()):
            settings[key] = os.environ["SNOWFLAKE_" + key.upper()]
    return snowflake.connector.connect(**settings)


def ddl_only(ddl):
    match = re.search(r"\bCREATE\s+OR\s+REPLACE\b", ddl, re.I)
    if not match:
        raise ValueError("Expected a complete exported definition")
    return ddl[match.start():].strip().rstrip(";").rstrip()


def procedure_parts(ddl):
    ddl = ddl_only(ddl)
    match = re.search(r"\bAS\s+(\$\$|'|\$[A-Za-z_][A-Za-z0-9_]*\$)", ddl, re.I)
    if not match:
        raise ValueError("Procedure body delimiter was not found")
    delimiter = match.group(1)
    tail = ddl[match.end():].rstrip()
    if not tail.endswith(delimiter):
        raise ValueError("Procedure body is not terminated")
    body = tail[:-len(delimiter)]
    if delimiter == "'":
        body = body.replace("''", "'")
    return ddl[:match.start()].rstrip(), body


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Source definition drift around: " + old)
    return text.replace(old, new, 1)


def local_references(body, plan):
    # Longest names first avoids replacing WORKFLOW before WORKFLOW_JOB.
    mappings = [(item["source"], ".".join(item["target"].split(".")[1:]))
                for item in plan["objects"] if item["type"] != "STAGE"]
    mappings.append(("KUMO_TST.META.WORKFLOW_STATUS", "META.WORKFLOW_STATUS"))
    for source, target in sorted(mappings, key=lambda pair: -len(pair[0])):
        body = re.sub(re.escape(source) + r"(?![A-Za-z0-9_$])", lambda _: target, body)
    return body


def caller_database_validation(variable):
    return (
        f"  IF ({variable} IS NULL OR NOT REGEXP_LIKE({variable}, '^[A-Z_][A-Z0-9_$]*$')) THEN\n"
        "    RAISE E_INVALID_DATABASE;\n"
        "  END IF;\n"
    )


def render_procedure(item, source, plan):
    header, body = procedure_parts(source)
    header = replace_once(header, item["source"], item["target"])
    header = replace_once(header, "CREATE OR REPLACE PROCEDURE", "CREATE PROCEDURE")
    name = item["source"].rsplit(".", 1)[1]

    if name in ("SP_RULE_BASED_WORKFLOW_REQUEST_RUN", "SP_RAW_LIM_META_REFRESH"):
        # Preserve caller rights. Its session database cannot identify where
        # the procedure was installed, so make the database an explicit input.
        line, rest = header.split("\n", 1)
        end = line.rfind(")")
        line = line[:end] + ', "P_DATABASE" VARCHAR DEFAULT \'KUMO_DEV\'' + line[end:]
        header = line + "\n" + rest
        if "EXECUTE AS CALLER" not in header:
            raise ValueError("Expected caller rights for " + name)

    if name == "SP_RULE_BASED_WORKFLOW_REQUEST_RUN":
        body = re.sub(
            r"^\s*begin\b",
            "DECLARE\n  E_INVALID_DATABASE EXCEPTION (-20002, 'Invalid database identifier');\n"
            "BEGIN\n  LET V_DATABASE VARCHAR := UPPER(TRIM(P_DATABASE));\n"
            + caller_database_validation("V_DATABASE"), body, count=1, flags=re.I,
        )
        body = replace_once(body, "let monitorSchema := 'KUMO_ADMIN.WORKFLOW_MANAGER';",
                            "let monitorSchema := V_DATABASE || '.MONITOR_META';\n"
                            "let appSchema := V_DATABASE || '.MONITOR_APP';")
        body = replace_once(body, "let metaSchema := 'META';", "let metaSchema := V_DATABASE || '.META';")
        body = replace_once(body, "procName varchar := monitorSchema", "procName varchar := appSchema")

    if name == "SP_RAW_LIM_META_REFRESH":
        body = replace_once(body, "DECLARE\n", "DECLARE\n"
                            "    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));\n"
                            "    V_METADATA_TABLE VARCHAR;\n"
                            "    V_SOURCE_TABLE VARCHAR;\n"
                            "    E_INVALID_DATABASE EXCEPTION (-20002, 'Invalid database identifier');\n")
        body = replace_once(body, "BEGIN\n", "BEGIN\n" + caller_database_validation("V_DATABASE")
                            + "    V_METADATA_TABLE := V_DATABASE || '.MONITOR_META.RAW_LIM_META';\n"
                            "    V_SOURCE_TABLE := V_DATABASE || '.MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE';\n")
        body = body.replace("'KUMO_TST.RAW_LIM.RAW_LIM_'", ":V_DATABASE || '.RAW_LIM.RAW_LIM_'")
        body = body.replace("KUMO_TST.RAW_LIM.RAW_LIM_META", "IDENTIFIER(:V_METADATA_TABLE)")
        body = body.replace(SOURCE_NAMESPACE + ".RAW_LIM_PKG_GROUP_SOURCE", "IDENTIFIER(:V_SOURCE_TABLE)")

    if name == "SP_RUN_WORKFLOW":
        body = replace_once(body, "DECLARE\n", "DECLARE\n  V_DATABASE VARCHAR DEFAULT CURRENT_DATABASE();\n")
        body = replace_once(body, "'KUMO_ADMIN.WORKFLOW_MANAGER.TMP_WF_'",
                            ":V_DATABASE || '.MONITOR_APP.TMP_WF_'")
        old = "'   USE DATABASE KUMO_TST; USE SCHEMA RAW_LIM;'"
        if body.count(old) != 2:
            raise ValueError("Expected two generated task execution contexts")
        body = body.replace(old, "'   USE DATABASE \"' || :V_DATABASE || '\"; USE SCHEMA RAW_LIM;'")

    body = local_references(body, plan)
    if "KUMO_TST" in body or "KUMO_ADMIN" in body:
        raise ValueError("A legacy database reference remains in " + name)
    # GET_DDL escapes backslashes inside its quoted body. Preserve that SQL
    # representation when changing names; converting to $$ would interpret
    # those escapes differently (including JavaScript regular expressions).
    return header + "\nAS '" + body.replace("'", "''") + "';\n"


def render(item, source, plan):
    if item["type"] == "STAGE":
        return "CREATE STAGE " + item["target"] + " CLONE " + item["source"] + ";\n"
    if item["type"] == "PROCEDURE":
        return render_procedure(item, source, plan)
    ddl = ddl_only(source)
    ddl = replace_once(ddl, item["source"], item["target"])
    # Stored FK and stream references use explicit DEV creation destinations.
    for other in sorted(plan["objects"], key=lambda obj: -len(obj["source"])):
        if other["type"] == "TABLE":
            ddl = re.sub(re.escape(other["source"]) + r"(?![A-Za-z0-9_$])", lambda _: other["target"], ddl)
    ddl = re.sub(r"^CREATE\s+OR\s+REPLACE\s+", "CREATE ", ddl, count=1, flags=re.I)
    if item["type"] == "TABLE" and item["target"].endswith(".WORKFLOW_NOTIFICATIONS"):
        ddl = re.sub(r"(\bENVIRONMENT\s+VARCHAR\([^)]*\)\s+DEFAULT\s+)'PROD'", r"\1'DEV'", ddl, flags=re.I)
    if item["type"] == "STREAM":
        ddl = re.sub(r"\bon\s+table\s+(?:KUMO_DEV\.MONITOR_META\.)?WORKFLOW_RUN_QUEUE\b",
                     "on table KUMO_DEV.MONITOR_META.WORKFLOW_RUN_QUEUE", ddl, flags=re.I)
    if item["type"] == "TASK":
        start = ddl.lower().index("\n\tas ") if "\n\tas " in ddl.lower() else ddl.lower().index("\nas ")
        prefix, body = ddl[:start], ddl[start:]
        prefix = prefix.replace(SOURCE_NAMESPACE + ".WORKFLOW_EXECUTION_QUEUE_STREAM",
                                "KUMO_DEV.MONITOR_META.WORKFLOW_EXECUTION_QUEUE_STREAM")
        body = local_references(body, plan)
        ddl = prefix + body
    if "KUMO_TST" in ddl or "KUMO_ADMIN" in ddl:
        raise ValueError("Legacy database reference in " + item["target"])
    return ddl + ";\n"


def source_definitions(cursor, plan):
    definitions = {}
    for item in plan["objects"]:
        if item["type"] == "STAGE":
            continue
        identifier = item["source"] + item["signature"]
        cursor.execute("SELECT GET_DDL(%s, %s, TRUE) AS DDL", (item["type"], identifier))
        value = cursor.fetchone()["DDL"]
        if not value or "#UNKNOWN_" in value:
            raise RuntimeError("Incomplete source definition: " + identifier)
        for key in ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PASSCODE"):
            if os.getenv(key) and os.environ[key] in value:
                raise RuntimeError("A connection secret appears in a source definition")
        definitions[item["source"] + item["signature"]] = value
    return definitions


def existing_objects(cursor):
    result = {}
    for kind, show in SHOW.items():
        cursor.execute("SHOW " + show + " IN DATABASE KUMO_DEV")
        rows = [dict(row) for row in cursor.fetchall() if kind != "PROCEDURE" or row.get("schema_name")]
        if len(rows) >= 10000:
            raise RuntimeError("Inventory may be truncated")
        result[kind] = rows
    return result


def select_missing(plan, inventory):
    missing, skipped = [], []
    for item in plan["objects"]:
        schema, name = item["target"].split(".")[1:]
        # The workbook asks us to avoid duplicating an object anywhere in DEV.
        matches = [row for row in inventory[item["type"]] if row["name"] == name]
        if matches:
            if any(row["schema_name"] != schema for row in matches):
                raise RuntimeError("An object with this name already exists in another DEV schema: " + name)
            if item["type"] == "PROCEDURE":
                # Skip individual existing overloads, allowing other planned overloads.
                from sys import path
                if str(ROOT / "snowflake") not in path:
                    path.insert(0, str(ROOT / "snowflake"))
                from export_ddl import signature
                matches = [row for row in matches if signature(row["arguments"]) == item["target_signature"]]
            if matches:
                skipped.append(item)
                continue
        missing.append(item)
    return missing, skipped


def save_bundle(plan, definitions):
    statements = [(item, render(item, definitions.get(item["source"] + item["signature"]), plan))
                  for item in plan["objects"]]
    SQL_FILE.write_text("-- Reviewed workbook: " + plan["workbook_name"] + "\n"
                        "-- Definitions only. Run through apply_dev_workbook_objects.py to skip existing DEV objects.\n"
                        "-- No table rows, CALL statements, task RESUME or task EXECUTE statements are run by the migration.\n"
                        "USE DATABASE KUMO_DEV;\n\n"
                        + "\n".join("-- " + item["type"] + " " + item["target"] + "\n" + sql for item, sql in statements))
    return statements


def refresh_export():
    path = ROOT / "snowflake/export_scope.json"
    scope = json.loads(path.read_text())
    for schema in ("KUMO_DEV.MONITOR_APP", "KUMO_DEV.MONITOR_META", "KUMO_DEV.META"):
        if schema not in scope["schemas"]:
            scope["schemas"].append(schema)
    dispatcher = "KUMO_DEV.MONITOR_APP.TASK_WF_MASTER_DISPATCHER"
    if dispatcher not in scope["keep_tasks"]:
        scope["keep_tasks"].append(dispatcher)
    path.write_text(json.dumps(scope, indent=2) + "\n")
    subprocess.run([sys.executable, str(ROOT / "snowflake/export_ddl.py")], check=True)


def run(apply=False):
    from snowflake.connector import DictCursor
    plan = load_plan()
    created = []
    try:
        with connect() as connection, connection.cursor(DictCursor) as cursor:
            definitions = source_definitions(cursor, plan)
            # Keep fresh source DDL outside Git for offline validation and recovery.
            Path("/tmp/kumo-dev-workbook-sources.json").write_text(json.dumps(definitions, indent=2))
            statements = save_bundle(plan, definitions)
            missing, skipped = select_missing(plan, existing_objects(cursor))
            print("Missing:", dict(Counter(item["type"] for item in missing)), flush=True)
            print("Skipped existing:", len(skipped), flush=True)
            if not apply:
                print("Prepared SQL only; no Snowflake objects changed.", flush=True)
                return
            expected = {item["target"] + item["target_signature"] for item in missing}
            cursor.execute("USE DATABASE KUMO_DEV")
            for item, sql in statements:
                key = item["target"] + item["target_signature"]
                if key not in expected:
                    continue
                # Recheck immediately before each create; no replacement DDL.
                now_missing, _ = select_missing({"objects": [item]}, existing_objects(cursor))
                if not now_missing:
                    print("Skipped concurrently created:", key, flush=True)
                    continue
                cursor.execute(sql)
                created.append(item)
                print("Created:", item["type"], key, flush=True)
                if item["type"] == "PROCEDURE":
                    cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (key,))
                    actual = cursor.fetchone()["DDL"]
                    _, actual_body = procedure_parts(actual)
                    prepared = sql.replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1)
                    _, expected_body = procedure_parts(prepared)
                    if actual_body.strip() != expected_body.strip():
                        raise RuntimeError("Created procedure body differs: " + key)
            cursor.execute("SHOW TASKS IN SCHEMA KUMO_DEV.MONITOR_APP")
            tasks = cursor.fetchall()
            for task in tasks:
                if task["name"] == "TASK_WF_MASTER_DISPATCHER" and task["state"].upper() != "SUSPENDED":
                    raise RuntimeError("Dispatcher is unexpectedly active")
            receipt = {"workbook_sha256": plan["workbook_sha256"],
                       "created": [item["target"] + item["target_signature"] for item in created],
                       "skipped": [item["target"] + item["target_signature"] for item in skipped],
                       "tasks": [{key: task[key] for key in ("name", "state")} for task in tasks]}
            (ROOT / "review/dev-workbook-migration-result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    finally:
        if created:
            refresh_export()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    run(parser.parse_args().apply)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = str(error)
        for key in ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PASSCODE"):
            if os.getenv(key):
                message = message.replace(os.environ[key], "[redacted]")
        print(type(error).__name__ + ": " + message, file=sys.stderr)
        sys.exit(1)
