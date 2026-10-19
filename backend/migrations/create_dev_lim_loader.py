"""Add the DEV LIM loader and results table in MONITOR_META, without running it."""

import argparse
import json
from pathlib import Path
import re
import sys

from apply_dev_workbook_objects import (
    ROOT, connect, ddl_only, existing_objects, procedure_parts,
    refresh_export, replace_once, select_missing,
)


PLAN = {"objects": [
    {"type": "TABLE", "source": "KUMO_TST.RAW_LIM.RAW_LOAD_RESULTS",
     "signature": "", "target": "KUMO_DEV.MONITOR_META.RAW_LOAD_RESULTS",
     "target_signature": ""},
    {"type": "PROCEDURE", "source": "KUMO_TST.META.LOAD_RAW_LIM_FROM_STAGE",
     "signature": "(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN)",
     "target": "KUMO_DEV.MONITOR_META.LOAD_RAW_LIM_FROM_STAGE",
     "target_signature": "(VARCHAR, DATE, DATE, BOOLEAN, BOOLEAN, BOOLEAN, VARCHAR)"},
]}
SQL_FILE = Path(__file__).with_suffix(".sql")
RECEIPT = ROOT / "review/dev-lim-loader-migration-result.json"


def render_loader(source):
    item = PLAN["objects"][1]
    header, body = procedure_parts(source)
    header = replace_once(header, item["source"], item["target"])
    header = replace_once(header, "CREATE OR REPLACE PROCEDURE", "CREATE PROCEDURE")
    header = replace_once(header, '"P_SET_READY_TO_LOAD" BOOLEAN)',
                          '"P_SET_READY_TO_LOAD" BOOLEAN, "P_DATABASE" VARCHAR DEFAULT \'KUMO_DEV\')')
    if "EXECUTE AS CALLER" not in header:
        raise ValueError("The loader must preserve caller rights")

    # Qualify both persistent and temporary objects. Caller rights must not
    # inherit a different database/schema for any part of a reload operation.
    references = {
        "KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE": ("V_SOURCE_MAP", "MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE"),
        "RAW_LIM.RAW_LOAD_RESULTS": ("V_LOAD_RESULTS", "MONITOR_META.RAW_LOAD_RESULTS"),
        "RAW_LIM._TMP_SET_READY_LOG": ("V_READY_LATEST", "META._TMP_SET_READY_LOG"),
        "RAW_LIM._SET_READY_LOG": ("V_READY_HISTORY", "MONITOR_META._SET_READY_LOG"),
        "RAW_LIM.RAW_LIM_META": ("V_RAW_METADATA", "MONITOR_META.RAW_LIM_META"),
        "META.LIM_BATCH_CTRL": ("V_BATCH_CONTROL", "META.LIM_BATCH_CTRL"),
        "INFORMATION_SCHEMA.TABLES": ("V_INFORMATION_TABLES", "INFORMATION_SCHEMA.TABLES"),
        "TMP_RAW_LIM_FILES": ("V_FILES_TABLE", "MONITOR_META.TMP_RAW_LIM_FILES"),
        "TMP_RAW_LIM_RELOAD_PACKAGES": ("V_PACKAGES_TABLE", "MONITOR_META.TMP_RAW_LIM_RELOAD_PACKAGES"),
    }
    declarations = "".join("    " + variable + " VARCHAR;\n" for variable, _ in references.values())
    body = replace_once(body, "DECLARE\n", "DECLARE\n" + declarations
                        + "    EX_INVALID_DATABASE EXCEPTION (-20004, 'Invalid P_DATABASE');\n")
    assignments = "".join("    " + variable + " := V_DB || '." + target + "';\n"
                          for variable, target in references.values())
    # Rewrite the original SQL references before adding identifier strings,
    # which can contain the same names (e.g. INFORMATION_SCHEMA.TABLES).
    for original, (variable, _) in references.items():
        pattern = r"(?<![A-Za-z0-9_$.'])" + re.escape(original) + r"(?![A-Za-z0-9_$'])"
        body, count = re.subn(pattern, lambda _: "IDENTIFIER(:" + variable + ")", body)
        if not count:
            raise ValueError("Source reference not found: " + original)
    body = replace_once(body, "    V_DB := CURRENT_DATABASE();",
                        "    V_DB := UPPER(TRIM(P_DATABASE));\n"
                        "    IF (V_DB IS NULL OR NOT REGEXP_LIKE(V_DB, '^[A-Z_][A-Z0-9_$]*$')) THEN\n"
                        "        RAISE EX_INVALID_DATABASE;\n"
                        "    END IF;\n" + assignments)

    body = replace_once(body,
                        "'CREATE TABLE SDL.' || :V_CLONE_NAME || ' CLONE SDL.' || :V_TBL_NAME",
                        "'CREATE TABLE ' || :V_DB || '.SDL.' || :V_CLONE_NAME || ' CLONE ' || :V_DB || '.SDL.' || :V_TBL_NAME")
    body = replace_once(body, "'DROP TABLE IF EXISTS SDL.' || :V_TBL_NAME",
                        "'DROP TABLE IF EXISTS ' || :V_DB || '.SDL.' || :V_TBL_NAME")
    if "KUMO_TST" in body or "KUMO_ADMIN" in body or "CURRENT_DATABASE()" in body:
        raise ValueError("The loader retains an implicit or source database reference")
    # Retain GET_DDL's SQL string escaping, including regular expressions.
    return header + "\nAS '" + body.replace("'", "''") + "';\n"


def render(item, source):
    if item["type"] == "PROCEDURE":
        return render_loader(source)
    ddl = replace_once(ddl_only(source), item["source"], item["target"])
    return re.sub(r"^CREATE\s+OR\s+REPLACE\s+", "CREATE ", ddl, count=1, flags=re.I) + ";\n"


def source_definitions(cursor):
    result = {}
    for item in PLAN["objects"]:
        cursor.execute("SELECT GET_DDL(%s, %s, TRUE) AS DDL",
                       (item["type"], item["source"] + item["signature"]))
        ddl = cursor.fetchone()["DDL"]
        if not ddl or "#UNKNOWN_" in ddl:
            raise RuntimeError("Incomplete source DDL")
        result[item["source"]] = ddl
    return result


def missing_objects(cursor, plan):
    inventory = existing_objects(cursor)
    # This migration introduces one interface, not an additional overload of
    # an existing DEV loader. Stop if a different signature already exists.
    for item in plan["objects"]:
        if item["type"] != "PROCEDURE":
            continue
        matches = [row for row in inventory["PROCEDURE"] if row["name"] == item["target"].rsplit(".", 1)[1]]
        if matches:
            if str(ROOT / "snowflake") not in sys.path:
                sys.path.insert(0, str(ROOT / "snowflake"))
            from export_ddl import signature
            if any(signature(row["arguments"]) != item["target_signature"] for row in matches):
                raise RuntimeError("A different DEV loader interface already exists")
    return select_missing(plan, inventory)


def run(apply=False):
    from snowflake.connector import DictCursor
    created = []
    try:
        with connect() as connection, connection.cursor(DictCursor) as cursor:
            sources = source_definitions(cursor)
            statements = [(item, render(item, sources[item["source"]])) for item in PLAN["objects"]]
            SQL_FILE.write_text("-- Definitions only; use create_dev_lim_loader.py --apply to skip existing objects.\n"
                                "-- No data is copied, no loader is called and no task is enabled.\n"
                                "USE DATABASE KUMO_DEV;\n\n" + "\n".join(sql for _, sql in statements))
            missing, skipped = missing_objects(cursor, PLAN)
            for item in missing:
                print("Planned:", item["type"], item["target"] + item["target_signature"], flush=True)
            for item in skipped:
                print("Skip existing:", item["target"], flush=True)
            if not apply:
                print("Prepared SQL only; no Snowflake objects changed.", flush=True)
                return
            cursor.execute("USE DATABASE KUMO_DEV")
            for item, sql in statements:
                if item not in missing:
                    continue
                now_missing, _ = missing_objects(cursor, {"objects": [item]})
                if not now_missing:
                    skipped.append(item)
                    continue
                cursor.execute(sql)
                created.append(item)
                print("Created:", item["target"] + item["target_signature"], flush=True)
                if item["type"] == "PROCEDURE":
                    cursor.execute("SELECT GET_DDL('PROCEDURE', %s, TRUE) AS DDL",
                                   (item["target"] + item["target_signature"],))
                    actual = cursor.fetchone()["DDL"]
                    prepared = sql.replace("CREATE PROCEDURE", "CREATE OR REPLACE PROCEDURE", 1)
                    if procedure_parts(actual)[1].strip() != procedure_parts(prepared)[1].strip():
                        raise RuntimeError("Live procedure body differs from prepared SQL")
            if source_definitions(cursor) != sources:
                raise RuntimeError("Source definitions changed during migration")
            missing_after, _ = select_missing(PLAN, existing_objects(cursor))
            if missing_after:
                raise RuntimeError("A target is still missing")
            cursor.execute("SHOW TASKS IN DATABASE KUMO_DEV")
            tasks = [{key: row[key] for key in ("schema_name", "name", "state")} for row in cursor.fetchall()]
            if any(row["state"].upper() != "SUSPENDED" for row in tasks):
                raise RuntimeError("A DEV task is unexpectedly active")
            if PLAN["objects"][0] in created:
                cursor.execute("SELECT COUNT(*) AS N FROM KUMO_DEV.MONITOR_META.RAW_LOAD_RESULTS")
                if cursor.fetchone()["N"] != 0:
                    raise RuntimeError("The new results table is not empty")
            RECEIPT.write_text(json.dumps({
                "created": [item["target"] + item["target_signature"] for item in created],
                "skipped": [item["target"] + item["target_signature"] for item in skipped],
                "source_definitions_unchanged": True,
                "loader_called": False, "table_rows_copied": False,
                "workflow_dep_rule_changed": False, "tasks": tasks,
            }, indent=2) + "\n")
    finally:
        if created:
            refresh_export()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    run(parser.parse_args().apply)
