"""Copy reviewed workflow configuration into DEV and add its support definitions.

Configuration rows stay in memory and are bound as data, never executed or
written to files. Existing DEV rows/objects are preserved. No workflow,
loader, task or email is executed by this migration.
"""

import argparse
import json
from pathlib import Path
import re
import sys

from apply_dev_workbook_objects import (
    ROOT, connect, ddl_only, load_plan, procedure_parts, refresh_export,
    replace_once, select_missing,
)
from dev_workflow_support import OBJECTS, render_procedure


META = "KUMO_DEV.MONITOR_META."
PROJECT = "KUMO_DEV.DBT_APP.KUMO_DEV"
SQL_FILE = Path(__file__).with_name("create_dev_workflow_support.sql")
RECEIPT = ROOT / "review/dev-workflow-configuration-result.json"
TABLES = [
    ("EMAIL_GROUPS", "KUMO_ADMIN.WORKFLOW_MANAGER", ("GROUP_NAME",), ()),
    ("RAW_LIM_PKG_GROUP_SOURCE", "KUMO_ADMIN.WORKFLOW_MANAGER", ("PKG_GROUP_NAME", "SUBJECT_AREA", "SOURCE_ID", "PKG_ID"), ()),
    ("APPLICATION_PARAMETERS", "KUMO_ADMIN.WORKFLOW_MANAGER", ("INSTANCE_NAME", "PARAMETER_GROUP", "PARAMETER_KEY"), ()),
    ("WORKFLOWS", "KUMO_ADMIN.WORKFLOW_MANAGER", ("WORKFLOW_ID",), (("WORKFLOW_GROUP", "WORKFLOW_NAME"),)),
    ("WORKFLOW_TASKS", "KUMO_ADMIN.WORKFLOW_MANAGER", ("WORKFLOW_ID",), ()),
    ("WORKFLOW_NOTIFICATIONS", "KUMO_ADMIN.WORKFLOW_MANAGER", ("WORKFLOW_ID",), ()),
    ("JOB", "KUMO_TST.MONITOR_APP", ("JOB_ID",), (("JOB_NAME",),)),
    ("WORKFLOW", "KUMO_TST.MONITOR_APP", ("WORKFLOW_ID",), (("WORKFLOW_NAME",),)),
    ("WORKFLOW_JOB", "KUMO_TST.MONITOR_APP", ("WORKFLOW_JOB_ID",), (("WORKFLOW_ID", "JOB_ID"), ("WORKFLOW_ID", "JOB_SEQUENCE"))),
    ("WORKFLOW_DEPENDENCY", "KUMO_TST.MONITOR_APP", ("WORKFLOW_DEPENDENCY_ID",), (("PARENT_WORKFLOW_ID", "CHILD_WORKFLOW_ID"),)),
]
EXECUTION_FIELDS = {
    "SQL_COMMAND", "COMMAND_LINE", "DBT_COMMAND", "DBT_PROJECT_FQN", "DBT_WORKSPACE",
    "ON_SUCCESS_COMMAND_LINE", "ON_WARNING_COMMAND_LINE", "ON_ERROR_COMMAND_LINE",
}


def reference_map():
    result = {item["source"]: item["target"] for item in load_plan()["objects"]}
    result.update({item["source"]: item["target"] for item in OBJECTS})
    result.update({
        "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE": META + "LOAD_RAW_LIM_FROM_STAGE",
        "KUMO_TST.RAW_LIM.RAW_LOAD_RESULTS": META + "RAW_LOAD_RESULTS",
        "KUMO_TST.DBT.KUMO_TST": PROJECT,
    })
    return result


def rewrite_references(value, dbt_target):
    if value is None:
        return None
    for source, target in sorted(reference_map().items(), key=lambda item: -len(item[0])):
        value = re.sub(r"(?<![A-Za-z0-9_$])" + re.escape(source) + r"(?![A-Za-z0-9_$])", lambda _: target, value, flags=re.I)
    # Honor the existing DEV project's profile target, including its case.
    value = re.sub(r"(?i)(--target(?:=|\s+))kumo_tst\b", lambda m: m.group(1) + dbt_target, value)
    value = re.sub(r"(?i)\bKUMO_TST\b", "KUMO_DEV", value)
    if re.search(r"(?i)\bKUMO_ADMIN\b", value):
        raise ValueError("Unmapped ADMIN reference in an executable configuration field")
    return value


def transform_row(table, row, dbt_target):
    result = dict(row)
    for name in EXECUTION_FIELDS.intersection(result):
        result[name] = rewrite_references(result[name], dbt_target)
    if result.get("DBT_PROJECT_FQN"):
        if result["DBT_PROJECT_FQN"].upper() != PROJECT:
            raise ValueError("Unexpected configured dbt project")
        result["DBT_TARGET"] = dbt_target
    if table == "WORKFLOW_TASKS":
        result.update(IS_ENABLED=False, NEXT_SCHEDULE_TS=None, LAST_SCHEDULE_TS=None)
    if table == "WORKFLOW":
        result.update(IS_SCHEDULE_ENABLED=False, TASK_NAME=None)
    if table == "JOB":
        result["TASK_NAME"] = None
    if table == "WORKFLOW_NOTIFICATIONS":
        result["ENVIRONMENT"] = "DEV"
    if table == "APPLICATION_PARAMETERS":
        if result.get("IS_SECRET"):
            raise ValueError("Secret application parameters require a separate reviewed migration")
        if result["PARAMETER_GROUP"] == "ENVIRONMENT":
            values = {"DATABASE": "KUMO_DEV", "APP_SCHEMA": "MONITOR_APP", "META_SCHEMA": "MONITOR_META"}
            key = result["PARAMETER_KEY"]
            result["PARAMETER_VALUE"] = values.get(key, rewrite_references(result["PARAMETER_VALUE"], dbt_target))
    return result


def normalized(row, variants):
    result = dict(row)
    for name in variants:
        if isinstance(result.get(name), str):
            result[name] = json.loads(result[name])
    return result


def row_key(row, columns):
    return tuple(row[name] for name in columns)


def select_rows_to_insert(table, source, existing, keys, unique_keys, variants):
    by_id = {}
    for row in existing:
        key = row_key(row, keys)
        if key in by_id:
            raise ValueError("Duplicate existing DEV keys in " + table)
        by_id[key] = row
    seen = set()
    unique_values = [set() for _ in unique_keys]
    existing_unique = [{row_key(row, cols): row_key(row, keys) for row in existing} for cols in unique_keys]
    missing = []
    for row in source:
        key = row_key(row, keys)
        if key in seen:
            raise ValueError("Duplicate source configuration keys in " + table)
        seen.add(key)
        for index, cols in enumerate(unique_keys):
            value = row_key(row, cols)
            if value in unique_values[index]:
                raise ValueError("Duplicate source configuration names or relationships in " + table)
            unique_values[index].add(value)
            if value in existing_unique[index] and existing_unique[index][value] != key:
                raise ValueError("A DEV configuration name already belongs to another ID in " + table)
        if key in by_id:
            if normalized(by_id[key], variants) != normalized(row, variants):
                raise ValueError("An existing DEV configuration row differs in " + table + "; it will not be overwritten")
        else:
            missing.append(row)
    return missing


def check_graphs(rows):
    workflow_ids = {r["WORKFLOW_ID"] for r in rows["WORKFLOWS"]}
    for row in rows["WORKFLOW_TASKS"]:
        if row["WORKFLOW_ID"] not in workflow_ids:
            raise ValueError("A source schedule refers to a missing workflow")
        for column in ("ON_SUCCESS", "ON_FAIL"):
            value = row.get(column)
            value = json.loads(value) if isinstance(value, str) else value
            children = value if isinstance(value, list) else [value]
            if any(str(child).strip() not in workflow_ids and str(child).strip().upper() not in ("", "NOTHING", "NONE")
                   for child in children if child is not None):
                raise ValueError("A workflow chain refers to a missing source workflow")
    catalog_ids = {r["WORKFLOW_ID"] for r in rows["WORKFLOW"]}
    job_ids = {r["JOB_ID"] for r in rows["JOB"]}
    for row in rows["WORKFLOW_JOB"]:
        if row["WORKFLOW_ID"] not in catalog_ids or row["JOB_ID"] not in job_ids:
            raise ValueError("A source workflow/job binding is orphaned")
        if row.get("PARENT_JOB_ID") and row["PARENT_JOB_ID"] not in job_ids:
            raise ValueError("A source job parent is missing")
    for row in rows["WORKFLOW_DEPENDENCY"]:
        if row["PARENT_WORKFLOW_ID"] not in catalog_ids or row["CHILD_WORKFLOW_ID"] not in catalog_ids:
            raise ValueError("A source catalog dependency is orphaned")


def configurations(cursor, dbt_target):
    plans = []
    all_rows = {}
    for table, namespace, keys, unique_keys in TABLES:
        source = namespace + "." + table
        target = META + table
        cursor.execute("DESC TABLE " + source)
        source_columns = {row["name"] for row in cursor.fetchall()}
        cursor.execute("DESC TABLE " + target)
        definition = cursor.fetchall()
        columns = [row["name"] for row in definition if row.get("kind") == "COLUMN"]
        variants = {row["name"] for row in definition if row.get("type", "").startswith(("VARIANT", "ARRAY", "OBJECT"))}
        if source_columns != set(columns):
            raise ValueError("Source/DEV column mismatch in " + table)
        cursor.execute("SELECT * FROM " + source)
        original = cursor.fetchall()
        if table == "WORKFLOW_NOTIFICATIONS":
            workflow_ids = {row["WORKFLOW_ID"] for row in all_rows["WORKFLOWS"]}
            selected = [row for row in original if row["WORKFLOW_ID"] in workflow_ids]
        else:
            selected = original
        prepared = [transform_row(table, row, dbt_target) for row in selected]
        all_rows[table] = prepared
        cursor.execute("SELECT * FROM " + target)
        existing = cursor.fetchall()
        missing = select_rows_to_insert(table, prepared, existing, keys, unique_keys, variants)
        plans.append({"table": table, "target": target, "columns": columns, "variants": variants,
                      "keys": keys, "rows": prepared, "missing": missing, "existing": existing,
                      "source_count": len(original), "excluded_orphans": len(original) - len(prepared)})
        print("Configuration:", table, "source", len(original), "selected", len(prepared), "insert", len(missing), flush=True)
    check_graphs(all_rows)
    return plans


def object_inventory(cursor):
    result = {}
    for kind, show in (("TABLE", "TABLES"), ("PROCEDURE", "PROCEDURES")):
        cursor.execute("SHOW " + show + " IN DATABASE KUMO_DEV")
        result[kind] = [row for row in cursor.fetchall() if row.get("schema_name")]
        if len(result[kind]) >= 10000:
            raise ValueError("DEV inventory may be truncated")
    return result


def missing_objects(objects, inventory):
    missing, skipped = select_missing({"objects": objects}, inventory)
    for item in objects:
        if item["type"] != "PROCEDURE":
            continue
        name = item["target"].rsplit(".", 1)[1]
        matches = [row for row in inventory["PROCEDURE"] if row["name"] == name]
        if matches and (item in missing or len(matches) != 1):
            raise ValueError("An existing DEV support procedure has another interface: " + name)
    return missing, skipped


def read_definitions(cursor):
    result = {}
    for item in OBJECTS:
        cursor.execute("SELECT GET_DDL(%s, %s, TRUE) AS DDL", (item["type"], item["source"] + item["signature"]))
        ddl = cursor.fetchone()["DDL"]
        if not ddl or "#UNKNOWN_" in ddl:
            raise ValueError("Incomplete source definition")
        result[item["source"]] = ddl
    return result


def statements(definitions):
    result = []
    for item in OBJECTS:
        source = definitions[item["source"]]
        if item["type"] == "PROCEDURE":
            sql = render_procedure(item, source)
        else:
            sql = replace_once(ddl_only(source), item["source"], item["target"])
            sql = re.sub(r"^CREATE\s+OR\s+REPLACE\s+", "CREATE ", sql, count=1, flags=re.I) + ";\n"
        if not re.match(r"^CREATE\s+(?:PROCEDURE|TABLE)\s+KUMO_DEV[.]", sql, re.I):
            raise ValueError("DDL must be create-only in DEV")
        result.append((item, sql))
    return result


def insert_configuration(cursor, plan):
    columns = plan["columns"]
    projection = ", ".join("PARSE_JSON(%s)" if col in plan["variants"] else "%s" for col in columns)
    predicate = " AND ".join(name + " IS NOT DISTINCT FROM %s" for name in plan["keys"])
    sql = ("INSERT INTO " + plan["target"] + " (" + ", ".join(columns) + ") SELECT " + projection
           + " WHERE NOT EXISTS (SELECT 1 FROM " + plan["target"] + " WHERE " + predicate + ")")
    inserted = 0
    for row in plan["missing"]:
        values = []
        for name in columns:
            value = row[name]
            if name in plan["variants"] and value is not None and not isinstance(value, str):
                value = json.dumps(value)
            values.append(value)
        values.extend(row[name] for name in plan["keys"])
        cursor.execute(sql, tuple(values))
        inserted += max(0, cursor.rowcount)
    return inserted


def verify_configuration(cursor, plans):
    for plan in plans:
        cursor.execute("SELECT * FROM " + plan["target"])
        actual = cursor.fetchall()
        indexed = {row_key(row, plan["keys"]): normalized(row, plan["variants"]) for row in actual}
        if len(indexed) != len(actual):
            raise ValueError("Duplicate DEV configuration keys after insert")
        # Verify the rewritten copies and every row that predated the migration.
        for row in plan["rows"] + plan["existing"]:
            if indexed.get(row_key(row, plan["keys"])) != normalized(row, plan["variants"]):
                raise ValueError("DEV configuration verification failed in " + plan["table"])


def assert_tasks_suspended(cursor):
    cursor.execute("SHOW TASKS IN DATABASE KUMO_DEV")
    tasks = [{key: row[key] for key in ("schema_name", "name", "state")} for row in cursor.fetchall()]
    if any(row["state"].upper() != "SUSPENDED" for row in tasks):
        raise ValueError("An existing DEV task is running")
    return tasks


def run(apply=False):
    from snowflake.connector import DictCursor
    created = []
    try:
        with connect() as connection, connection.cursor(DictCursor) as cursor:
            cursor.execute("ALTER SESSION SET QUERY_TAG = 'KUMO_DEV_WORKFLOW_CONFIGURATION_MIGRATION'")
            definitions = read_definitions(cursor)
            prepared = statements(definitions)
            SQL_FILE.write_text("-- Create-only support definitions; use apply_dev_workflow_configuration.py --apply.\n"
                                "-- Configuration data is migrated by bound parameters, never stored in this file.\n"
                                + "\n".join(sql for _, sql in prepared))
            cursor.execute('SHOW DBT PROJECTS IN DATABASE KUMO_DEV')
            projects = [row for row in cursor.fetchall() if row['schema_name'] == 'DBT_APP' and row['name'] == 'KUMO_DEV']
            if len(projects) != 1 or not projects[0].get('default_target'):
                raise ValueError("The DEV dbt project/default target must be present")
            dbt_target = projects[0]['default_target']
            plans = configurations(cursor, dbt_target)
            before_tasks = assert_tasks_suspended(cursor)
            missing, skipped = missing_objects(OBJECTS, object_inventory(cursor))
            for item in missing:
                print("Create:", item["target"] + item["target_signature"], flush=True)
            if not apply:
                print("Prepared and validated only; no Snowflake writes.", flush=True)
                return
            for item, sql in prepared:
                if item not in missing:
                    continue
                now_missing, _ = missing_objects([item], object_inventory(cursor))
                if not now_missing:
                    skipped.append(item)
                    continue
                cursor.execute(sql)
                created.append(item)
                print("Created:", item["target"] + item["target_signature"], flush=True)
                if item["type"] == "PROCEDURE":
                    cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL", (item["target"] + item["target_signature"],))
                    actual_body = procedure_parts(cursor.fetchone()["DDL"])[1]
                    expected_body = procedure_parts(sql.replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1))[1]
                    if actual_body.strip() != expected_body.strip():
                        raise ValueError("Created support procedure differs from prepared SQL")
            if read_definitions(cursor) != definitions:
                raise ValueError("A source definition changed during preparation")

            # Only DEV objects are accessed in this transaction. In particular,
            # the source ADMIN hybrid table was read before BEGIN.
            cursor.execute("BEGIN TRANSACTION")
            counts = {}
            try:
                for plan in plans:
                    counts[plan["table"]] = insert_configuration(cursor, plan)
                    print("Inserted:", plan["table"], counts[plan["table"]], flush=True)
                verify_configuration(cursor, plans)
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
            tasks = assert_tasks_suspended(cursor)
            if tasks != before_tasks:
                raise ValueError("The DEV task inventory changed")
            cursor.execute("SELECT COUNT(*) AS N FROM KUMO_DEV.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE")
            if any(item["type"] == "TABLE" for item in created) and cursor.fetchone()["N"] != 0:
                raise ValueError("The newly created archive table is not empty")
            RECEIPT.write_text(json.dumps({
                "created": [item["target"] + item["target_signature"] for item in created],
                "skipped_existing_objects": [item["target"] + item["target_signature"] for item in skipped],
                "configuration": [{"target": plan["target"], "source_count": plan["source_count"],
                                   "selected": len(plan["rows"]), "inserted": counts[plan["table"]],
                                   "excluded_orphan_notifications": plan["excluded_orphans"]} for plan in plans],
                "dbt_project": PROJECT, "dbt_target": dbt_target,
                "tasks": tasks, "procedures_called": False, "runtime_rows_copied": False,
                "source_objects_written": False, "workflow_dep_rule_changed": False,
            }, indent=2) + "\n")
    finally:
        if created:
            refresh_export()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    run(parser.parse_args().apply)
