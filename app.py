import logging
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
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


def _json_error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


@app.before_request
def log_request():
    app.logger.info("REQUEST method=%s path=%s", request.method, request.path)


@app.after_request
def log_response(response):
    app.logger.info("RESPONSE method=%s path=%s status=%s", request.method, request.path, response.status_code)
    return response


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "app": "KUMO Monitor",
        "mock": bool(config.USE_MOCK),
        "snowflakeConfigured": sf.is_configured(),
        "snowflakeConnectionMode": sf.connection_mode(),
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
        })
    try:
        return jsonify({"ok": True, **sf.session_context()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "displayName": "KUMO user", "roleName": "Unknown role"}), 200


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


@app.route("/api/workflows/<workflow_id>/run", methods=["POST"])
def run_workflow(workflow_id):
    payload = request.get_json(silent=True) or {}
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "runId": "mock-run", "message": "Mock mode: run request accepted"})
    try:
        run_id = repo.request_run(
            workflow_id=workflow_id,
            trigger_source=payload.get("triggerSource", "MANUAL"),
            requested_by=payload.get("requestedBy"),
        )
        monitor_cache.refresh(force=True)
        return jsonify({"ok": True, "runId": run_id})
    except Exception as exc:
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
    try:
        return jsonify({"ok": True, **repo.get_workflow_detail(workflow_id)})
    except Exception as exc:
        return _json_error(exc, 404)


@app.route("/api/workflows/<workflow_id>", methods=["PATCH"])
def update_workflow(workflow_id):
    payload = request.get_json(silent=True) or {}
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "workflowId": workflow_id, "message": "Mock mode: workflow update accepted"})
    try:
        result = repo.update_workflow_detail(workflow_id, payload)
        monitor_cache.refresh(force=True)
        return jsonify({"ok": True, "workflow": result})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/clone", methods=["POST"])
def clone_workflow(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "workflowId": "mock-clone", "message": "Mock mode: clone accepted"})
    try:
        result = repo.clone_workflow(workflow_id)
        monitor_cache.refresh(force=True)
        return jsonify({"ok": True, "workflow": result})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>", methods=["DELETE"])
def delete_workflow(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "workflowId": workflow_id, "message": "Mock mode: delete accepted"})
    try:
        result = repo.delete_workflow(workflow_id)
        monitor_cache.refresh(force=True)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/workflow-enabled", methods=["POST"])
def workflow_enabled(workflow_id):
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "workflowId": workflow_id, "workflowEnabled": enabled})
    try:
        result = repo.toggle_workflow(workflow_id, enabled)
        monitor_cache.refresh(force=True)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/schedule-enabled", methods=["POST"])
def schedule_enabled(workflow_id):
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "workflowId": workflow_id, "taskEnabled": enabled})
    try:
        result = repo.toggle_schedule(workflow_id, enabled)
        monitor_cache.refresh(force=True)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/history")
def workflow_history(workflow_id):
    limit = request.args.get("limit", "100")
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "source": "mock", "rows": [r for r in MOCK_HISTORY if r.get("WORKFLOW_ID") == workflow_id]})
    try:
        return jsonify({"ok": True, "source": "snowflake", "rows": repo.load_workflow_history(workflow_id, limit)})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/workflows/<workflow_id>/dag")
def workflow_dag(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "run": {"RUN_ID": "mock-run", "STATUS": "RUNNING"}, "nodes": [], "edges": [], "errors": []})
    try:
        return jsonify({"ok": True, **repo.load_dag_run(workflow_id)})
    except Exception as exc:
        return _json_error(exc, 500)


@app.route("/api/history")
def history():
    limit = request.args.get("limit", "200")
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"source": "mock", "rows": MOCK_HISTORY})
    return jsonify({"source": "snowflake", "rows": repo.load_history(limit)})


@app.route("/api/notifications")
def notifications():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"source": "mock", "rows": []})
    try:
        rows = sf.query(f"SELECT * FROM {config.T_NOTIFICATIONS} ORDER BY WORKFLOW_ID")
        from utils import normalize_rows
        return jsonify({"source": "snowflake", "rows": normalize_rows(rows)})
    except Exception as exc:
        return jsonify({"source": "error", "error": str(exc), "rows": []}), 200


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "API route not found", "path": request.path}), 404
    return send_from_directory(app.static_folder, "index.html")
