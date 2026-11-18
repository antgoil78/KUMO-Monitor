"""Notification administration API for KUMO Monitor.

This module is intentionally implemented as a Flask Blueprint so the feature can
be added to the existing application with only two integration lines in app.py.
It manages email distribution groups and exposes the workflow notification
configuration overview used by the React Notifications page.
"""

from flask import Blueprint, current_app, jsonify, request
from snowflake.connector import DictCursor
import re
import uuid

import config
import snowflake_client as sf
from utils import normalize_rows


notification_admin_bp = Blueprint("notification_admin", __name__)

EMAIL_GROUPS_TABLE = config.T_EMAIL_GROUPS

# Useful for local UI development when KUMO_USE_MOCK=true.
_MOCK_GROUPS = []
_MOCK_WORKFLOWS = []


def _clean(value):
    return str(value or "").strip()


def _json_error(error, status=500):
    message = str(error or "Unexpected error")
    return jsonify({"ok": False, "error": message}), status


def _load_notification_admin():
    """Load groups and workflow notification configuration in one connection."""
    with sf.connection(
        use_warehouse=True,
        include_context=True,
        force_service=True,
    ) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute(
                f"""
                SELECT
                    GROUP_NAME,
                    RECIPIENTS,
                    DESCRIPTION
                FROM {EMAIL_GROUPS_TABLE}
                ORDER BY GROUP_NAME
                """
            )
            groups = normalize_rows(cur.fetchall())

            cur.execute(
                f"""
                SELECT
                    w.WORKFLOW_ID,
                    w.WORKFLOW_NAME,
                    w.WORKFLOW_GROUP,
                    n.ON_SUCCESS_EMAIL,
                    n.ON_FAIL_EMAIL,
                    n.SUCCESS_GROUP,
                    n.FAIL_GROUP,
                    n.EMAIL_INTEGRATION,
                    n.ENVIRONMENT
                FROM {config.T_ORCHESTRATION_WORKFLOW if config.LEGACY_ENGINE_RETIRED else config.T_WORKFLOWS} w
                LEFT JOIN {config.T_NOTIFICATIONS} n
                    ON n.WORKFLOW_ID = w.WORKFLOW_ID
                ORDER BY
                    w.WORKFLOW_GROUP,
                    w.WORKFLOW_NAME
                """
            )
            workflows = normalize_rows(cur.fetchall())
        finally:
            cur.close()

    return {
        "groups": groups,
        "workflows": workflows,
    }


def _notification_values(payload):
    if not isinstance(payload, dict):
        raise ValueError('A JSON object is required')
    for field in ('onFailEmail', 'onSuccessEmail'):
        if type(payload.get(field)) is not bool:
            raise ValueError(f'{field} must be true or false')
    values = dict(on_fail=payload['onFailEmail'], on_success=payload['onSuccessEmail'],
                  fail_group=_clean(payload.get('failGroup')), success_group=_clean(payload.get('successGroup')),
                  integration=_clean(payload.get('emailIntegration')), environment=config.ENVIRONMENT)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', values['integration']):
        raise ValueError('Choose a valid Snowflake email integration name')
    for channel in ('fail', 'success'):
        if values['on_' + channel] and not values[channel + '_group']:
            raise ValueError(f'Choose a recipient group for {channel} emails')
    return values


@notification_admin_bp.patch('/api/notification-workflows/<workflow_id>')
def update_workflow_notifications(workflow_id):
    try:
        workflow_id = str(uuid.UUID(workflow_id))
        values = _notification_values(request.get_json(silent=True))
        if config.USE_MOCK or not sf.is_configured():
            return _json_error('Connect to Snowflake to edit workflow notifications.', 503)
        catalog = config.T_ORCHESTRATION_WORKFLOW if config.LEGACY_ENGINE_RETIRED else config.T_WORKFLOWS
        with sf.connection_scope(force_service=True):
            if not sf.query_one_service(f'SELECT WORKFLOW_ID FROM {catalog} WHERE WORKFLOW_ID = %(workflow_id)s', {'workflow_id': workflow_id}):
                return _json_error('Workflow not found', 404)
            for channel in ('fail', 'success'):
                if values['on_' + channel]:
                    group = sf.query_one_service(f'SELECT RECIPIENTS FROM {config.T_EMAIL_GROUPS} WHERE GROUP_NAME = %(group)s', {'group': values[channel + '_group']})
                    if not group or not _clean(group.get('RECIPIENTS')):
                        raise ValueError('Choose an email group with recipients')
            sf.execute_service(f'''
                MERGE INTO {config.T_NOTIFICATIONS} t
                USING (SELECT %(workflow_id)s AS WORKFLOW_ID) s ON t.WORKFLOW_ID = s.WORKFLOW_ID
                WHEN MATCHED THEN UPDATE SET ON_FAIL_EMAIL=%(on_fail)s, ON_SUCCESS_EMAIL=%(on_success)s,
                    FAIL_GROUP=%(fail_group)s, SUCCESS_GROUP=%(success_group)s,
                    EMAIL_INTEGRATION=%(integration)s, ENVIRONMENT=%(environment)s, UPDATED_AT=SYSDATE()
                WHEN NOT MATCHED THEN INSERT (WORKFLOW_ID,ON_FAIL_EMAIL,ON_SUCCESS_EMAIL,FAIL_GROUP,SUCCESS_GROUP,EMAIL_INTEGRATION,ENVIRONMENT)
                    VALUES (s.WORKFLOW_ID,%(on_fail)s,%(on_success)s,%(fail_group)s,%(success_group)s,%(integration)s,%(environment)s)
            ''', {**values, 'workflow_id': workflow_id})
        return jsonify(ok=True, workflowId=workflow_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return _json_error(exc, 400)
    except Exception as exc:
        current_app.logger.exception('Failed to save workflow notifications %s', workflow_id)
        return _json_error(exc, 500)


def _create_email_group(group_name, recipients="", description=""):
    group_name = _clean(group_name)
    if not group_name:
        raise ValueError("Group name is required")

    sf.execute_service(
        f"""
        INSERT INTO {EMAIL_GROUPS_TABLE}
            (GROUP_NAME, RECIPIENTS, DESCRIPTION)
        SELECT
            %(group_name)s,
            %(recipients)s,
            %(description)s
        """,
        params={
            "group_name": group_name,
            "recipients": str(recipients or ""),
            "description": str(description or ""),
        },
        use_warehouse=True,
        include_context=True,
    )

    return {
        "GROUP_NAME": group_name,
        "RECIPIENTS": str(recipients or ""),
        "DESCRIPTION": str(description or ""),
    }


def _update_email_group(group_name, recipients="", description=""):
    group_name = _clean(group_name)
    if not group_name:
        raise ValueError("Group name is required")

    sf.execute_service(
        f"""
        UPDATE {EMAIL_GROUPS_TABLE}
        SET
            RECIPIENTS = %(recipients)s,
            DESCRIPTION = %(description)s,
            UPDATED_AT = SYSDATE()
        WHERE GROUP_NAME = %(group_name)s
        """,
        params={
            "group_name": group_name,
            "recipients": str(recipients or ""),
            "description": str(description or ""),
        },
        use_warehouse=True,
        include_context=True,
    )

    return {
        "GROUP_NAME": group_name,
        "RECIPIENTS": str(recipients or ""),
        "DESCRIPTION": str(description or ""),
    }


def _delete_email_group(group_name):
    group_name = _clean(group_name)
    if not group_name:
        raise ValueError("Group name is required")

    sf.execute_service(
        f"""
        DELETE FROM {EMAIL_GROUPS_TABLE}
        WHERE GROUP_NAME = %(group_name)s
        """,
        params={"group_name": group_name},
        use_warehouse=True,
        include_context=True,
    )

    return group_name


@notification_admin_bp.get("/api/notification-admin")
def notification_admin():
    if config.USE_MOCK or not sf.is_configured():
        return jsonify(
            {
                "ok": True,
                "source": "mock",
                "groups": list(_MOCK_GROUPS),
                "workflows": list(_MOCK_WORKFLOWS),
            }
        )

    try:
        data = _load_notification_admin()
        return jsonify({"ok": True, "source": "snowflake", **data})
    except Exception as exc:
        current_app.logger.exception("Failed to load notification administration")
        return _json_error(exc, 500)


@notification_admin_bp.post("/api/notification-groups")
def create_notification_group():
    payload = request.get_json(silent=True) or {}
    group_name = _clean(payload.get("groupName"))

    if not group_name:
        return _json_error("Group name is required", 400)

    recipients = str(payload.get("recipients") or "")
    description = str(payload.get("description") or "")

    if config.USE_MOCK or not sf.is_configured():
        if any(str(row.get("GROUP_NAME")) == group_name for row in _MOCK_GROUPS):
            return _json_error(f'Email group "{group_name}" already exists', 409)
        row = {
            "GROUP_NAME": group_name,
            "RECIPIENTS": recipients,
            "DESCRIPTION": description,
        }
        _MOCK_GROUPS.append(row)
        _MOCK_GROUPS.sort(key=lambda item: str(item.get("GROUP_NAME") or "").lower())
        return jsonify({"ok": True, "source": "mock", "group": row}), 201

    try:
        row = _create_email_group(group_name, recipients, description)
        return jsonify({"ok": True, "source": "snowflake", "group": row}), 201
    except Exception as exc:
        current_app.logger.exception("Failed to create email group %s", group_name)
        return _json_error(exc, 500)


@notification_admin_bp.patch("/api/notification-groups/<path:group_name>")
def update_notification_group(group_name):
    group_name = _clean(group_name)
    payload = request.get_json(silent=True) or {}
    recipients = str(payload.get("recipients") or "")
    description = str(payload.get("description") or "")

    if not group_name:
        return _json_error("Group name is required", 400)

    if config.USE_MOCK or not sf.is_configured():
        for row in _MOCK_GROUPS:
            if str(row.get("GROUP_NAME")) == group_name:
                row["RECIPIENTS"] = recipients
                row["DESCRIPTION"] = description
                return jsonify({"ok": True, "source": "mock", "group": row})
        return _json_error("Email group not found", 404)

    try:
        row = _update_email_group(group_name, recipients, description)
        return jsonify({"ok": True, "source": "snowflake", "group": row})
    except Exception as exc:
        current_app.logger.exception("Failed to update email group %s", group_name)
        return _json_error(exc, 500)


@notification_admin_bp.delete("/api/notification-groups/<path:group_name>")
def delete_notification_group(group_name):
    group_name = _clean(group_name)

    if not group_name:
        return _json_error("Group name is required", 400)

    if config.USE_MOCK or not sf.is_configured():
        before = len(_MOCK_GROUPS)
        _MOCK_GROUPS[:] = [
            row for row in _MOCK_GROUPS
            if str(row.get("GROUP_NAME")) != group_name
        ]
        if len(_MOCK_GROUPS) == before:
            return _json_error("Email group not found", 404)
        return jsonify(
            {
                "ok": True,
                "source": "mock",
                "groupName": group_name,
                "deleted": True,
            }
        )

    try:
        deleted_name = _delete_email_group(group_name)
        return jsonify(
            {
                "ok": True,
                "source": "snowflake",
                "groupName": deleted_name,
                "deleted": True,
            }
        )
    except Exception as exc:
        current_app.logger.exception("Failed to delete email group %s", group_name)
        return _json_error(exc, 500)
