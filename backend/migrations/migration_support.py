"""Shared helpers for the current DEV migrations; no automatic DDL exports."""

import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def connect():
    from dotenv import load_dotenv
    import snowflake.connector
    load_dotenv(ROOT / ".env")
    settings = {key: os.environ["SNOWFLAKE_" + key.upper()] for key in ("account", "user", "password")}
    settings.update(login_timeout=20, network_timeout=90,
                    session_parameters={"QUERY_TAG": "KUMO_DEV_MIGRATION", "STATEMENT_TIMEOUT_IN_SECONDS": 180})
    for key in ("role", "warehouse", "passcode"):
        if os.getenv("SNOWFLAKE_" + key.upper()):
            settings[key] = os.environ["SNOWFLAKE_" + key.upper()]
    return snowflake.connector.connect(**settings)


def procedure_parts(ddl):
    match = re.search(r"\bCREATE\s+OR\s+REPLACE\b", ddl, re.I)
    if not match:
        raise ValueError("Expected a complete exported definition")
    ddl = ddl[match.start():].strip().rstrip(";").rstrip()
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
