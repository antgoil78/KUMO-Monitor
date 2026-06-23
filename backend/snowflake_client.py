from contextlib import contextmanager
from contextvars import ContextVar
import os
import re

import snowflake.connector
from snowflake.connector import DictCursor

import config

SPCS_TOKEN_FILE = "/snowflake/session/token"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_ingress_user_token = ContextVar("sf_ingress_user_token", default=None)


def set_ingress_user_token(token):
    """Store the Snowflake caller token for the current Flask request context."""
    token = str(token or "").strip() or None
    return _ingress_user_token.set(token)


def reset_ingress_user_token(token_handle):
    if token_handle is not None:
        _ingress_user_token.reset(token_handle)


def caller_token_present():
    return bool(_ingress_user_token.get())


def _read_spcs_token():
    try:
        with open(SPCS_TOKEN_FILE, "r", encoding="utf-8") as token_file:
            token = token_file.read().strip()
            return token or None
    except FileNotFoundError:
        return None


def _is_spcs_configured():
    return bool(
        os.getenv("SNOWFLAKE_HOST")
        and os.getenv("SNOWFLAKE_ACCOUNT")
        and _read_spcs_token()
    )


def _is_password_configured():
    return bool(config.SNOWFLAKE_ACCOUNT and config.SNOWFLAKE_USER and config.SNOWFLAKE_PASSWORD)


def connection_mode():
    if _is_spcs_configured():
        return "spcs-caller-oauth" if caller_token_present() else "spcs-service-oauth"
    if _is_password_configured():
        return "password"
    return "not-configured"


def is_configured():
    return connection_mode() != "not-configured"


def _quote_identifier_path(identifier):
    parts = [p.strip() for p in str(identifier or "").split(".") if p.strip()]
    if not parts:
        return None
    quoted = []
    for part in parts:
        if _SAFE_IDENTIFIER.match(part):
            quoted.append(part)
        else:
            quoted.append('"' + part.replace('"', '""') + '"')
    return ".".join(quoted)


def _connection_kwargs(include_context=True, include_warehouse=True):
    """Build connector kwargs.

    include_context=False is used for /api/session because CURRENT_USER/CURRENT_ROLE
    should not require database/schema/warehouse privileges. This prevents a missing
    warehouse/caller grant from hiding the actual signed-in user.
    """
    mode = connection_mode()
    if mode == "not-configured":
        raise RuntimeError(
            "Snowflake connection is not configured. In SPCS, Snowflake must provide "
            "SNOWFLAKE_HOST, SNOWFLAKE_ACCOUNT and /snowflake/session/token. "
            "Outside SPCS, set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and SNOWFLAKE_PASSWORD, "
            "or use KUMO_USE_MOCK=true."
        )

    if mode in ("spcs-service-oauth", "spcs-caller-oauth"):
        service_token = _read_spcs_token()
        ingress_user_token = _ingress_user_token.get()
        token = f"{service_token}.{ingress_user_token}" if ingress_user_token else service_token
        kwargs = {
            "host": os.getenv("SNOWFLAKE_HOST"),
            "account": os.getenv("SNOWFLAKE_ACCOUNT"),
            "token": token,
            "authenticator": "oauth",
        }
        if include_context:
            if config.SNOWFLAKE_DATABASE:
                kwargs["database"] = config.SNOWFLAKE_DATABASE
            if config.SNOWFLAKE_SCHEMA:
                kwargs["schema"] = config.SNOWFLAKE_SCHEMA
        # In caller-rights mode, let Snowflake activate the caller's default role.
        if mode == "spcs-service-oauth" and config.SNOWFLAKE_ROLE:
            kwargs["role"] = config.SNOWFLAKE_ROLE
    else:
        kwargs = {
            "account": config.SNOWFLAKE_ACCOUNT,
            "user": config.SNOWFLAKE_USER,
            "password": config.SNOWFLAKE_PASSWORD,
        }
        if include_context:
            if config.SNOWFLAKE_DATABASE:
                kwargs["database"] = config.SNOWFLAKE_DATABASE
            if config.SNOWFLAKE_SCHEMA:
                kwargs["schema"] = config.SNOWFLAKE_SCHEMA
        if config.SNOWFLAKE_ROLE:
            kwargs["role"] = config.SNOWFLAKE_ROLE

    if include_warehouse and config.SNOWFLAKE_WAREHOUSE:
        kwargs["warehouse"] = config.SNOWFLAKE_WAREHOUSE

    return kwargs


@contextmanager
def connection(use_warehouse=True, include_context=True):
    conn = snowflake.connector.connect(**_connection_kwargs(include_context=include_context, include_warehouse=use_warehouse))
    try:
        # Some connector/session combinations do not activate the warehouse even when
        # warehouse=... is passed. Force it for data queries only, not for /api/session.
        wh = _quote_identifier_path(config.SNOWFLAKE_WAREHOUSE) if use_warehouse else None
        if wh:
            cur = conn.cursor()
            try:
                cur.execute(f"USE WAREHOUSE {wh}")
            finally:
                cur.close()
        yield conn
    finally:
        conn.close()


def query(sql, params=None, use_warehouse=True, include_context=True):
    with connection(use_warehouse=use_warehouse, include_context=include_context) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute(sql, params or {})
            return cur.fetchall()
        finally:
            cur.close()


def execute(sql, params=None, use_warehouse=True, include_context=True):
    with connection(use_warehouse=use_warehouse, include_context=include_context) as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute(sql, params or {})
            try:
                return cur.fetchall()
            except Exception:
                return []
        finally:
            cur.close()


def query_one(sql, params=None, use_warehouse=True, include_context=True):
    rows = query(sql, params=params, use_warehouse=use_warehouse, include_context=include_context)
    return dict(rows[0]) if rows else {}


def ping():
    return query_one(
        """
        SELECT
          CURRENT_ACCOUNT() AS ACCOUNT_NAME,
          CURRENT_USER() AS USER_NAME,
          CURRENT_ROLE() AS ROLE_NAME,
          CURRENT_DATABASE() AS DATABASE_NAME,
          CURRENT_SCHEMA() AS SCHEMA_NAME,
          CURRENT_WAREHOUSE() AS WAREHOUSE_NAME
        """
    )


def _basic_session_context():
    # Do not select a warehouse here. The current-user query must work even if the
    # caller has no USAGE/CALLER USAGE on the configured warehouse yet.
    return query_one(
        """
        SELECT
          CURRENT_ACCOUNT() AS ACCOUNT_NAME,
          CURRENT_USER() AS USER_NAME,
          CURRENT_ROLE() AS ROLE_NAME,
          CURRENT_WAREHOUSE() AS WAREHOUSE_NAME,
          CURRENT_DATABASE() AS DATABASE_NAME,
          CURRENT_SCHEMA() AS SCHEMA_NAME
        """,
        use_warehouse=False,
        include_context=False,
    )


def _lookup_user_profile(user_name):
    """Best-effort profile lookup. Never make /api/session depend on this."""
    first_name = ""
    last_name = ""
    display_name = ""
    if not user_name:
        return first_name, last_name, display_name

    try:
        pattern = str(user_name).replace("'", "''")
        rows = query(f"SHOW USERS LIKE '{pattern}'", use_warehouse=False, include_context=False)
        if rows:
            row = {str(k).upper(): v for k, v in dict(rows[0]).items()}
            first_name = str(row.get("FIRST_NAME") or "").strip()
            last_name = str(row.get("LAST_NAME") or "").strip()
            display_name = str(row.get("DISPLAY_NAME") or "").strip()
    except Exception:
        # Many caller roles cannot SHOW USERS. That is fine; use CURRENT_USER instead.
        pass

    return first_name, last_name, display_name


def session_context():
    """Current browser user context for UI and audit logging.

    Uses a warehouse-free query so missing warehouse grants cannot mask the caller user.
    """
    ctx = _basic_session_context()
    user_name = str(ctx.get("USER_NAME") or ctx.get("user_name") or "").strip()
    role_name = str(ctx.get("ROLE_NAME") or ctx.get("role_name") or "").strip()
    first_name, last_name, snowflake_display_name = _lookup_user_profile(user_name)

    configured_name = os.getenv("KUMO_DISPLAY_NAME", "").strip()
    display_name = configured_name or snowflake_display_name
    if not display_name and (first_name or last_name):
        display_name = f"{first_name} {last_name}".strip()
    if not display_name:
        display_name = os.getenv("KUMO_USER_NAME", "").strip() or user_name or "KUMO user"

    # If no active warehouse is selected in the basic session, show the configured one.
    warehouse_name = str(ctx.get("WAREHOUSE_NAME") or "").strip() or config.SNOWFLAKE_WAREHOUSE or "Not selected"

    return {
        "displayName": display_name,
        "firstName": first_name,
        "lastName": last_name,
        "userName": user_name or "UNKNOWN",
        "roleName": role_name or "Unknown role",
        "warehouseName": warehouse_name,
        "mode": connection_mode(),
        "callerRightsActive": connection_mode() == "spcs-caller-oauth",
        "callerTokenPresent": caller_token_present(),
        "raw": ctx,
    }
