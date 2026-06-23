import os

DB = os.getenv("KUMO_DB", "KUMO_ADMIN")
SCHEMA = os.getenv("KUMO_SCHEMA", "WORKFLOW_MANAGER")

T_WORKFLOWS = f"{DB}.{SCHEMA}.WORKFLOWS"
T_TASKS = f"{DB}.{SCHEMA}.WORKFLOW_TASKS"
T_HISTORY = f"{DB}.{SCHEMA}.WORKFLOW_HISTORY"
T_QUEUE = f"{DB}.{SCHEMA}.WORKFLOW_RUN_QUEUE"
T_LOGS = f"{DB}.{SCHEMA}.WORKFLOW_RUN_LOGS"
T_NOTIFICATIONS = f"{DB}.{SCHEMA}.WORKFLOW_NOTIFICATIONS"

DEFAULT_TASK_WAREHOUSE = os.getenv("KUMO_TASK_WAREHOUSE", "KUMO_ELT_GEN_1")
PROGRESS_TABLE = os.getenv("KUMO_PROGRESS_TABLE", "KUMO_TST.META.EXECUTION_PROGRESS")
RUN_LOG_TABLE = os.getenv("KUMO_RUN_LOG_TABLE", "KUMO_TST.META.RUN_LOG")
REFRESH_SECONDS = int(os.getenv("KUMO_REFRESH_SECONDS", "5"))
USE_MOCK = os.getenv("KUMO_USE_MOCK", "false").lower() in ("1", "true", "yes", "y")

SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER = os.getenv("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE", "")
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "")
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", DB)
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", SCHEMA)

# Manual run behavior:
# queue = direct Streamlit-compatible path: insert WORKFLOW_HISTORY + WORKFLOW_RUN_QUEUE rows.
# procedure = call SP_WORKFLOW_REQUEST_RUN first, then fallback to queue insert if needed.
#
# The React/Flask endpoint must return quickly. In SPCS, calling the workflow
# procedure synchronously can block the HTTP request long enough for the browser
# to time out, leaving the UI stuck on the optimistic INITIATING state.
KUMO_MANUAL_RUN_MODE = os.getenv("KUMO_MANUAL_RUN_MODE", "queue").strip().lower()

# Application audit / user registry tables
T_APP_USER_SESSIONS = f"{DB}.{SCHEMA}.APP_USER_SESSIONS"
T_APP_USER_INTERACTIONS = f"{DB}.{SCHEMA}.APP_USER_INTERACTIONS"

# Audit behavior
KUMO_AUDIT_ENABLED = os.getenv("KUMO_AUDIT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
KUMO_APP_VERSION = os.getenv("KUMO_APP_VERSION", "dev")

# Application-level workflow run locks. Used for immediate cross-user UI state
# while the dispatcher/procedures catch up in WORKFLOW_HISTORY / WORKFLOW_RUN_QUEUE.
T_APP_WORKFLOW_RUN_LOCKS = f"{DB}.{SCHEMA}.APP_WORKFLOW_RUN_LOCKS"
KUMO_RUN_LOCK_TTL_MINUTES = int(os.getenv("KUMO_RUN_LOCK_TTL_MINUTES", "360"))
