import json
import logging
from datetime import datetime, timezone
from threading import Lock
from flask import Flask, jsonify, request, send_from_directory, g
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv

load_dotenv()

import config
import kumo_repository as repo
from monitor_cache import monitor_cache
from mock_data import MOCK_HISTORY, MOCK_MONITOR
import snowflake_client as sf

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)

monitor_cache.start()

_active_users_lock = Lock()
_active_users = {}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _user_key(session_info):
    user_name = str(session_info.get("userName") or "").strip()
    if user_name and user_name.upper() != "UNKNOWN":
        return user_name.upper()
    display_name = str(session_info.get("displayName") or "").strip()
    return display_name.upper() if display_name else "UNKNOWN"


def _register_active_user(session_info, source="session"):
    key = _user_key(session_info)
    if not key or key == "UNKNOWN":
        return None

    now = _now_iso()
    item = {
        "userName": session_info.get("userName") or key,
        "displayName": session_info.get("displayName") or session_info.get("userName") or key,
        "firstName": session_info.get("firstName") or "",
        "lastName": session_info.get("lastName") or "",
        "roleName": session_info.get("roleName") or "Unknown role",
        "warehouseName": session_info.get("warehouseName") or "Not selected",
        "mode": session_info.get("mode") or "unknown",
        "callerRightsActive": bool(session_info.get("callerRightsActive")),
        "callerTokenPresent": bool(session_info.get("callerTokenPresent")),
        "source": source,
    }

    with _active_users_lock:
        previous = _active_users.get(key, {})
        item["firstSeenAt"] = previous.get("firstSeenAt") or now
        item["lastSeenAt"] = now
        item["hitCount"] = int(previous.get("hitCount") or 0) + 1
        _active_users[key] = item

    return item


def _active_user_list():
    with _active_users_lock:
        rows = list(_active_users.values())
    rows.sort(key=lambda r: r.get("lastSeenAt") or "", reverse=True)
    return rows


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


def _user_agent():
    return request.headers.get("User-Agent", "")[:1000]


def _json_dumps(value):
    try:
        return json.dumps(value if value is not None else {}, default=str)
    except Exception:
        return json.dumps({"serializationError": str(value)[:1000]})


def _audit_enabled():
    return bool(getattr(config, "KUMO_AUDIT_ENABLED", True)) and sf.is_configured() and not config.USE_MOCK


def _persist_user_session(session_info, source="session", last_action=None):
    """Persist latest seen user data to KUMO APP_USER_SESSIONS.

    This uses the service context, not the caller context, so normal users do not
    need direct DML privileges on the application audit tables.
    """
    _register_active_user(session_info, source=source)
    if not _audit_enabled():
        return

    user_name = str(session_info.get("userName") or "").strip() or "UNKNOWN"
    if user_name.upper() == "UNKNOWN":
        return

    params = {
        "user_name": user_name,
        "display_name": session_info.get("displayName") or user_name,
        "first_name": session_info.get("firstName") or "",
        "last_name": session_info.get("lastName") or "",
        "role_name": session_info.get("roleName") or "Unknown role",
        "warehouse_name": session_info.get("warehouseName") or "Not selected",
        "session_mode": session_info.get("mode") or sf.connection_mode(),
        "caller_rights_active": bool(session_info.get("callerRightsActive")),
        "caller_token_present": bool(session_info.get("callerTokenPresent")),
        "last_action": last_action or source,
        "client_ip": _client_ip(),
        "user_agent": _user_agent(),
        "extra_json": _json_dumps({"source": source, "raw": session_info.get("raw") or {}}),
    }

    sql = f"""
    MERGE INTO {config.T_APP_USER_SESSIONS} t
    USING (
      SELECT
        %(user_name)s AS USER_NAME,
        %(display_name)s AS DISPLAY_NAME,
        %(first_name)s AS FIRST_NAME,
        %(last_name)s AS LAST_NAME,
        %(role_name)s AS ROLE_NAME,
        %(warehouse_name)s AS WAREHOUSE_NAME,
        %(session_mode)s AS SESSION_MODE,
        %(caller_rights_active)s AS CALLER_RIGHTS_ACTIVE,
        %(caller_token_present)s AS CALLER_TOKEN_PRESENT,
        %(last_action)s AS LAST_ACTION,
        %(client_ip)s AS CLIENT_IP,
        %(user_agent)s AS USER_AGENT,
        PARSE_JSON(%(extra_json)s) AS EXTRA
    ) s
    ON t.USER_NAME = s.USER_NAME
    WHEN MATCHED THEN UPDATE SET
      DISPLAY_NAME = s.DISPLAY_NAME,
      FIRST_NAME = s.FIRST_NAME,
      LAST_NAME = s.LAST_NAME,
      ROLE_NAME = s.ROLE_NAME,
      WAREHOUSE_NAME = s.WAREHOUSE_NAME,
      SESSION_MODE = s.SESSION_MODE,
      CALLER_RIGHTS_ACTIVE = s.CALLER_RIGHTS_ACTIVE,
      CALLER_TOKEN_PRESENT = s.CALLER_TOKEN_PRESENT,
      LAST_SEEN_AT = CURRENT_TIMESTAMP(),
      LAST_INTERACTION_AT = IFF(s.LAST_ACTION IS NULL, t.LAST_INTERACTION_AT, CURRENT_TIMESTAMP()),
      LAST_ACTION = COALESCE(s.LAST_ACTION, t.LAST_ACTION),
      HIT_COUNT = COALESCE(t.HIT_COUNT, 0) + 1,
      CLIENT_IP = s.CLIENT_IP,
      USER_AGENT = s.USER_AGENT,
      EXTRA = s.EXTRA,
      UPDATED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (
      USER_NAME, DISPLAY_NAME, FIRST_NAME, LAST_NAME, ROLE_NAME, WAREHOUSE_NAME,
      SESSION_MODE, CALLER_RIGHTS_ACTIVE, CALLER_TOKEN_PRESENT,
      FIRST_SEEN_AT, LAST_SEEN_AT, LAST_INTERACTION_AT, LAST_ACTION, HIT_COUNT,
      CLIENT_IP, USER_AGENT, EXTRA, UPDATED_AT
    ) VALUES (
      s.USER_NAME, s.DISPLAY_NAME, s.FIRST_NAME, s.LAST_NAME, s.ROLE_NAME, s.WAREHOUSE_NAME,
      s.SESSION_MODE, s.CALLER_RIGHTS_ACTIVE, s.CALLER_TOKEN_PRESENT,
      CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), s.LAST_ACTION, 1,
      s.CLIENT_IP, s.USER_AGENT, s.EXTRA, CURRENT_TIMESTAMP()
    )
    """
    try:
        sf.execute_service(sql, params=params, use_warehouse=True, include_context=True)
    except Exception as exc:
        app.logger.warning("Could not persist app user session: %s", exc)


def _load_persistent_users(limit=50):
    if not _audit_enabled():
        return _active_user_list()
    try:
        rows = sf.query_service(
            f"""
            SELECT
              USER_NAME,
              DISPLAY_NAME,
              FIRST_NAME,
              LAST_NAME,
              ROLE_NAME,
              WAREHOUSE_NAME,
              SESSION_MODE,
              CALLER_RIGHTS_ACTIVE,
              CALLER_TOKEN_PRESENT,
              FIRST_SEEN_AT,
              LAST_SEEN_AT,
              LAST_INTERACTION_AT,
              LAST_ACTION,
              HIT_COUNT,
              CLIENT_IP
            FROM {config.T_APP_USER_SESSIONS}
            ORDER BY LAST_SEEN_AT DESC NULLS LAST
            LIMIT {int(limit)}
            """,
            use_warehouse=True,
            include_context=True,
        )
        out = []
        for row in rows:
            r = {str(k).upper(): v for k, v in dict(row).items()}
            out.append({
                "userName": r.get("USER_NAME"),
                "displayName": r.get("DISPLAY_NAME") or r.get("USER_NAME"),
                "firstName": r.get("FIRST_NAME") or "",
                "lastName": r.get("LAST_NAME") or "",
                "roleName": r.get("ROLE_NAME") or "Unknown role",
                "warehouseName": r.get("WAREHOUSE_NAME") or "Not selected",
                "mode": r.get("SESSION_MODE") or "unknown",
                "callerRightsActive": bool(r.get("CALLER_RIGHTS_ACTIVE")),
                "callerTokenPresent": bool(r.get("CALLER_TOKEN_PRESENT")),
                "firstSeenAt": r.get("FIRST_SEEN_AT"),
                "lastSeenAt": r.get("LAST_SEEN_AT"),
                "lastInteractionAt": r.get("LAST_INTERACTION_AT"),
                "lastAction": r.get("LAST_ACTION") or "",
                "hitCount": int(r.get("HIT_COUNT") or 0),
                "clientIp": r.get("CLIENT_IP") or "",
            })
        return out
    except Exception as exc:
        app.logger.warning("Could not load persistent app users: %s", exc)
        return _active_user_list()


def _record_interaction(action, actor=None, entity_type=None, entity_id=None, workflow_id=None,
                        run_id=None, status="SUCCESS", success=True, error_message=None,
                        payload=None, response=None):
    """Write one audit row to APP_USER_INTERACTIONS and update APP_USER_SESSIONS."""
    if not actor:
        actor = _actor_context()

    _persist_user_session(actor, source="interaction", last_action=action)

    if not _audit_enabled():
        return

    user_name = str(actor.get("userName") or "UNKNOWN")
    params = {
        "user_name": user_name,
        "display_name": actor.get("displayName") or user_name,
        "first_name": actor.get("firstName") or "",
        "last_name": actor.get("lastName") or "",
        "role_name": actor.get("roleName") or "Unknown role",
        "warehouse_name": actor.get("warehouseName") or "Not selected",
        "session_mode": actor.get("mode") or sf.connection_mode(),
        "caller_rights_active": bool(actor.get("callerRightsActive")),
        "caller_token_present": bool(actor.get("callerTokenPresent")),
        "app_version": getattr(config, "KUMO_APP_VERSION", "dev"),
        "request_method": request.method,
        "request_path": request.path,
        "action": action,
        "entity_type": entity_type or "APP",
        "entity_id": entity_id or workflow_id or "",
        "workflow_id": workflow_id or "",
        "run_id": run_id or "",
        "status": status,
        "success": bool(success),
        "http_status": 200 if success else 500,
        "error_message": str(error_message or "")[:8000],
        "payload_json": _json_dumps(payload or {}),
        "response_json": _json_dumps(response or {}),
        "client_ip": _client_ip(),
        "user_agent": _user_agent(),
    }

    sql = f"""
    INSERT INTO {config.T_APP_USER_INTERACTIONS} (
      INTERACTION_ID, EVENT_TS,
      USER_NAME, DISPLAY_NAME, FIRST_NAME, LAST_NAME, ROLE_NAME, WAREHOUSE_NAME,
      SESSION_MODE, CALLER_RIGHTS_ACTIVE, CALLER_TOKEN_PRESENT, APP_VERSION,
      REQUEST_METHOD, REQUEST_PATH, ACTION, ENTITY_TYPE, ENTITY_ID,
      WORKFLOW_ID, RUN_ID, STATUS, SUCCESS, HTTP_STATUS, ERROR_MESSAGE,
      PAYLOAD, RESPONSE, CLIENT_IP, USER_AGENT
    )
    SELECT
      UUID_STRING(), CURRENT_TIMESTAMP(),
      %(user_name)s, %(display_name)s, %(first_name)s, %(last_name)s, %(role_name)s, %(warehouse_name)s,
      %(session_mode)s, %(caller_rights_active)s, %(caller_token_present)s, %(app_version)s,
      %(request_method)s, %(request_path)s, %(action)s, %(entity_type)s, %(entity_id)s,
      %(workflow_id)s, %(run_id)s, %(status)s, %(success)s, %(http_status)s, %(error_message)s,
      PARSE_JSON(%(payload_json)s), PARSE_JSON(%(response_json)s), %(client_ip)s, %(user_agent)s
    """
    try:
        sf.execute_service(sql, params=params, use_warehouse=True, include_context=True)
    except Exception as exc:
        app.logger.warning("Could not persist app interaction audit row: %s", exc)


def _json_error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


@app.before_request
def bind_request_context():
    ingress_token = request.headers.get("Sf-Context-Current-User-Token")
    g.sf_token_handle = sf.set_ingress_user_token(ingress_token)
    g.caller_token_present = bool(ingress_token)
    app.logger.info(
        "REQUEST method=%s path=%s caller_token_present=%s",
        request.method,
        request.path,
        bool(ingress_token),
    )


@app.after_request
def log_response(response):
    app.logger.info(
        "RESPONSE method=%s path=%s status=%s caller_token_present=%s",
        request.method,
        request.path,
        response.status_code,
        bool(getattr(g, "caller_token_present", False)),
    )
    return response


@app.teardown_request
def release_request_context(exception=None):
    token_handle = getattr(g, "sf_token_handle", None)
    if token_handle is not None:
        sf.reset_ingress_user_token(token_handle)


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "app": "KUMO Monitor",
        "mock": bool(config.USE_MOCK),
        "snowflakeConfigured": sf.is_configured(),
        "snowflakeConnectionMode": sf.connection_mode(),
        "callerTokenPresent": sf.caller_token_present(),
        "refreshSeconds": config.REFRESH_SECONDS,
        "db": config.DB,
        "schema": config.SCHEMA,
    })


@app.route("/api/session")
def session_context():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({
            "ok": True,
            "displayName": "Andreas Larsson",
            "firstName": "Andreas",
            "lastName": "Larsson",
            "userName": "ANDREAS",
            "roleName": "KUMO_ADMIN_ROLE",
            "warehouseName": config.SNOWFLAKE_WAREHOUSE or config.DEFAULT_TASK_WAREHOUSE,
            "mode": "mock",
            "callerRightsActive": False,
            "callerTokenPresent": False,
        })
    try:
        session_info = sf.session_context()
        _persist_user_session(session_info, source="session", last_action="SESSION_REFRESH")
        _record_interaction("SESSION_REFRESH", actor=session_info, entity_type="SESSION", status="SUCCESS", success=True)
        return jsonify({"ok": True, **session_info, "activeUsers": _load_persistent_users()})
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
            "displayName": "KUMO user",
            "firstName": "",
            "lastName": "",
            "userName": "UNKNOWN",
            "roleName": "Unknown role",
            "warehouseName": config.SNOWFLAKE_WAREHOUSE or "",
            "mode": sf.connection_mode(),
            "callerRightsActive": sf.connection_mode() == "spcs-caller-oauth",
            "callerTokenPresent": sf.caller_token_present(),
        }), 200


@app.route("/api/users/active")
def active_users():
    users = _load_persistent_users()
    return jsonify({
        "ok": True,
        "source": "snowflake" if _audit_enabled() else "memory",
        "users": users,
        "count": len(users),
    })


@app.route("/api/snowflake/ping")
def snowflake_ping():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": False, "mode": sf.connection_mode(), "error": "Snowflake is not configured or mock mode is enabled"}), 200
    try:
        return jsonify({"ok": True, "mode": sf.connection_mode(), "snowflake": sf.ping()})
    except Exception as exc:
        return jsonify({"ok": False, "mode": sf.connection_mode(), "error": str(exc)}), 200


@app.route("/api/monitor")
def monitor():
    return jsonify(monitor_cache.get())


@app.route("/api/monitor/refresh", methods=["POST"])
def refresh_monitor():
    return jsonify(monitor_cache.refresh(force=True))


def _actor_context():
    if config.USE_MOCK or not sf.is_configured():
        return {
            "displayName": "Andreas Larsson",
            "userName": "ANDREAS",
            "roleName": "KUMO_ADMIN_ROLE",
            "callerRightsActive": False,
        }
    try:
        actor = sf.session_context()
        _register_active_user(actor, source="action")
        return actor
    except Exception as exc:
        app.logger.warning("Could not resolve actor context: %s", exc)
        return {
            "displayName": "KUMO user",
            "userName": "UNKNOWN",
            "roleName": "Unknown role",
            "callerRightsActive": sf.connection_mode() == "spcs-caller-oauth",
        }


@app.route("/api/workflows/<workflow_id>/run", methods=["POST"])
def run_workflow(workflow_id):
    payload = request.get_json(silent=True) or {}
    actor = _actor_context()
    requested_by = (
        payload.get("requestedBy")
        or actor.get("displayName")
        or actor.get("userName")
        or "UNKNOWN"
    )

    app.logger.info(
        "KUMO_ACTION action=run_workflow workflow_id=%s actor=%s user=%s role=%s caller_rights=%s",
        workflow_id,
        actor.get("displayName"),
        actor.get("userName"),
        actor.get("roleName"),
        actor.get("callerRightsActive"),
    )

    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "runId": "mock-run", "actor": actor, "message": "Mock mode: run request accepted"}
        _record_interaction("RUN_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, run_id="mock-run", payload=payload, response=response)
        return jsonify(response)
    try:
        run_id = repo.request_run(
            workflow_id=workflow_id,
            trigger_source=payload.get("triggerSource", "MANUAL"),
            requested_by=requested_by,
        )
        monitor_cache.refresh(force=True)
        response = {"ok": True, "runId": run_id, "actor": actor}
        _record_interaction("RUN_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, run_id=run_id, payload=payload, response=response)
        return jsonify(response)
    except Exception as exc:
        _record_interaction("RUN_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload=payload)
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>")
def workflow_detail(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        workflow = next((w for w in MOCK_MONITOR["workflows"] if w["workflowId"] == workflow_id), None)
        if not workflow:
            return _json_error("Workflow not found", 404)
        return jsonify({
            "ok": True,
            "workflowId": workflow_id,
            "workflowName": workflow["workflowName"],
            "workflowGroup": workflow["workflowGroup"],
            "workflowType": workflow["workflowType"],
            "workflowEnabled": workflow["workflowEnabled"],
            "description": "Mock workflow",
            "dbtCommand": "dbt build --select tag:daily" if workflow["workflowType"] == "DBT" else "",
            "sqlCommand": "select 1" if workflow["workflowType"] == "SQL" else "",
            "dbtProjectFqn": "KUMO_TST.META.DBT_PROJECT",
            "dbtTarget": "prod",
            "scheduleCron": workflow["scheduleCron"],
            "scheduleTimezone": workflow["scheduleTimezone"],
            "taskEnabled": workflow["taskEnabled"],
            "onSuccess": [],
            "onFail": [],
            "notifications": {"onSuccessEmail": False, "onFailEmail": True, "successGroup": "", "failGroup": "Ops", "emailIntegration": "MY_EMAIL_INT", "environment": "PROD"},
            "workflowOptions": [{"workflowId": w["workflowId"], "label": f"{w['workflowGroup']} / {w['workflowName']}"} for w in MOCK_MONITOR["workflows"] if w["workflowId"] != workflow_id],
            "emailGroups": ["Ops", "Data Platform"],
        })
    actor = _actor_context()
    try:
        result = {"ok": True, **repo.get_workflow_detail(workflow_id)}
        _record_interaction("VIEW_WORKFLOW_DETAIL", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response={"ok": True})
        return jsonify(result)
    except Exception as exc:
        _record_interaction("VIEW_WORKFLOW_DETAIL", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc))
        return _json_error(exc, 404)


@app.route("/api/workflows/<workflow_id>", methods=["PATCH"])
def update_workflow(workflow_id):
    payload = request.get_json(silent=True) or {}
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "message": "Mock mode: workflow update accepted"}
        _record_interaction("UPDATE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    try:
        result = repo.update_workflow_detail(workflow_id, payload)
        monitor_cache.refresh(force=True)
        response = {"ok": True, "workflow": result}
        _record_interaction("UPDATE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response={"ok": True})
        return jsonify(response)
    except Exception as exc:
        _record_interaction("UPDATE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload=payload)
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/clone", methods=["POST"])
def clone_workflow(workflow_id):
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": "mock-clone", "message": "Mock mode: clone accepted"}
        _record_interaction("CLONE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response=response)
        return jsonify(response)
    try:
        result = repo.clone_workflow(workflow_id)
        monitor_cache.refresh(force=True)
        response = {"ok": True, "workflow": result}
        _record_interaction("CLONE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response={"ok": True, "newWorkflow": result})
        return jsonify(response)
    except Exception as exc:
        _record_interaction("CLONE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc))
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>", methods=["DELETE"])
def delete_workflow(workflow_id):
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "message": "Mock mode: delete accepted"}
        _record_interaction("DELETE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response=response)
        return jsonify(response)
    try:
        result = repo.delete_workflow(workflow_id)
        monitor_cache.refresh(force=True)
        response = {"ok": True, **result}
        _record_interaction("DELETE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response=response)
        return jsonify(response)
    except Exception as exc:
        _record_interaction("DELETE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc))
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/workflow-enabled", methods=["POST"])
def workflow_enabled(workflow_id):
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "workflowEnabled": enabled}
        _record_interaction("TOGGLE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    try:
        result = repo.toggle_workflow(workflow_id, enabled)
        monitor_cache.refresh(force=True)
        response = {"ok": True, **result}
        _record_interaction("TOGGLE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    except Exception as exc:
        _record_interaction("TOGGLE_WORKFLOW", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload=payload)
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/schedule-enabled", methods=["POST"])
def schedule_enabled(workflow_id):
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    actor = _actor_context()
    if config.USE_MOCK or not sf.is_configured():
        response = {"ok": True, "workflowId": workflow_id, "taskEnabled": enabled}
        _record_interaction("TOGGLE_SCHEDULE", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    try:
        result = repo.toggle_schedule(workflow_id, enabled)
        monitor_cache.refresh(force=True)
        response = {"ok": True, **result}
        _record_interaction("TOGGLE_SCHEDULE", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload=payload, response=response)
        return jsonify(response)
    except Exception as exc:
        _record_interaction("TOGGLE_SCHEDULE", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload=payload)
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/history")
def workflow_history(workflow_id):
    limit = request.args.get("limit", "100")
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": [r for r in MOCK_HISTORY if r.get("WORKFLOW_ID") == workflow_id]})
    actor = _actor_context()
    try:
        rows = repo.load_workflow_history(workflow_id, limit)
        _record_interaction("VIEW_WORKFLOW_HISTORY", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, payload={"limit": limit}, response={"rowCount": len(rows)})
        return jsonify({"ok": True, "source": "snowflake", "rows": rows})
    except Exception as exc:
        _record_interaction("VIEW_WORKFLOW_HISTORY", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc), payload={"limit": limit})
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/dag")
def workflow_dag(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "run": {"RUN_ID": "mock-run", "STATUS": "RUNNING"}, "nodes": [], "edges": [], "errors": []})
    actor = _actor_context()
    try:
        result = repo.load_dag_run(workflow_id)
        _record_interaction("VIEW_DAG_RUN", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, response={"ok": True})
        return jsonify({"ok": True, **result})
    except Exception as exc:
        _record_interaction("VIEW_DAG_RUN", actor=actor, entity_type="WORKFLOW", entity_id=workflow_id, workflow_id=workflow_id, status="FAILED", success=False, error_message=str(exc))
        return _json_error(exc, 500)


@app.route("/api/history")
def history():
    limit = request.args.get("limit", "200")
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": MOCK_HISTORY})
    try:
        rows = repo.load_history(limit)
        return jsonify({"ok": True, "source": "snowflake", "rows": rows})
    except Exception as exc:
        app.logger.exception("Failed to load history")
        return jsonify({"ok": False, "source": "error", "error": str(exc), "rows": []}), 500


@app.route("/api/notifications")
def notifications():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"source": "mock", "rows": []})
    try:
        rows = sf.query_service(f"SELECT * FROM {config.T_NOTIFICATIONS} ORDER BY WORKFLOW_ID", use_warehouse=True, include_context=True)
        from utils import normalize_rows
        return jsonify({"ok": True, "source": "snowflake", "rows": normalize_rows(rows)})
    except Exception as exc:
        return jsonify({"source": "error", "error": str(exc), "rows": []}), 200


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": error.description, "type": error.__class__.__name__}), error.code
        return error

    app.logger.exception("Unhandled application error")
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": str(error), "type": error.__class__.__name__}), 500
    return jsonify({"ok": False, "error": str(error), "type": error.__class__.__name__}), 500


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "API route not found", "path": request.path}), 404
    return send_from_directory(app.static_folder, "index.html")
