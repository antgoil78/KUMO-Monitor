"""Read-only HTTP checks for the DEV readiness release and verified PAAR load.

Run from inside SPCS against the deployed monitor's internal endpoint. The
PAAR assertions describe the 20 October verification snapshot. No valid load,
readiness, workflow, or reload request is submitted. Output contains checks
and aggregate counts, never file contents or authentication tokens.
"""

import collections
import json
import os
import re
import sys
import time

import requests


BASE_URL = os.environ["KUMO_SMOKE_BASE_URL"].rstrip("/")
EXPECTED_IMAGE = os.environ["KUMO_SMOKE_IMAGE_VERSION"]
EXPECTED_HISTORY_ROWS = int(os.environ["KUMO_SMOKE_HISTORY_ROWS"])
results = []
session = requests.Session()
session.trust_env = False


def check(name, passed, **details):
    result = {"name": name, "passed": bool(passed), **details}
    results.append(result)
    print("CHECK " + json.dumps(result), flush=True)


def request(method, path, expected=200, **kwargs):
    started = time.monotonic()
    response = session.request(
        method, BASE_URL + path, timeout=(10, 90), allow_redirects=False, **kwargs
    )
    check(
        method + " " + path,
        response.status_code == expected,
        status=response.status_code,
        expected_status=expected,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    response.raise_for_status() if expected < 400 else None
    return response


def api(path):
    payload = request("GET", path).json()
    check(path + " uses live Snowflake", payload.get("ok") and payload.get("source") == "snowflake")
    return payload


def run():
    health = request("GET", "/api/health").json()
    context = health.get("objectContext", {})
    check("DEV service context", health.get("ok") and health.get("environment") == "DEV"
          and health.get("db") == "KUMO_DEV" and not health.get("mock"))
    check("Published image is running", health.get("imageVersion") == EXPECTED_IMAGE,
          image_version=health.get("imageVersion"))
    check("SPCS OAuth available", health.get("snowflakeConfigured")
          and health.get("snowflakeConnectionMode") == "spcs-service-oauth")
    check("Batch control readiness selected", context.get("limReadinessSource") == "BATCH_CONTROL"
          and context.get("limDatabase") == "KUMO_DEV"
          and context.get("rawLimMetaTable") == "KUMO_DEV.MONITOR_META.RAW_LIM_META")

    html = request("GET", "/").text
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    check("React bundle referenced", bool(scripts))
    if scripts:
        bundle = request("GET", scripts[0]).text
        check("Updated readiness UI shipped", "BATCH_CONTROL" in bundle
              and "Register nonempty payload files" in bundle)

    subjects = api("/api/file-ingestion/reload/subject-areas")
    check("PAAR discovered in DEV RAW", "PAAR" in subjects.get("subjectAreas", []),
          subject_count=len(subjects.get("subjectAreas", [])))
    overview = api("/api/file-ingestion")
    check("PAAR appears in overview", any(row.get("PKG_GROUP_NAME") == "PKG_PAAR_1"
          for row in overview.get("overview", [])))

    raw = api("/api/file-ingestion/PKG_PAAR_1/raw")
    rows = raw.get("rows", [])
    accepted = [row for row in rows if row.get("DW_READY_TO_LOAD_FL") is True]
    nonempty = [row for row in accepted if row.get("FILE_TYPE") == "PAYLOAD"
                and (row.get("DATA_ROWS") or 0) > 0]
    check("Accepted PAAR files preserve boundary", len(accepted) == 230
          and all(str(row.get("DLVY_END_DATE"))[:10] <= "2026-03-10" for row in accepted),
          accepted_files=len(accepted), metadata_files=len(rows))
    check("Registered nonempty payloads agree with batch", len(nonempty) == 8
          and sum(row.get("DATA_ROWS") or 0 for row in nonempty) == 29,
          nonempty_files=len(nonempty), data_rows=sum(row.get("DATA_ROWS") or 0 for row in nonempty))
    check("Failed and blocked PAAR files remain unready", all(
        row.get("DW_READY_TO_LOAD_FL") is False for row in rows
        if str(row.get("DLVY_END_DATE"))[:10] >= "2026-03-11"))

    ready = api("/api/file-ingestion/PKG_PAAR_1/ready")
    counts = dict(collections.Counter(row.get("STATUS") for row in ready.get("rows", [])))
    check("Validation outcomes preserved", counts == {"UPDATED": 23, "STOPPED": 1, "BLOCKED": 150},
          statuses=counts)
    check("Accepted row metric preserved", ready.get("metrics", {}).get("rowsUpdated") == 29)
    stops = [row for row in ready.get("rows", []) if row.get("STATUS") == "STOPPED"]
    check("Actual first stop remains visible", len(stops) == 1
          and str(stops[0].get("DLVY_END_DATE"))[:10] == "2026-03-11"
          and "ROWCOUNT_MISMATCH" in str(stops[0].get("REASON")))
    history = api("/api/file-ingestion/PKG_PAAR_1/history")
    check("Validation history agrees with Snowflake archive",
          len(history.get("rows", [])) == EXPECTED_HISTORY_ROWS,
          history_rows=len(history.get("rows", [])), expected_history_rows=EXPECTED_HISTORY_ROWS)

    request("GET", "/api/file-ingestion/reload/spcs-smoke-missing-job", expected=404)
    invalid = [
        ("invalid subject", {"limFormat": "PAAR;invalid", "mode": "NORMAL"}),
        ("invalid mode", {"limFormat": "PAAR", "mode": "INVALID"}),
        ("missing date pair", {"limFormat": "PAAR", "mode": "NORMAL", "fromDate": "2026-03-01"}),
        ("reversed dates", {"limFormat": "PAAR", "mode": "NORMAL",
                            "fromDate": "2026-03-10", "toDate": "2026-03-01"}),
        ("reload without confirmation", {"limFormat": "PAAR", "mode": "FULL_RELOAD"}),
    ]
    for name, payload in invalid:
        response = request("POST", "/api/file-ingestion/reload", expected=400, json=payload)
        body = response.json()
        check("Rejected " + name, body.get("ok") is False and "job" not in body)


try:
    run()
except Exception as exc:
    # Exception bodies can include customer rows; report only the exception type.
    check("Smoke suite completed", False, error_type=type(exc).__name__)

summary = {"passed": sum(item["passed"] for item in results),
           "failed": sum(not item["passed"] for item in results), "checks": results}
print("SPCS_SMOKE_RESULT " + json.dumps(summary), flush=True)
sys.exit(1 if summary["failed"] else 0)
