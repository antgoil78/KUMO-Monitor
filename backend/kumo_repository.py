import json
import uuid
from datetime import datetime, timezone

import config
import snowflake_client as sf
from utils import normalize_rows, row_get, sql_escape, parse_variant_array, next_run, format_task_name

_describe_cache = {}


def _query(sql, params=None):
    """Run KUMO metadata/admin queries through the SPCS service context.

    Caller-rights is used to identify the browser user, but the KUMO admin
    tables and workflow queue/history are application-owned objects. Using the
    service context avoids every caller needing direct object privileges on
    KUMO_ADMIN.WORKFLOW_MANAGER.
    """
    return sf.query_service(sql, params=params or {}, use_warehouse=True, include_context=True)


def _execute(sql, params=None):
    """Execute KUMO metadata/admin DML through the SPCS service context."""
    return sf.execute_service(sql, params=params or {}, use_warehouse=True, include_context=True)


def clear_cache():
    _describe_cache.clear()


def describe_table(fqn):
    if fqn in _describe_cache:
        return _describe_cache[fqn]
    rows = _query(f"DESC TABLE {fqn}")
    out = {}
    for row in rows:
        name = row_get(row, "name", "NAME")
        typ = row_get(row, "type", "TYPE")
        if name:
            out[str(name).upper()] = str(typ)
    _describe_cache[fqn] = out
    return out


def object_exists(full_name):
    try:
        _query(f"SELECT 1 FROM {full_name} LIMIT 1")
        return True
    except Exception:
        return False


def load_history(limit=200):
    limit = max(1, min(int(limit or 200), 2000))
    h_types = describe_table(config.T_HISTORY)
    order_expr = "COALESCE(REQUESTED_AT, START_TIME, END_TIME)" if "REQUESTED_AT" in h_types else "COALESCE(START_TIME, END_TIME)"
    rows = _query(f"SELECT * FROM {config.T_HISTORY} ORDER BY {order_expr} DESC NULLS LAST LIMIT {limit}")
    return normalize_rows(rows)


def load_workflow_history(workflow_id, limit=100):
    limit = max(1, min(int(limit or 100), 1000))
    h_types = describe_table(config.T_HISTORY)
    order_expr = "COALESCE(REQUESTED_AT, START_TIME, END_TIME)" if "REQUESTED_AT" in h_types else "COALESCE(START_TIME, END_TIME)"
    rows = _query(
        f"""
        SELECT *
        FROM {config.T_HISTORY}
        WHERE WORKFLOW_ID = %(workflow_id)s
        ORDER BY {order_expr} DESC NULLS LAST
        LIMIT {limit}
        """,
        {"workflow_id": workflow_id},
    )
    return normalize_rows(rows)


def load_tasks():
    try:
        return normalize_rows(_query(f"SELECT * FROM {config.T_TASKS}"))
    except Exception:
        return []


def get_engine_state():
    def rdict(row):
        return {str(k).upper(): v for k, v in dict(row).items()}

    task_name = "TASK_WF_MASTER_DISPATCHER"
    for pattern in (f"{task_name}%", f"%{task_name}%"):
        for scope in (f"SCHEMA {config.DB}.{config.SCHEMA}", f"DATABASE {config.DB}", "ACCOUNT"):
            try:
                rows = _query(f"SHOW TASKS LIKE '{pattern}' IN {scope}")
                if rows:
                    for row in rows:
                        d = rdict(row)
                        if str(d.get("NAME", "")).upper() == task_name:
                            state = str(d.get("STATE", "")).upper().strip()
                            return {"task": str(d.get("NAME", task_name)), "state": state, "status": _engine_status(state)}
                    d = rdict(rows[0])
                    state = str(d.get("STATE", "")).upper().strip()
                    return {"task": str(d.get("NAME", task_name)), "state": state, "status": _engine_status(state)}
            except Exception:
                continue
    return {"task": None, "state": None, "status": "MISSING"}


def _engine_status(state):
    if state in ("STARTED", "RESUMED"):
        return "RUNNING"
    if state == "SUSPENDED":
        return "STOPPED"
    return state or "UNKNOWN"


def load_monitor_rows():
    wf_types = describe_table(config.T_WORKFLOWS)
    t_types = describe_table(config.T_TASKS)
    h_types = describe_table(config.T_HISTORY)

    has_desc = "DESCRIPTION" in wf_types
    has_workflow_type = "WORKFLOW_TYPE" in wf_types
    has_sql_command = "SQL_COMMAND" in wf_types
    has_schedule_cron = "SCHEDULE_CRON" in t_types
    has_schedule = "SCHEDULE" in t_types
    has_tz = "SCHEDULE_TIMEZONE" in t_types
    has_task_enabled = "IS_ENABLED" in t_types
    has_requested_at = "REQUESTED_AT" in h_types
    has_requested_by = "REQUESTED_BY" in h_types
    has_trigger_source = "TRIGGER_SOURCE" in h_types

    desc_expr = "w.DESCRIPTION" if has_desc else "NULL"
    workflow_type_expr = "w.WORKFLOW_TYPE" if has_workflow_type else "'DBT'"
    sql_command_expr = "w.SQL_COMMAND" if has_sql_command else "NULL"
    order_expr = "COALESCE(hh.END_TIME, hh.START_TIME, hh.REQUESTED_AT)" if has_requested_at else "COALESCE(hh.END_TIME, hh.START_TIME)"

    extra_select = []
    if has_requested_at:
        extra_select.append("hh.REQUESTED_AT")
    if has_requested_by:
        extra_select.append("hh.REQUESTED_BY")
    if has_trigger_source:
        extra_select.append("hh.TRIGGER_SOURCE")
    extra_sql = (", " + ", ".join(extra_select)) if extra_select else ""

    q = f"""
    WITH LAST_RUN AS (
      SELECT
        hh.WORKFLOW_ID,
        hh.RUN_ID,
        hh.STATUS,
        hh.START_TIME,
        hh.END_TIME
        {extra_sql}
      FROM {config.T_HISTORY} hh
      QUALIFY ROW_NUMBER() OVER (
        PARTITION BY hh.WORKFLOW_ID
        ORDER BY {order_expr} DESC NULLS LAST, hh.RUN_ID DESC
      ) = 1
    ),
    ONE_TASK AS (
      SELECT
        t.WORKFLOW_ID,
        {('t.SCHEDULE_CRON' if has_schedule_cron else ('t.SCHEDULE' if has_schedule else 'NULL'))} AS SCHEDULE_ANY,
        {('t.SCHEDULE_TIMEZONE' if has_tz else "'UTC'")} AS SCHEDULE_TIMEZONE,
        {('t.IS_ENABLED' if has_task_enabled else 'TRUE')} AS IS_ENABLED,
        {('t.ON_SUCCESS' if 'ON_SUCCESS' in t_types else 'NULL')} AS ON_SUCCESS,
        {('t.ON_FAIL' if 'ON_FAIL' in t_types else 'NULL')} AS ON_FAIL
      FROM {config.T_TASKS} t
      QUALIFY ROW_NUMBER() OVER (PARTITION BY t.WORKFLOW_ID ORDER BY t.WORKFLOW_ID) = 1
    )
    SELECT
      w.WORKFLOW_ID,
      w.WORKFLOW_GROUP,
      w.WORKFLOW_NAME,
      {workflow_type_expr} AS WORKFLOW_TYPE,
      {desc_expr} AS DESCRIPTION,
      w.DBT_COMMAND,
      w.DBT_PROJECT_FQN,
      w.DBT_TARGET,
      w.DBT_WORKSPACE,
      {sql_command_expr} AS SQL_COMMAND,
      w.IS_ENABLED AS WORKFLOW_ENABLED,
      tt.SCHEDULE_ANY AS SCHEDULE_CRON,
      tt.SCHEDULE_TIMEZONE AS SCHEDULE_TIMEZONE,
      tt.IS_ENABLED AS TASK_ENABLED,
      tt.ON_SUCCESS AS ON_SUCCESS,
      tt.ON_FAIL AS ON_FAIL,
      lr.RUN_ID AS LAST_RUN_ID,
      lr.STATUS AS LAST_STATUS,
      lr.START_TIME AS LAST_START_TIME,
      lr.END_TIME AS LAST_END_TIME
      {(', lr.REQUESTED_AT AS LAST_REQUESTED_AT' if has_requested_at else '')}
      {(', lr.REQUESTED_BY AS LAST_REQUESTED_BY' if has_requested_by else '')}
      {(', lr.TRIGGER_SOURCE AS LAST_TRIGGER_SOURCE' if has_trigger_source else '')},
      CASE
        WHEN lr.START_TIME IS NULL OR lr.END_TIME IS NULL THEN NULL
        ELSE DATEDIFF('second', lr.START_TIME, lr.END_TIME)
      END AS LAST_DURATION_SECONDS
    FROM {config.T_WORKFLOWS} w
    LEFT JOIN ONE_TASK tt ON tt.WORKFLOW_ID = w.WORKFLOW_ID
    LEFT JOIN LAST_RUN lr ON lr.WORKFLOW_ID = w.WORKFLOW_ID
    ORDER BY w.WORKFLOW_GROUP, w.WORKFLOW_NAME
    """
    rows = normalize_rows(_query(q))
    return _order_and_enrich(rows)


def _order_and_enrich(rows):
    children_of = {}
    row_by_id = {}
    for row in rows:
        workflow_id = str(row.get("WORKFLOW_ID") or "")
        if not workflow_id:
            continue
        row_by_id[workflow_id] = row
        for col in ("ON_SUCCESS", "ON_FAIL"):
            for child_id in parse_variant_array(row.get(col)):
                if child_id and child_id != "[]":
                    children_of.setdefault(workflow_id, []).append((child_id, col))

    child_ids = {cid for kids in children_of.values() for cid, _ in kids}
    ordered = []
    seen = set()

    def add_row(workflow_id, indent=0):
        if workflow_id in seen or workflow_id not in row_by_id:
            return
        seen.add(workflow_id)
        row = dict(row_by_id[workflow_id])
        row["INDENT"] = indent
        ordered.append(row)
        for child_id, _ in children_of.get(workflow_id, []):
            add_row(child_id, indent + 1)

    for row in rows:
        wid = str(row.get("WORKFLOW_ID") or "")
        if wid and wid not in child_ids:
            add_row(wid, 0)
    for row in rows:
        wid = str(row.get("WORKFLOW_ID") or "")
        if wid and wid not in seen:
            add_row(wid, 0)

    run_ids = [str(r.get("LAST_RUN_ID")) for r in ordered if str(r.get("LAST_STATUS", "")).upper() in ("RUNNING", "IN_PROGRESS", "EXECUTING") and r.get("LAST_RUN_ID")]
    progress = load_progress_for_runs(run_ids) if run_ids else {}

    enriched = []
    for row in ordered:
        status = str(row.get("LAST_STATUS") or "-").upper()
        progress_obj = progress.get(str(row.get("LAST_RUN_ID")))
        if progress_obj is None and status in ("RUNNING", "IN_PROGRESS", "EXECUTING"):
            progress_obj = {"total": None, "done": None, "running": None, "failed": None, "percent": None}

        enriched.append({
            "workflowId": row.get("WORKFLOW_ID"),
            "workflowGroup": row.get("WORKFLOW_GROUP") or "Ungrouped",
            "workflowName": row.get("WORKFLOW_NAME") or "",
            "workflowType": row.get("WORKFLOW_TYPE") or "DBT",
            "description": row.get("DESCRIPTION") or "",
            "workflowEnabled": bool(row.get("WORKFLOW_ENABLED", True)),
            "taskEnabled": bool(row.get("TASK_ENABLED", True)),
            "lastRunId": row.get("LAST_RUN_ID"),
            "lastStatus": row.get("LAST_STATUS") or "-",
            "lastStartTime": row.get("LAST_START_TIME"),
            "lastEndTime": row.get("LAST_END_TIME"),
            "lastDurationSeconds": row.get("LAST_DURATION_SECONDS"),
            "lastRequestedAt": row.get("LAST_REQUESTED_AT"),
            "lastRequestedBy": row.get("LAST_REQUESTED_BY"),
            "lastTriggerSource": row.get("LAST_TRIGGER_SOURCE"),
            "scheduleCron": row.get("SCHEDULE_CRON") or "-",
            "scheduleTimezone": row.get("SCHEDULE_TIMEZONE") or "UTC",
            "nextRunTime": next_run(row.get("SCHEDULE_CRON"), row.get("SCHEDULE_TIMEZONE") or "UTC") if bool(row.get("TASK_ENABLED", True)) else None,
            "indent": int(row.get("INDENT") or 0),
            "progress": progress_obj,
        })
    return enriched


def load_progress_for_runs(run_ids):
    if not run_ids or not config.PROGRESS_TABLE:
        return {}
    quoted = ", ".join([f"'{sql_escape(x)}'" for x in run_ids])
    q = f"""
    SELECT
      RUN_ID,
      COUNT(*) AS TOTAL,
      SUM(IFF(UPPER(STATUS) IN ('DONE','SUCCESS','SUCCEEDED','COMPLETED','OK'), 1, 0)) AS DONE,
      SUM(IFF(UPPER(STATUS) IN ('RUNNING','IN_PROGRESS','EXECUTING'), 1, 0)) AS RUNNING,
      SUM(IFF(UPPER(STATUS) IN ('FAILED','FAILURE','ERROR'), 1, 0)) AS FAILED
    FROM {config.PROGRESS_TABLE}
    WHERE RUN_ID IN ({quoted})
    GROUP BY RUN_ID
    """
    out = {}
    try:
        rows = normalize_rows(_query(q))
        for row in rows:
            total = int(row.get("TOTAL") or 0)
            done = int(row.get("DONE") or 0)
            failed = int(row.get("FAILED") or 0)
            running = int(row.get("RUNNING") or 0)
            percent = round((done / total) * 100) if total else None
            out[str(row.get("RUN_ID"))] = {"total": total, "done": done, "running": running, "failed": failed, "percent": percent}
    except Exception:
        return {}
    return out


def build_summary(workflows):
    statuses = [str(w.get("lastStatus") or "-").upper() for w in workflows]
    total = len(statuses)
    success = sum(s in ("SUCCESS", "SUCCEEDED", "COMPLETED", "OK") for s in statuses)
    failed = sum(s in ("FAILED", "FAILURE", "ERROR") for s in statuses)
    running = sum(s in ("RUNNING", "IN_PROGRESS", "EXECUTING", "INITIATING") for s in statuses)
    queued = sum(s in ("QUEUED", "PENDING", "REQUESTED", "SCHEDULED") for s in statuses)
    return {"total": total, "success": success, "failed": failed, "running": running, "queued": queued}


def _extract_run_id_from_call_result(rows):
    """Best-effort extraction of a RUN_ID from a stored procedure result."""
    for row in normalize_rows(rows or []):
        for value in row.values():
            if value is None:
                continue
            text = str(value)
            # Common procedure return styles: a plain UUID, JSON, or text containing a UUID.
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    for key in ("RUN_ID", "run_id", "runId"):
                        if parsed.get(key):
                            return str(parsed[key])
            except Exception:
                pass
            import re
            match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)
            if match:
                return match.group(0)
    return None


def latest_run_id_for_workflow(workflow_id):
    h_types = describe_table(config.T_HISTORY)
    order_expr = "COALESCE(REQUESTED_AT, START_TIME, END_TIME)" if "REQUESTED_AT" in h_types else "COALESCE(START_TIME, END_TIME)"
    try:
        rows = normalize_rows(_query(
            f"""
            SELECT RUN_ID
            FROM {config.T_HISTORY}
            WHERE WORKFLOW_ID = %(workflow_id)s
            ORDER BY {order_expr} DESC NULLS LAST, RUN_ID DESC
            LIMIT 1
            """,
            {"workflow_id": workflow_id},
        ))
        return str(rows[0].get("RUN_ID")) if rows and rows[0].get("RUN_ID") else None
    except Exception:
        return None


def _request_run_via_queue_insert(workflow_id, trigger_source="MANUAL", requested_by=None):
    """Original Streamlit-compatible queue insert path.

    Important: DB status remains QUEUED. The React UI may show INITIATING optimistically,
    but the dispatcher/procedures should see the same persisted status as the old Streamlit app.
    """
    h_types = describe_table(config.T_HISTORY)
    q_types = describe_table(config.T_QUEUE)
    run_id = str(uuid.uuid4())

    h_cols = ["RUN_ID", "WORKFLOW_ID", "STATUS"]
    h_vals = ["%(run_id)s", "%(workflow_id)s", "'QUEUED'"]
    params = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "trigger_source": trigger_source,
        "requested_by": requested_by or "",
    }

    if "TRIGGER_SOURCE" in h_types:
        h_cols.append("TRIGGER_SOURCE")
        h_vals.append("%(trigger_source)s")
    if "REQUESTED_AT" in h_types:
        h_cols.append("REQUESTED_AT")
        h_vals.append("SYSDATE()")
    if "REQUESTED_BY" in h_types:
        h_cols.append("REQUESTED_BY")
        h_vals.append("%(requested_by)s" if requested_by else "CURRENT_USER()")
    if "UPDATED_AT" in h_types:
        h_cols.append("UPDATED_AT")
        h_vals.append("SYSDATE()")

    _execute(f"INSERT INTO {config.T_HISTORY} ({', '.join(h_cols)}) VALUES ({', '.join(h_vals)})", params)

    if "WORKFLOW_ID" not in q_types or "RUN_ID" not in q_types:
        raise RuntimeError(f"{config.T_QUEUE} must have WORKFLOW_ID and RUN_ID columns")

    q_cols = ["WORKFLOW_ID", "RUN_ID"]
    q_vals = ["%(workflow_id)s", "%(run_id)s"]
    if "STATUS" in q_types:
        q_cols.append("STATUS")
        q_vals.append("'QUEUED'")
    if "TRIGGER_SOURCE" in q_types:
        q_cols.append("TRIGGER_SOURCE")
        q_vals.append("%(trigger_source)s")
    if "REQUESTED_AT" in q_types:
        q_cols.append("REQUESTED_AT")
        q_vals.append("SYSDATE()")
    if "REQUESTED_BY" in q_types:
        q_cols.append("REQUESTED_BY")
        q_vals.append("%(requested_by)s" if requested_by else "CURRENT_USER()")

    _execute(f"INSERT INTO {config.T_QUEUE} ({', '.join(q_cols)}) VALUES ({', '.join(q_vals)})", params)
    return run_id


def _request_run_via_procedure(workflow_id, trigger_source="MANUAL", requested_by=None):
    """Use the same stored procedure path that scheduled tasks use.

    The scheduler task in your Streamlit app calls:
      SP_WORKFLOW_REQUEST_RUN(workflow_id, 'SCHEDULED', NULL, 0, NULL)

    Manual runs should use the same path where possible, because it is the safest way
    to match your existing dispatcher/orchestration behavior.
    """
    params = {
        "workflow_id": workflow_id,
        "trigger_source": trigger_source,
        "requested_by": requested_by,
    }
    rows = _execute(
        f"CALL {config.DB}.{config.SCHEMA}.SP_WORKFLOW_REQUEST_RUN(%(workflow_id)s, %(trigger_source)s, %(requested_by)s, 0, NULL)",
        params,
    )
    return _extract_run_id_from_call_result(rows) or latest_run_id_for_workflow(workflow_id)


def request_run(workflow_id, trigger_source="MANUAL", requested_by=None):
    """Create/start a manual workflow run.

    Default behavior is the original Streamlit-compatible queue insert path.
    Set KUMO_MANUAL_RUN_MODE=procedure if you want to call SP_WORKFLOW_REQUEST_RUN first.
    """
    mode = getattr(config, "KUMO_MANUAL_RUN_MODE", "queue")
    if mode == "queue":
        return _request_run_via_queue_insert(workflow_id, trigger_source, requested_by)

    try:
        run_id = _request_run_via_procedure(workflow_id, trigger_source, requested_by)
        if run_id:
            return run_id
        # Procedure ran but did not return/record a visible run id. Use queue fallback as a safety net.
        return _request_run_via_queue_insert(workflow_id, trigger_source, requested_by)
    except Exception:
        # If the proc signature differs in an older environment, preserve the old Streamlit behavior.
        return _request_run_via_queue_insert(workflow_id, trigger_source, requested_by)


def task_name_for_workflow(workflow_id):
    return format_task_name(workflow_id, config.DB, config.SCHEMA)


def create_or_replace_sf_task(workflow_id, warehouse=None):
    warehouse = warehouse or config.DEFAULT_TASK_WAREHOUSE
    t_types = describe_table(config.T_TASKS)
    wf_types = describe_table(config.T_WORKFLOWS)

    sched_col = "SCHEDULE_CRON" if "SCHEDULE_CRON" in t_types else ("SCHEDULE" if "SCHEDULE" in t_types else None)
    tz_col = "SCHEDULE_TIMEZONE" if "SCHEDULE_TIMEZONE" in t_types else None
    task_enabled_col = "IS_ENABLED" if "IS_ENABLED" in t_types else None
    wf_enabled_expr = "w.IS_ENABLED" if "IS_ENABLED" in wf_types else "TRUE"

    if not sched_col:
        raise RuntimeError(f"{config.T_TASKS} must have SCHEDULE_CRON or SCHEDULE")

    row = _query(
        f"""
        SELECT
          t.{sched_col} AS CRON,
          {('t.' + tz_col) if tz_col else "'UTC'"} AS TZ,
          {('t.' + task_enabled_col) if task_enabled_col else 'TRUE'} AS SCHEDULE_ENABLED,
          {wf_enabled_expr} AS WORKFLOW_ENABLED
        FROM {config.T_TASKS} t
        JOIN {config.T_WORKFLOWS} w ON w.WORKFLOW_ID = t.WORKFLOW_ID
        WHERE t.WORKFLOW_ID = %(workflow_id)s
        """,
        {"workflow_id": workflow_id},
    )

    if not row:
        raise RuntimeError(f"No task/workflow row found for WORKFLOW_ID={workflow_id}")

    row = normalize_rows(row)[0]
    cron = (row.get("CRON") or "0 0 * * *").strip()
    tz = (row.get("TZ") or "UTC").strip()
    schedule_enabled = bool(row.get("SCHEDULE_ENABLED"))
    workflow_enabled = bool(row.get("WORKFLOW_ENABLED"))

    task_fqn = task_name_for_workflow(workflow_id)
    scheduler_fqn = f"{task_fqn}_SCHEDULER"

    try:
        _execute(f"DROP TASK IF EXISTS {task_fqn}")
    except Exception:
        pass

    if not schedule_enabled:
        _execute(f"DROP TASK IF EXISTS {scheduler_fqn}")
        return

    ddl = f"""
    CREATE OR REPLACE TASK {scheduler_fqn}
      WAREHOUSE = {warehouse}
      SCHEDULE = 'USING CRON {sql_escape(cron)} {sql_escape(tz)}'
      USER_TASK_TIMEOUT_MS = 21600000
    AS
      CALL {config.DB}.{config.SCHEMA}.SP_WORKFLOW_REQUEST_RUN('{sql_escape(workflow_id)}', 'SCHEDULED', NULL, 0, NULL)
    """
    _execute(ddl)
    _execute(f"ALTER TASK {scheduler_fqn} {'RESUME' if workflow_enabled else 'SUSPEND'}")


def _ensure_json_array_str(value):
    if value is None:
        return "[]"
    if isinstance(value, list):
        return json.dumps([str(x).strip() for x in value if str(x).strip()])
    s = str(value).strip()
    if not s:
        return "[]"
    if s.startswith("[") and "'" in s and '"' not in s:
        s = s.replace("'", '"')
    parsed = json.loads(s)
    if not isinstance(parsed, list):
        raise ValueError("Expected JSON array")
    return json.dumps([str(x).strip() for x in parsed if str(x).strip()])


def upsert_task(workflow_id, schedule_cron, schedule_timezone, schedule_enabled, on_success_json=None, on_fail_json=None):
    t_types = describe_table(config.T_TASKS)
    sched_col = "SCHEDULE_CRON" if "SCHEDULE_CRON" in t_types else ("SCHEDULE" if "SCHEDULE" in t_types else None)
    tz_col = "SCHEDULE_TIMEZONE" if "SCHEDULE_TIMEZONE" in t_types else None
    enabled_col = "IS_ENABLED" if "IS_ENABLED" in t_types else None

    if not sched_col:
        raise RuntimeError(f"{config.T_TASKS} must have SCHEDULE_CRON or SCHEDULE")

    on_success_json = _ensure_json_array_str(on_success_json)
    on_fail_json = _ensure_json_array_str(on_fail_json)
    exists = _query(f"SELECT COUNT(*) AS CNT FROM {config.T_TASKS} WHERE WORKFLOW_ID = %(workflow_id)s", {"workflow_id": workflow_id})[0]["CNT"] > 0

    if exists:
        sets = [f"{sched_col} = %(schedule_cron)s"]
        params = {"workflow_id": workflow_id, "schedule_cron": schedule_cron, "schedule_timezone": schedule_timezone, "on_success": on_success_json, "on_fail": on_fail_json}
        if tz_col:
            sets.append(f"{tz_col} = %(schedule_timezone)s")
        if enabled_col:
            sets.append(f"{enabled_col} = {'TRUE' if schedule_enabled else 'FALSE'}")
        if "ON_SUCCESS" in t_types:
            sets.append("ON_SUCCESS = PARSE_JSON(%(on_success)s)")
        if "ON_FAIL" in t_types:
            sets.append("ON_FAIL = PARSE_JSON(%(on_fail)s)")
        _execute(f"UPDATE {config.T_TASKS} SET {', '.join(sets)} WHERE WORKFLOW_ID = %(workflow_id)s", params)
    else:
        cols = ["WORKFLOW_ID", sched_col]
        vals = ["%(workflow_id)s", "%(schedule_cron)s"]
        params = {"workflow_id": workflow_id, "schedule_cron": schedule_cron, "schedule_timezone": schedule_timezone, "on_success": on_success_json, "on_fail": on_fail_json}
        if tz_col:
            cols.append(tz_col); vals.append("%(schedule_timezone)s")
        if enabled_col:
            cols.append(enabled_col); vals.append("TRUE" if schedule_enabled else "FALSE")
        if "ON_SUCCESS" in t_types:
            cols.append("ON_SUCCESS"); vals.append("PARSE_JSON(%(on_success)s)")
        if "ON_FAIL" in t_types:
            cols.append("ON_FAIL"); vals.append("PARSE_JSON(%(on_fail)s)")
        _execute(f"INSERT INTO {config.T_TASKS} ({', '.join(cols)}) VALUES ({', '.join(vals)})", params)

    create_or_replace_sf_task(workflow_id)


def _workflow_payload_defaults(payload):
    wtype = str(payload.get("workflowType") or payload.get("WORKFLOW_TYPE") or "DBT").strip().upper()
    if wtype not in ("DBT", "SQL"):
        wtype = "DBT"
    return {
        "workflowName": str(payload.get("workflowName") or "").strip(),
        "workflowGroup": str(payload.get("workflowGroup") or "").strip(),
        "workflowType": wtype,
        "workflowEnabled": bool(payload.get("workflowEnabled", True)),
        "description": str(payload.get("description") or "").strip(),
        "dbtCommand": str(payload.get("dbtCommand") or "").strip(),
        "sqlCommand": str(payload.get("sqlCommand") or "").strip(),
        "dbtTarget": str(payload.get("dbtTarget") or "").strip(),
        "dbtWorkspace": str(payload.get("dbtWorkspace") or "").strip(),
        "dbtProjectFqn": str(payload.get("dbtProjectFqn") or "").strip(),
        "scheduleCron": str(payload.get("scheduleCron") or "0 0 * * *").strip(),
        "scheduleTimezone": str(payload.get("scheduleTimezone") or "UTC").strip(),
        "taskEnabled": bool(payload.get("taskEnabled", False)),
        "onSuccess": payload.get("onSuccess") or [],
        "onFail": payload.get("onFail") or [],
        "notifications": payload.get("notifications") or {},
    }


def insert_workflow(payload):
    wf_types = describe_table(config.T_WORKFLOWS)
    t_types = describe_table(config.T_TASKS)
    data = _workflow_payload_defaults(payload)
    workflow_id = str(uuid.uuid4())

    if not data["workflowName"]:
        raise ValueError("Workflow name is required")
    if data["workflowType"] == "DBT" and not data["dbtCommand"]:
        raise ValueError("DBT workflow requires dbtCommand")
    if data["workflowType"] == "SQL" and not data["sqlCommand"]:
        raise ValueError("SQL workflow requires sqlCommand")

    cols = ["WORKFLOW_ID", "WORKFLOW_NAME", "WORKFLOW_GROUP", "IS_ENABLED"]
    vals = ["%(workflow_id)s", "%(workflow_name)s", "%(workflow_group)s", "TRUE" if data["workflowEnabled"] else "FALSE"]
    params = {
        "workflow_id": workflow_id,
        "workflow_name": data["workflowName"],
        "workflow_group": data["workflowGroup"],
        "workflow_type": data["workflowType"],
        "description": data["description"],
        "dbt_command": data["dbtCommand"] if data["workflowType"] == "DBT" else "",
        "sql_command": data["sqlCommand"] if data["workflowType"] == "SQL" else None,
        "dbt_target": data["dbtTarget"] if data["workflowType"] == "DBT" else None,
        "dbt_workspace": data["dbtWorkspace"] if data["workflowType"] == "DBT" else None,
        "dbt_project_fqn": data["dbtProjectFqn"] if data["workflowType"] == "DBT" else None,
    }
    if "WORKFLOW_TYPE" in wf_types:
        cols.append("WORKFLOW_TYPE"); vals.append("%(workflow_type)s")
    if "DBT_COMMAND" in wf_types:
        cols.append("DBT_COMMAND"); vals.append("%(dbt_command)s")
    if "SQL_COMMAND" in wf_types:
        cols.append("SQL_COMMAND"); vals.append("%(sql_command)s")
    if "DESCRIPTION" in wf_types:
        cols.append("DESCRIPTION"); vals.append("%(description)s")
    if "DBT_TARGET" in wf_types:
        cols.append("DBT_TARGET"); vals.append("%(dbt_target)s")
    if "DBT_WORKSPACE" in wf_types:
        cols.append("DBT_WORKSPACE"); vals.append("%(dbt_workspace)s")
    if "DBT_PROJECT_FQN" in wf_types:
        cols.append("DBT_PROJECT_FQN"); vals.append("%(dbt_project_fqn)s")

    _execute(f"INSERT INTO {config.T_WORKFLOWS} ({', '.join(cols)}) VALUES ({', '.join(vals)})", params)
    upsert_task(workflow_id, data["scheduleCron"], data["scheduleTimezone"], data["taskEnabled"], data["onSuccess"], data["onFail"])
    save_notifications(workflow_id, data["notifications"])
    return workflow_id


def update_workflow_detail(workflow_id, payload):
    wf_types = describe_table(config.T_WORKFLOWS)
    data = _workflow_payload_defaults(payload)

    if not data["workflowName"]:
        raise ValueError("Workflow name is required")
    if data["workflowType"] == "DBT" and not data["dbtCommand"]:
        raise ValueError("DBT workflow requires dbtCommand")
    if data["workflowType"] == "SQL" and not data["sqlCommand"]:
        raise ValueError("SQL workflow requires sqlCommand")
    if not data["workflowEnabled"]:
        data["taskEnabled"] = False

    sets = [
        "WORKFLOW_NAME = %(workflow_name)s",
        "WORKFLOW_GROUP = %(workflow_group)s",
        f"IS_ENABLED = {'TRUE' if data['workflowEnabled'] else 'FALSE'}",
    ]
    params = {
        "workflow_id": workflow_id,
        "workflow_name": data["workflowName"],
        "workflow_group": data["workflowGroup"],
        "workflow_type": data["workflowType"],
        "description": data["description"],
        "dbt_command": data["dbtCommand"] if data["workflowType"] == "DBT" else "",
        "sql_command": data["sqlCommand"] if data["workflowType"] == "SQL" else None,
        "dbt_target": data["dbtTarget"] if data["workflowType"] == "DBT" else None,
        "dbt_workspace": data["dbtWorkspace"] if data["workflowType"] == "DBT" else None,
        "dbt_project_fqn": data["dbtProjectFqn"] if data["workflowType"] == "DBT" else None,
    }
    if "WORKFLOW_TYPE" in wf_types:
        sets.append("WORKFLOW_TYPE = %(workflow_type)s")
    if "DESCRIPTION" in wf_types:
        sets.append("DESCRIPTION = %(description)s")
    if "DBT_COMMAND" in wf_types:
        sets.append("DBT_COMMAND = %(dbt_command)s")
    if "SQL_COMMAND" in wf_types:
        sets.append("SQL_COMMAND = %(sql_command)s")
    if "DBT_TARGET" in wf_types:
        sets.append("DBT_TARGET = %(dbt_target)s")
    if "DBT_WORKSPACE" in wf_types:
        sets.append("DBT_WORKSPACE = %(dbt_workspace)s")
    if "DBT_PROJECT_FQN" in wf_types:
        sets.append("DBT_PROJECT_FQN = %(dbt_project_fqn)s")
    if "UPDATED_AT" in wf_types:
        sets.append("UPDATED_AT = SYSDATE()")
    if "UPDATED_BY" in wf_types:
        sets.append("UPDATED_BY = CURRENT_USER()")

    _execute(f"UPDATE {config.T_WORKFLOWS} SET {', '.join(sets)} WHERE WORKFLOW_ID = %(workflow_id)s", params)
    upsert_task(workflow_id, data["scheduleCron"], data["scheduleTimezone"], data["taskEnabled"], data["onSuccess"], data["onFail"])
    save_notifications(workflow_id, data["notifications"])
    return get_workflow_detail(workflow_id)


def save_notifications(workflow_id, notifications):
    if not object_exists(config.T_NOTIFICATIONS):
        return
    n = notifications or {}
    params = {
        "workflow_id": workflow_id,
        "on_success_email": bool(n.get("onSuccessEmail", False)),
        "on_fail_email": bool(n.get("onFailEmail", True)),
        "success_group": str(n.get("successGroup") or ""),
        "fail_group": str(n.get("failGroup") or ""),
        "email_integration": str(n.get("emailIntegration") or "MY_EMAIL_INT"),
        "environment": str(n.get("environment") or "PROD"),
    }
    _execute(
        f"""
        MERGE INTO {config.T_NOTIFICATIONS} t
        USING (
          SELECT %(workflow_id)s AS WF_ID,
                 %(on_success_email)s AS OSE,
                 %(on_fail_email)s AS OFE,
                 %(success_group)s AS SR,
                 %(fail_group)s AS FR,
                 %(email_integration)s AS EI,
                 %(environment)s AS ENV
        ) s
        ON t.WORKFLOW_ID = s.WF_ID
        WHEN MATCHED THEN UPDATE SET
          ON_SUCCESS_EMAIL = s.OSE,
          ON_FAIL_EMAIL = s.OFE,
          SUCCESS_GROUP = s.SR,
          FAIL_GROUP = s.FR,
          EMAIL_INTEGRATION = s.EI,
          ENVIRONMENT = s.ENV,
          UPDATED_AT = SYSDATE()
        WHEN NOT MATCHED THEN INSERT
          (WORKFLOW_ID, ON_SUCCESS_EMAIL, ON_FAIL_EMAIL, SUCCESS_GROUP, FAIL_GROUP, EMAIL_INTEGRATION, ENVIRONMENT)
          VALUES (s.WF_ID, s.OSE, s.OFE, s.SR, s.FR, s.EI, s.ENV)
        """,
        params,
    )


def _admin_query(sql, params=None):
    """Use service context for app administration reads.

    The monitor itself can run under caller rights, but edit dialogs and action
    metadata should not hang or fail just because the browser user's role lacks
    metadata visibility on helper tables such as notifications or email groups.
    """
    if hasattr(sf, "query_service"):
        return sf.query_service(sql, params or {}, use_warehouse=True, include_context=True)
    return _query(sql, params or {})


def _admin_execute(sql, params=None):
    if hasattr(sf, "execute_service"):
        return sf.execute_service(sql, params or {}, use_warehouse=True, include_context=True)
    return _execute(sql, params or {})


def get_workflow_detail(workflow_id):
    wf_rows = normalize_rows(_admin_query(f"SELECT * FROM {config.T_WORKFLOWS} WHERE WORKFLOW_ID = %(workflow_id)s", {"workflow_id": workflow_id}))
    if not wf_rows:
        raise ValueError("Workflow not found")
    task_rows = normalize_rows(_admin_query(f"SELECT * FROM {config.T_TASKS} WHERE WORKFLOW_ID = %(workflow_id)s", {"workflow_id": workflow_id}))
    wf = wf_rows[0]
    task = task_rows[0] if task_rows else {}

    notif = {}
    try:
        rows = normalize_rows(_admin_query(f"SELECT * FROM {config.T_NOTIFICATIONS} WHERE WORKFLOW_ID = %(workflow_id)s", {"workflow_id": workflow_id}))
        notif = rows[0] if rows else {}
    except Exception:
        notif = {}

    options = normalize_rows(_admin_query(f"SELECT WORKFLOW_ID, WORKFLOW_GROUP, WORKFLOW_NAME FROM {config.T_WORKFLOWS} ORDER BY WORKFLOW_GROUP, WORKFLOW_NAME"))
    email_groups = []
    try:
        email_groups = [r.get("GROUP_NAME") for r in normalize_rows(_admin_query(f"SELECT GROUP_NAME FROM {config.DB}.{config.SCHEMA}.EMAIL_GROUPS ORDER BY GROUP_NAME")) if r.get("GROUP_NAME")]
    except Exception:
        pass

    return {
        "workflowId": workflow_id,
        "workflowName": wf.get("WORKFLOW_NAME") or "",
        "workflowGroup": wf.get("WORKFLOW_GROUP") or "",
        "workflowType": wf.get("WORKFLOW_TYPE") or "DBT",
        "workflowEnabled": bool(wf.get("IS_ENABLED", True)),
        "description": wf.get("DESCRIPTION") or "",
        "dbtCommand": wf.get("DBT_COMMAND") or "",
        "sqlCommand": wf.get("SQL_COMMAND") or "",
        "dbtProjectFqn": wf.get("DBT_PROJECT_FQN") or "",
        "dbtTarget": wf.get("DBT_TARGET") or "",
        "dbtWorkspace": wf.get("DBT_WORKSPACE") or "",
        "scheduleCron": task.get("SCHEDULE_CRON") or task.get("SCHEDULE") or "0 0 * * *",
        "scheduleTimezone": task.get("SCHEDULE_TIMEZONE") or "UTC",
        "taskEnabled": bool(task.get("IS_ENABLED", False)) if task else False,
        "onSuccess": parse_variant_array(task.get("ON_SUCCESS")),
        "onFail": parse_variant_array(task.get("ON_FAIL")),
        "notifications": {
            "onSuccessEmail": bool(notif.get("ON_SUCCESS_EMAIL", False)),
            "onFailEmail": bool(notif.get("ON_FAIL_EMAIL", True)),
            "successGroup": notif.get("SUCCESS_GROUP") or "",
            "failGroup": notif.get("FAIL_GROUP") or "",
            "emailIntegration": notif.get("EMAIL_INTEGRATION") or "MY_EMAIL_INT",
            "environment": notif.get("ENVIRONMENT") or "PROD",
        },
        "workflowOptions": [
            {"workflowId": r.get("WORKFLOW_ID"), "label": f"{r.get('WORKFLOW_GROUP') or 'Ungrouped'} / {r.get('WORKFLOW_NAME') or ''}"}
            for r in options if r.get("WORKFLOW_ID") != workflow_id
        ],
        "emailGroups": email_groups,
    }


def toggle_workflow(workflow_id, enabled):
    _execute(
        f"UPDATE {config.T_WORKFLOWS} SET IS_ENABLED = {'TRUE' if enabled else 'FALSE'} WHERE WORKFLOW_ID = %(workflow_id)s",
        {"workflow_id": workflow_id},
    )
    if not enabled:
        _execute(
            f"UPDATE {config.T_TASKS} SET IS_ENABLED = FALSE WHERE WORKFLOW_ID = %(workflow_id)s",
            {"workflow_id": workflow_id},
        )
    create_or_replace_sf_task(workflow_id)
    return {"workflowId": workflow_id, "workflowEnabled": enabled}


def toggle_schedule(workflow_id, enabled):
    if enabled:
        wf = normalize_rows(_query(f"SELECT IS_ENABLED FROM {config.T_WORKFLOWS} WHERE WORKFLOW_ID = %(workflow_id)s", {"workflow_id": workflow_id}))
        if wf and not bool(wf[0].get("IS_ENABLED")):
            raise ValueError("Cannot enable schedule when workflow is disabled")
    row = normalize_rows(_query(f"SELECT * FROM {config.T_TASKS} WHERE WORKFLOW_ID = %(workflow_id)s", {"workflow_id": workflow_id}))
    task = row[0] if row else {}
    upsert_task(
        workflow_id,
        task.get("SCHEDULE_CRON") or task.get("SCHEDULE") or "0 0 * * *",
        task.get("SCHEDULE_TIMEZONE") or "UTC",
        enabled,
        parse_variant_array(task.get("ON_SUCCESS")),
        parse_variant_array(task.get("ON_FAIL")),
    )
    return {"workflowId": workflow_id, "taskEnabled": enabled}


def delete_workflow(workflow_id):
    for table in [config.T_QUEUE, config.T_HISTORY, config.T_TASKS, config.T_WORKFLOWS]:
        if object_exists(table):
            _execute(f"DELETE FROM {table} WHERE WORKFLOW_ID = %(workflow_id)s", {"workflow_id": workflow_id})
    for task_fqn in [task_name_for_workflow(workflow_id), f"{task_name_for_workflow(workflow_id)}_SCHEDULER"]:
        try:
            _execute(f"DROP TASK IF EXISTS {task_fqn}")
        except Exception:
            pass
    return {"workflowId": workflow_id, "deleted": True}


def clone_workflow(workflow_id):
    detail = get_workflow_detail(workflow_id)
    detail["workflowName"] = f"{detail['workflowName']} (copy)"
    detail["workflowEnabled"] = False
    detail["taskEnabled"] = False
    new_id = insert_workflow(detail)
    return get_workflow_detail(new_id)


def load_dag_run(workflow_id):
    h = normalize_rows(_query(
        f"""
        SELECT RUN_ID, STATUS
        FROM {config.T_HISTORY}
        WHERE WORKFLOW_ID = %(workflow_id)s
          AND STATUS IN ('INITIATING','QUEUED','RUNNING','SUCCESS','FAILED','COMPLETED')
        ORDER BY COALESCE(START_TIME, REQUESTED_AT, END_TIME) DESC NULLS LAST
        LIMIT 1
        """,
        {"workflow_id": workflow_id},
    ))
    if not h:
        return {"workflowId": workflow_id, "run": None, "nodes": [], "edges": [], "errors": []}
    run = h[0]
    run_id = run.get("RUN_ID")
    progress_rows = []
    errors = []
    if config.PROGRESS_TABLE:
        try:
            progress_rows = normalize_rows(_query(
                f"""
                SELECT MODEL_NAME, MODEL_NAME_PARENT, STATUS, SRT
                FROM {config.PROGRESS_TABLE}
                WHERE RUN_ID = %(run_id)s
                ORDER BY SRT
                """,
                {"run_id": run_id},
            ))
        except Exception:
            progress_rows = []
    if config.RUN_LOG_TABLE:
        try:
            errors = normalize_rows(_query(
                f"""
                SELECT LOG_DTTM, ORIGIN, MESSAGE
                FROM {config.RUN_LOG_TABLE}
                WHERE RUN_ID = %(run_id)s
                  AND MESSAGE LIKE 'ERROR:%'
                ORDER BY LOG_DTTM
                """,
                {"run_id": run_id},
            ))
        except Exception:
            errors = []

    error_models = {str(e.get("ORIGIN")) for e in errors if e.get("ORIGIN")}
    nodes = []
    edges = []
    seen_edges = set()
    for row in progress_rows:
        model = str(row.get("MODEL_NAME") or "")
        parent = str(row.get("MODEL_NAME_PARENT") or "")
        status = str(row.get("STATUS") or "UNKNOWN")
        nodes.append({"id": model, "label": _short_model_name(model), "status": "ERROR" if model in error_models else status})
        if parent and parent not in (model, "None"):
            key = (parent, model)
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"source": parent, "target": model})
    return {"workflowId": workflow_id, "run": run, "nodes": nodes, "edges": edges, "errors": errors}


def _short_model_name(model):
    parts = str(model or "").replace("EDV__", "").replace("SDL_", "").split("__")
    short = parts[-1] if parts else str(model or "")
    return short[:32]
