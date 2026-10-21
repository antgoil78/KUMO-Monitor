import os
import re


# One application layout in every environment; local work defaults to DEV.
ENVIRONMENT = os.getenv("KUMO_ENV", "DEV").strip().upper() or "DEV"
if ENVIRONMENT not in ("DEV", "TST"):
    raise ValueError("KUMO_ENV must be DEV or TST")
ENVIRONMENT_DATABASE = f"KUMO_{ENVIRONMENT}"


def _setting(name, default):
    return os.getenv(name, "").strip() or default


# Deployment identity labels the service and never changes data routing.
DEPLOYMENT_ENVIRONMENT = _setting("KUMO_DEPLOYMENT_ENV", ENVIRONMENT).upper()
DEPLOYMENT_ENVIRONMENT = {"TST": "TEST", "PRD": "PROD"}.get(DEPLOYMENT_ENVIRONMENT, DEPLOYMENT_ENVIRONMENT)
if DEPLOYMENT_ENVIRONMENT and DEPLOYMENT_ENVIRONMENT not in ("DEV", "TEST", "UAT", "PROD"):
    raise ValueError("KUMO_DEPLOYMENT_ENV must be DEV, TEST, UAT or PROD")


def _identifier(name, default):
    value = _setting(name, default)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        raise ValueError(f"{name} must be an unquoted Snowflake identifier")
    return value.upper()


# The one service bootstrap pointer. The table may live outside the database
# being monitored; no data-routing decision is derived from KUMO_ENV.
PARAMETER_STORE = _setting("KUMO_ENVIRONMENT_TABLE", f"{ENVIRONMENT_DATABASE}.MONITOR_META.APPLICATION_PARAMETERS")
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){2}", PARAMETER_STORE):
    raise ValueError("KUMO_ENVIRONMENT_TABLE must be an unquoted database.schema.table name")
PARAMETER_STORE = PARAMETER_STORE.upper()
ENVIRONMENT_PARAMETER_KEYS = (
    "DATABASE", "MONITOR_APP_SCHEMA", "META_SCHEMA", "APP_SCHEMA", "RAW_SCHEMA",
    "DBT_SCHEMA", "DBT_PROJECT", "DBT_TARGET",
)


def apply_environment(rows):
    """Use the shared parameter store; reject missing, secret or unsafe routing."""
    parameters = {}
    for row in rows:
        key = row["PARAMETER_KEY"]
        if key in parameters:
            raise ValueError(f"Duplicate environment parameter: {key}")
        parameters[key] = row
    values = {}
    for key in ENVIRONMENT_PARAMETER_KEYS:
        row = parameters.get(key, {})
        value = row.get("PARAMETER_VALUE")
        if (not row.get("ACTIVE_FL") or row.get("IS_SECRET") or row.get("VALUE_TYPE") != "STRING"
                or not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value)):
            raise ValueError(f"Missing or invalid environment parameter: {key}")
        values[key] = value if key == "DBT_TARGET" else value.upper()
    _configure_routes(values, verified=True)


def _configure_routes(values, verified=False):
    database, engine, metadata = (values[key] for key in ("DATABASE", "MONITOR_APP_SCHEMA", "META_SCHEMA"))
    application, raw = values["APP_SCHEMA"], values["RAW_SCHEMA"]
    routes = dict(DB=database, DATA_DATABASE=database, LIM_DATABASE=database,
                  SCHEMA=engine, METADATA_SCHEMA=metadata, ORCHESTRATION_SCHEMA=metadata,
                  APPLICATION_SCHEMA=application, RAW_SCHEMA=raw,
                  APP_NAMESPACE=f"{database}.{engine}", METADATA_NAMESPACE=f"{database}.{metadata}",
                  ORCHESTRATION_NAMESPACE=f"{database}.{metadata}", RAW_NAMESPACE=f"{database}.{raw}",
                  SNOWFLAKE_DATABASE=database, SNOWFLAKE_SCHEMA=engine)
    for name, obj in {
        "T_WORKFLOWS": "WORKFLOWS", "T_TASKS": "WORKFLOW_TASKS", "T_HISTORY": "WORKFLOW_HISTORY",
        "T_QUEUE": "WORKFLOW_RUN_QUEUE", "T_LOGS": "WORKFLOW_RUN_LOGS", "T_NOTIFICATIONS": "WORKFLOW_NOTIFICATIONS",
        "T_APPLICATION_PARAMETERS": "APPLICATION_PARAMETERS", "T_EMAIL_GROUPS": "EMAIL_GROUPS",
        "T_WORKFLOW_TMP_TASKS": "WORKFLOW_TMP_TASKS", "T_RAW_LIM_PKG_GROUP_SOURCE": "RAW_LIM_PKG_GROUP_SOURCE",
        "T_ORCHESTRATION_WORKFLOW": "WORKFLOW", "T_ORCHESTRATION_JOB": "JOB",
        "T_ORCHESTRATION_WORKFLOW_JOB": "WORKFLOW_JOB", "T_ORCHESTRATION_DEPENDENCY": "WORKFLOW_DEPENDENCY",
        "RAW_LIM_META_TABLE": "RAW_LIM_META", "SET_READY_HISTORY_TABLE": "_SET_READY_LOG",
        "SET_READY_LATEST_TABLE": "_TMP_SET_READY_LOG",
        "T_APP_USER_SESSIONS": "APP_USER_SESSIONS", "T_APP_USER_INTERACTIONS": "APP_USER_INTERACTIONS",
        "T_APP_WORKFLOW_RUN_LOCKS": "APP_WORKFLOW_RUN_LOCKS", "LIM_LOAD_PROCEDURE": "LOAD_RAW_LIM_FROM_STAGE",
    }.items():
        routes[name] = f"{database}.{metadata}.{obj}"
    for name, obj in {
        "PROGRESS_TABLE": "EXECUTION_PROGRESS", "MODEL_PROGRESS_TABLE": "MODEL_PROGRESS",
        "TEST_PROGRESS_TABLE": "TEST_PROGRESS", "RUN_LOG_TABLE": "RUN_LOG",
        "WORKFLOW_DEP_RULE_TABLE": "WORKFLOW_DEP_RULE", "MODEL_STATUS_TABLE": "MODEL_STATUS",
        "WORKFLOW_STATUS_TABLE": "WORKFLOW_STATUS", "LIM_STAGE": "AZURE_LIM_STAGE",
        "LIM_BATCH_CTRL_TABLE": "LIM_BATCH_CTRL",
    }.items():
        routes[name] = f"{database}.{application}.{obj}"
    routes["RULE_VERIFY_PROCEDURE"] = f"{database}.{engine}.SP_RULE_BASED_WORKFLOW_REQUEST_RUN"
    # Old per-object overrides may agree, but may no longer redirect queries away
    # from the parameter store. Fail explicitly instead of silently ignoring one.
    overrides = {"KUMO_DB": "DB", "KUMO_SCHEMA": "SCHEMA", "KUMO_METADATA_SCHEMA": "METADATA_SCHEMA",
                 "KUMO_LIM_DATABASE": "LIM_DATABASE", "KUMO_ORCHESTRATION_SCHEMA": "ORCHESTRATION_SCHEMA"}
    overrides.update({"KUMO_" + name: name for name in (
        "PROGRESS_TABLE", "MODEL_PROGRESS_TABLE", "TEST_PROGRESS_TABLE", "RUN_LOG_TABLE",
        "WORKFLOW_DEP_RULE_TABLE", "MODEL_STATUS_TABLE", "WORKFLOW_STATUS_TABLE",
        "RULE_VERIFY_PROCEDURE", "LIM_STAGE", "LIM_LOAD_PROCEDURE")})
    routes["T_APPLICATION_PARAMETERS"] = PARAMETER_STORE
    for setting, name in overrides.items():
        if verified and _setting(setting, routes[name]).upper() != routes[name].upper():
            raise ValueError(f"{setting} conflicts with the environment parameters; configure the parameter store")
    globals().update(values)
    globals().update(routes)


_configure_routes(dict(DATABASE=ENVIRONMENT_DATABASE, MONITOR_APP_SCHEMA="MONITOR_APP",
                       META_SCHEMA="MONITOR_META", APP_SCHEMA="META", RAW_SCHEMA="RAW_LIM",
                       DBT_SCHEMA="DBT_APP", DBT_PROJECT=ENVIRONMENT_DATABASE, DBT_TARGET="kumo_" + ENVIRONMENT))

DEFAULT_TASK_WAREHOUSE = os.getenv("KUMO_TASK_WAREHOUSE", "KUMO_ELT_GEN_1")
AUTO_RESUME_TASKS = _setting("KUMO_AUTO_RESUME_TASKS", "false").lower() in ("1", "true", "yes", "y")
LIM_ROLE = _identifier("KUMO_LIM_ROLE", "KUMO_ADMIN")
LIM_READINESS_SOURCE = "BATCH_CONTROL"
REFRESH_SECONDS = int(os.getenv("KUMO_REFRESH_SECONDS", "5"))
KUMO_DASHBOARD_CACHE_SECONDS = int(os.getenv("KUMO_DASHBOARD_CACHE_SECONDS", "30"))
USE_MOCK = os.getenv("KUMO_USE_MOCK", "false").lower() in ("1", "true", "yes", "y")

SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER = os.getenv("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_PASSCODE = os.getenv("SNOWFLAKE_PASSCODE", "")
SNOWFLAKE_PASSCODE_IN_PASSWORD = os.getenv("SNOWFLAKE_PASSCODE_IN_PASSWORD", "false").strip().lower() in ("1", "true", "yes", "y")
SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE", "")
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "")
SNOWFLAKE_LOGIN_TIMEOUT_SECONDS = int(os.getenv("SNOWFLAKE_LOGIN_TIMEOUT_SECONDS", "15"))
SNOWFLAKE_NETWORK_TIMEOUT_SECONDS = int(os.getenv("SNOWFLAKE_NETWORK_TIMEOUT_SECONDS", "45"))
SNOWFLAKE_SOCKET_TIMEOUT_SECONDS = int(os.getenv("SNOWFLAKE_SOCKET_TIMEOUT_SECONDS", "30"))
SNOWFLAKE_STATEMENT_TIMEOUT_SECONDS = int(os.getenv("SNOWFLAKE_STATEMENT_TIMEOUT_SECONDS", "1200"))
KUMO_SESSION_CACHE_SECONDS = int(os.getenv("KUMO_SESSION_CACHE_SECONDS", "300"))
KUMO_ACTIVITY_LEASE_SECONDS = int(os.getenv("KUMO_ACTIVITY_LEASE_SECONDS", "90"))
KUMO_LOOKUP_USER_PROFILE = os.getenv("KUMO_LOOKUP_USER_PROFILE", "false").strip().lower() in ("1", "true", "yes", "y")

# Manual run behavior:
# queue = direct Streamlit-compatible path: insert WORKFLOW_HISTORY + WORKFLOW_RUN_QUEUE rows.
# procedure = call SP_WORKFLOW_REQUEST_RUN first, then fallback to queue insert if needed.
#
# The React/Flask endpoint must return quickly. In SPCS, calling the workflow
# procedure synchronously can block the HTTP request long enough for the browser
# to time out, leaving the UI stuck on the optimistic INITIATING state.
KUMO_MANUAL_RUN_MODE = os.getenv("KUMO_MANUAL_RUN_MODE", "queue").strip().lower()

# Audit behavior
KUMO_AUDIT_ENABLED = os.getenv("KUMO_AUDIT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
KUMO_APP_VERSION = os.getenv("KUMO_APP_VERSION", "dev")

# Application-level workflow run locks. Used for immediate cross-user UI state
# while the dispatcher/procedures catch up in WORKFLOW_HISTORY / WORKFLOW_RUN_QUEUE.
KUMO_RUN_LOCK_TTL_MINUTES = int(os.getenv("KUMO_RUN_LOCK_TTL_MINUTES", "360"))
KUMO_SHARED_LOCK_SYNC_SECONDS = int(os.getenv("KUMO_SHARED_LOCK_SYNC_SECONDS", "1"))
KUMO_STATUS_ACTIVE_POLL_SECONDS = float(os.getenv("KUMO_STATUS_ACTIVE_POLL_SECONDS", "1"))
KUMO_STATUS_IDLE_POLL_SECONDS = float(os.getenv("KUMO_STATUS_IDLE_POLL_SECONDS", "30"))
KUMO_STATUS_COORDINATOR_IDLE_GRACE_SECONDS = float(os.getenv("KUMO_STATUS_COORDINATOR_IDLE_GRACE_SECONDS", "10"))
