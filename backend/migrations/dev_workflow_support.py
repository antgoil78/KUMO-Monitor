"""Render the four DEV workflow support procedures without executing them."""

import re

from apply_dev_workbook_objects import procedure_parts, replace_once


OBJECTS = [
    {"type": "TABLE", "source": "KUMO_TST.RAW_LIM.RAW_LIM_CONTROL_ARCHIVE", "signature": "",
     "target": "KUMO_DEV.MONITOR_META.RAW_LIM_CONTROL_ARCHIVE", "target_signature": ""},
    {"type": "PROCEDURE", "source": "KUMO_ADMIN.WORKFLOW_MANAGER.SP_RAW_LIM_SET_READY",
     "signature": "(VARCHAR, BOOLEAN, BOOLEAN)", "target": "KUMO_DEV.MONITOR_APP.SP_RAW_LIM_SET_READY",
     "target_signature": "(VARCHAR, BOOLEAN, BOOLEAN, VARCHAR)"},
    {"type": "PROCEDURE", "source": "KUMO_ADMIN.WORKFLOW_MANAGER.SP_CLEANUP_OLD_TMP_TASKS",
     "signature": "(FLOAT)", "target": "KUMO_DEV.MONITOR_APP.SP_CLEANUP_OLD_TMP_TASKS",
     "target_signature": "(FLOAT)"},
    {"type": "PROCEDURE", "source": "KUMO_TST.RAW_LIM.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST",
     "signature": "()", "target": "KUMO_DEV.MONITOR_APP.SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST",
     "target_signature": "(VARCHAR)"},
    {"type": "PROCEDURE", "source": "KUMO_ADMIN.META.SP_PURGE_LIM_BATCH",
     "signature": "(VARCHAR)", "target": "KUMO_DEV.MONITOR_APP.SP_PURGE_LIM_BATCH",
     "target_signature": "(VARCHAR)"},
]


def validate_database(variable="V_DATABASE"):
    return (
        f"    IF ({variable} IS NULL OR NOT REGEXP_LIKE({variable}, '^[A-Z_][A-Z0-9_$]*$')) THEN\n"
        "        RAISE E_INVALID_DATABASE;\n"
        "    END IF;\n"
    )


def render_procedure(item, source):
    header, body = procedure_parts(source)
    name = item["source"].rsplit(".", 1)[1]
    header = replace_once(header, item["source"], item["target"])
    header = replace_once(header, "CREATE OR REPLACE PROCEDURE", "CREATE PROCEDURE")
    if name in ("SP_RAW_LIM_SET_READY", "SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST"):
        first, rest = header.split("\n", 1)
        end = first.rfind(")")
        separator = "" if first[end - 1] == "(" else ", "
        header = first[:end] + separator + '"P_DATABASE" VARCHAR DEFAULT \'KUMO_DEV\'' + first[end:] + "\n" + rest

    if name == "SP_RAW_LIM_SET_READY":
        references = {
            "KUMO_ADMIN.WORKFLOW_MANAGER.RAW_LIM_PKG_GROUP_SOURCE": ("V_SOURCE_MAP", "MONITOR_META.RAW_LIM_PKG_GROUP_SOURCE"),
            "KUMO_TST.RAW_LIM.RAW_LIM_META": ("V_METADATA_TABLE", "MONITOR_META.RAW_LIM_META"),
            "KUMO_TST.RAW_LIM._TMP_SET_READY_LOG": ("V_LATEST_LOG", "META._TMP_SET_READY_LOG"),
            "KUMO_TST.RAW_LIM._SET_READY_LOG": ("V_HISTORY_LOG", "MONITOR_META._SET_READY_LOG"),
            "KUMO_TST.RAW_LIM.RAW_LIM_CONTROL_ARCHIVE": ("V_ARCHIVE", "MONITOR_META.RAW_LIM_CONTROL_ARCHIVE"),
            "KUMO_TST.META.LIM_BATCH_CTRL": ("V_BATCH_CTRL", "META.LIM_BATCH_CTRL"),
        }
        for original, (variable, _) in references.items():
            body, count = re.subn(re.escape(original) + r"(?![A-Za-z0-9_$])", lambda _: "IDENTIFIER(:" + variable + ")", body)
            if not count:
                raise ValueError("Missing source reference: " + original)
        declarations = "    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));\n"
        declarations += "    E_INVALID_DATABASE EXCEPTION (-20002, 'Invalid database identifier');\n"
        declarations += "".join("    " + variable + " VARCHAR;\n" for variable, _ in references.values())
        body = replace_once(body, "DECLARE\n", "DECLARE\n" + declarations)
        assignments = "".join("    " + variable + " := V_DATABASE || '." + target + "';\n"
                              for variable, target in references.values())
        body = replace_once(body, "BEGIN\n", "BEGIN\n" + validate_database() + assignments)
        body = replace_once(body, "'KUMO_TST.RAW_LIM.RAW_LIM_'", ":V_DATABASE || '.RAW_LIM.RAW_LIM_'")

    elif name == "SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST":
        declarations = (
            "    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(P_DATABASE));\n"
            "    V_INFORMATION_TABLES VARCHAR;\n"
            "    V_TABLES RESULTSET;\n"
            "    E_INVALID_DATABASE EXCEPTION (-20002, 'Invalid database identifier');\n"
        )
        body = replace_once(body, "DECLARE\n", "DECLARE\n" + declarations)
        match = re.search(r"    cur CURSOR FOR\s+(SELECT TABLE_NAME.*?ORDER BY TABLE_NAME);", body, re.S)
        if not match:
            raise ValueError("Incremental loader source cursor changed")
        query = replace_once(match.group(1), "KUMO_TST.INFORMATION_SCHEMA.TABLES", "IDENTIFIER(:V_INFORMATION_TABLES)")
        body = body[:match.start()] + body[match.end():]
        body = replace_once(body, "\nBEGIN\n", "\nBEGIN\n" + validate_database()
                            + "    V_INFORMATION_TABLES := V_DATABASE || '.INFORMATION_SCHEMA.TABLES';\n"
                            + "    V_TABLES := (" + query + ");\n")
        body = replace_once(body, "FOR rec IN cur DO", "FOR rec IN V_TABLES DO")
        body = replace_once(body, """'"KUMO_TST"."RAW_LIM".'""", """'"' || V_DATABASE || '"."RAW_LIM".'""")
        body = replace_once(body, "@KUMO_TST.META.AZURE_LIM_STAGE", "@' || V_DATABASE || '.META.AZURE_LIM_STAGE")
        body = replace_once(body, "KUMO_TST.META.KUMO_LIM", "' || V_DATABASE || '.META.KUMO_LIM")

    elif name == "SP_CLEANUP_OLD_TMP_TASKS":
        body = body.replace("KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_TMP_TASKS", "MONITOR_META.WORKFLOW_TMP_TASKS")
        body = body.replace("KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY", "MONITOR_META.WORKFLOW_HISTORY")
        body = replace_once(body, "function assertSafeTaskFqn(taskFqn) {",
                            'const databaseRow = exec("SELECT CURRENT_DATABASE()");\n'
                            'databaseRow.next();\n'
                            'const taskPrefix = String(databaseRow.getColumnValue(1)).toUpperCase() + ".MONITOR_APP.TMP_WF_";\n\n'
                            'function assertSafeTaskFqn(taskFqn) {')
        body = replace_once(body, "  return s;", '  if (!s.toUpperCase().startsWith(taskPrefix)) {\n'
                            '    throw new Error("Task is outside this database and workflow execution namespace");\n'
                            '  }\n  return s;')

    elif name == "SP_PURGE_LIM_BATCH":
        # Preserve the processed/three-day purge contract, but qualify the
        # snapshot/control table and bind row values in generated DELETE SQL.
        for marker in ("WHERE PROCESSED_FL", "AND NOT DELETED_FL", "PROCESSED_DTTM < DATEADD('day', -3, sysdate())"):
            if marker not in body:
                raise ValueError("Purge source contract changed: " + marker)
        body = '''
DECLARE
    V_DATABASE VARCHAR DEFAULT UPPER(TRIM(COALESCE(DBT_DB, 'KUMO_DEV')));
    V_BATCH_CTRL VARCHAR;
    V_SNAPSHOT VARCHAR;
    V_TABLE VARCHAR;
    V_TARGET VARCHAR;
    V_DELETE_SQL VARCHAR;
    V_TOTAL INTEGER DEFAULT 0;
    V_COUNT INTEGER;
    V_TABLES RESULTSET;
    E_INVALID_DATABASE EXCEPTION (-20002, 'Invalid database identifier');
    E_INVALID_TARGET EXCEPTION (-20003, 'Purge target is outside this database RAW_LIM schema');
BEGIN
''' + validate_database() + '''
    V_BATCH_CTRL := V_DATABASE || '.META.LIM_BATCH_CTRL';
    V_SNAPSHOT := V_DATABASE || '.MONITOR_META._PURGE_BATCH_SNAPSHOT';

    CREATE OR REPLACE TEMPORARY TABLE IDENTIFIER(:V_SNAPSHOT) AS
    SELECT DW_TABLE_NM, DW_FILE_NM
      FROM IDENTIFIER(:V_BATCH_CTRL)
     WHERE PROCESSED_FL
       AND NOT DELETED_FL
       AND PROCESSED_DTTM < DATEADD('day', -3, SYSDATE());

    V_TABLES := (SELECT DISTINCT DW_TABLE_NM FROM IDENTIFIER(:V_SNAPSHOT));
    FOR REC IN V_TABLES DO
        V_TABLE := REC.DW_TABLE_NM;
        IF (REGEXP_LIKE(UPPER(V_TABLE), '^RAW_LIM[.][A-Z_][A-Z0-9_$]*$')) THEN
            V_TARGET := V_DATABASE || '.' || UPPER(V_TABLE);
        ELSEIF (REGEXP_LIKE(UPPER(V_TABLE), '^[A-Z_][A-Z0-9_$]*[.]RAW_LIM[.][A-Z_][A-Z0-9_$]*$')
                AND SPLIT_PART(UPPER(V_TABLE), '.', 1) = V_DATABASE) THEN
            V_TARGET := UPPER(V_TABLE);
        ELSE
            RAISE E_INVALID_TARGET;
        END IF;

        V_DELETE_SQL := 'DELETE FROM ' || V_TARGET || ' T USING ' || V_SNAPSHOT ||
            ' SNAP WHERE T.DW_FILE_NM = SNAP.DW_FILE_NM AND SNAP.DW_TABLE_NM = ?';
        EXECUTE IMMEDIATE :V_DELETE_SQL USING (V_TABLE);
        V_COUNT := SQLROWCOUNT;
        V_TOTAL := V_TOTAL + V_COUNT;
    END FOR;

    UPDATE IDENTIFIER(:V_BATCH_CTRL) CTRL
       SET DELETED_FL = TRUE
      FROM IDENTIFIER(:V_SNAPSHOT) SNAP
     WHERE CTRL.DW_TABLE_NM = SNAP.DW_TABLE_NM
       AND CTRL.DW_FILE_NM = SNAP.DW_FILE_NM
       AND CTRL.PROCESSED_FL
       AND NOT CTRL.DELETED_FL;

    DROP TABLE IF EXISTS IDENTIFIER(:V_SNAPSHOT);
    RETURN 'Deleted ' || V_TOTAL || ' total rows';
END;
'''
    else:
        raise ValueError("Unexpected support procedure")
    if "KUMO_TST" in body.upper() or "KUMO_ADMIN" in body.upper():
        raise ValueError("A source database reference remains")
    return header + "\nAS '" + body.replace("'", "''") + "';\n"
