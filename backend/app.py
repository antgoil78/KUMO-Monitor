import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
CORS(app)


@app.get("/api/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "simple-fullstack-docker-app",
            "port": int(os.getenv("PORT", "5000")),
        }
    )


@app.get("/api/config")
def config():
    """Return safe config diagnostics without leaking secret values."""
    return jsonify(
        {
            "snowflake_user_is_set": bool(os.getenv("SNOWFLAKE_USER")),
            "snowflake_password_is_set": bool(os.getenv("SNOWFLAKE_PASSWORD")),
        }
    )


@app.get("/")
def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return send_from_directory(STATIC_DIR, "index.html")
    return jsonify(
        {
            "message": "Frontend build not found. Run npm run build in frontend or build the Docker image."
        }
    )


@app.errorhandler(404)
def not_found(_error):
    """Support browser refresh for frontend routes while keeping API 404s as JSON."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return send_from_directory(STATIC_DIR, "index.html")
    return jsonify({"error": "not found"}), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
