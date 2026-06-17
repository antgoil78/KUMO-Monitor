import uuid
from datetime import datetime, timezone

import config
import snowflake_client as sf
from utils import normalize_rows, row_get, sql_escape, parse_variant_array, next_run

_describe_cache = {}


def describe_table(fqn):
    if fqn in _describe_cache:
        return _describe_cache[fqn]
    rows = sf.query(f"DESC TABLE {fqn}")
    out = {}
    for row in rows:
        name = row_get(row, "name", "NAME")
        typ = row_get(row, "type", "TYPE")
        if name:
            out[str(name).upper()] = str(typ)
    _describe_cache[fqn] = out
    return out


def load_history(limit=200):
    limit = max(1, min(int(limit or 200), 2000))
    h_types = describe_table(config.T_HISTORY)
    order_expr = "COALESCE(REQUESTED_AT, START_TIME, END_TIME)" if "REQUESTED_AT" in h_types else "COALESCE(START_TIME, END_TIME)"
    rows = sf.query(f"SELECT * FROM {config.T_HISTORY} ORDER BY {order_expr} DESC NULLS LAST LIMIT {limit}")
    return normalize_rows(rows)


def load_tasks():
    try:
        return normalize_rows(sf.query(f"SELECT * FROM {config.T_TASKS}"))
    except Exception:
        return []


def get_engine_state():
    def rdict(row):
        return {str(k).upper(): v for k, v in dict(row).items()}

    task_name = "TASK_WF_MASTER_DISPATCHER"
    for pattern in (f"{task_name}%", f"%{task_name}%"):
        for scope in (f"SCHEMA {config.DB}.{config.SCHEMA}", f"DATABASE {config.DB}", "ACCOUNT"):
            try:
                rows = sf.query(f"SHOW TASKS LIKE '{pattern}' IN {scope}")
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
    rows = normalize_rows(sf.query(q))
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
        rows = normalize_rows(sf.query(q))
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
    running = sum(s in ("RUNNING", "IN_PROGRESS", "EXECUTING") for s in statuses)
    queued = sum(s in ("QUEUED", "PENDING", "REQUESTED", "SCHEDULED") for s in statuses)
    return {"total": total, "success": success, "failed": failed, "running": running, "queued": queued}


def request_run(workflow_id, trigger_source="MANUAL", requested_by=None):
    h_types = describe_table(config.T_HISTORY)
    q_types = describe_table(config.T_QUEUE)
    run_id = str(uuid.uuid4())

    h_cols = ["RUN_ID", "WORKFLOW_ID", "STATUS"]
    h_vals = ["%(run_id)s", "%(workflow_id)s", "'QUEUED'"]
    params = {"run_id": run_id, "workflow_id": workflow_id, "trigger_source": trigger_source, "requested_by": requested_by or ""}

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

    sf.execute(f"INSERT INTO {config.T_HISTORY} ({', '.join(h_cols)}) VALUES ({', '.join(h_vals)})", params)

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

    sf.execute(f"INSERT INTO {config.T_QUEUE} ({', '.join(q_cols)}) VALUES ({', '.join(q_vals)})", params)
    return run_id
