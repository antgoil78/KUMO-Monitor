"""Portable definition defaults and validation; Snowflake resolves values."""

import re

DBT_PROJECT_TEMPLATE = "[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]"
DBT_TARGET_TEMPLATE = "[DBT_TARGET]"
COMMAND_FIELDS = ("sqlCommand", "dbtCommand", "dbtProjectFqn", "dbtTarget")


def validate_definition(definition):
    if not isinstance(definition, dict):
        raise ValueError("A workflow definition must be a JSON object")
    for field in COMMAND_FIELDS:
        value = str(definition.get(field) or "")
        if re.search(r"#\[[A-Za-z_][A-Za-z0-9_]*\]#", value):
            raise ValueError("Use [VARIABLE] without # in workflow commands")
        if re.search(r"\bKUMO_(?:DEV|TST|ADMIN)\b", value, re.I):
            raise ValueError(f"{field} contains an environment-specific name. Use [DATABASE], [DBT_PROJECT] or [DBT_TARGET].")
    return definition
