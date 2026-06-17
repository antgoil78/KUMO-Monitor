import logging
import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

import config
import kumo_repository as repo
from monitor_cache import monitor_cache
from mock_data import MOCK_HISTORY
import snowflake_client as sf

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)

monitor_cache.start()


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
        "refreshSeconds": config.REFRESH_SECONDS,
        "db": config.DB,
        "schema": config.SCHEMA,
    })


@app.route("/api/monitor")
def monitor():
    return jsonify(monitor_cache.get())


@app.route("/api/monitor/refresh", methods=["POST"])
def refresh_monitor():
    return jsonify(monitor_cache.refresh(force=True))


@app.route("/api/workflows/<workflow_id>/run", methods=["POST"])
def run_workflow(workflow_id):
    if config.USE_MOCK or not sf.is_configured():
        return jsonify({"ok": True, "runId": "mock-run", "message": "Mock mode: run request accepted"})
    payload = request.get_json(silent=True) or {}
    run_id = repo.request_run(
        workflow_id=workflow_id,
        trigger_source=payload.get("triggerSource", "MANUAL"),
        requested_by=payload.get("requestedBy"),
    )
    monitor_cache.refresh(force=True)
    return jsonify({"ok": True, "runId": run_id})


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
