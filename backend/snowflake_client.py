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
    """Store the caller token for the current Flask request context."""
    token = str(token or "").strip() or None
    return _ingress_user_token.set(token)


def reset_ingress_user_token(token_handle):
    if token_handle is not None:
        _ingress_user_token.reset(token_handle)


def caller_token_present():
    return bool(_ingress_user_token.get())


def _read_spcs_token():
    """Return the SPCS service OAuth token if this code is running inside Snowpark Container Services."""
    try:
        with open(SPCS_TOKEN_FILE, "r", encoding="utf-8") as token_file:
            token = token_file.read().strip()
            return token or None
    except FileNotFoundError:
        return None


def _is_spcs_configured():
    """Snowflake injects these values into SPCS containers."""
    return bool(
        os.getenv("SNOWFLAKE_HOST")
        and os.getenv("SNOWFLAKE_ACCOUNT")
        and _read_spcs_token()
    )


def _is_password_configured():
    """Fallback for local runs outside SPCS."""
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
    """Quote a Snowflake identifier or identifier path, e.g. DB.SCHEMA.WH."""
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


def _connection_kwargs():
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
            "database": config.SNOWFLAKE_DATABASE,
            "schema": config.SNOWFLAKE_SCHEMA,
        }
        # In caller-rights mode Snowflake activates the caller's default role.
        # Passing a role can make the session misleading or fail, so only pass role for service-user/password mode.
        if mode == "spcs-service-oauth" and config.SNOWFLAKE_ROLE:
            kwargs["role"] = config.SNOWFLAKE_ROLE
    else:
        kwargs = {
            "account": config.SNOWFLAKE_ACCOUNT,
            "user": config.SNOWFLAKE_USER,
            "password": config.SNOWFLAKE_PASSWORD,
            "database": config.SNOWFLAKE_DATABASE,
            "schema": config.SNOWFLAKE_SCHEMA,
        }
        if config.SNOWFLAKE_ROLE:
            kwargs["role"] = config.SNOWFLAKE_ROLE

    if config.SNOWFLAKE_WAREHOUSE:
        kwargs["warehouse"] = config.SNOWFLAKE_WAREHOUSE

    return kwargs


@contextmanager
def connection():
    conn = snowflake.connector.connect(**_connection_kwargs())
    try:
        wh = _quote_identifier_path(config.SNOWFLAKE_WAREHOUSE)
        if wh:
            cur = conn.cursor()
            try:
                cur.execute(f"USE WAREHOUSE {wh}")
            finally:
                cur.close()
        yield conn
    finally:
        conn.close()


def query(sql, params=None):
    with connection() as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute(sql, params or {})
            return cur.fetchall()
        finally:
            cur.close()


def execute(sql, params=None):
    with connection() as conn:
        cur = conn.cursor(DictCursor)
        try:
            cur.execute(sql, params or {})
            try:
                return cur.fetchall()
            except Exception:
                return []
        finally:
            cur.close()


def ping():
    rows = query(
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
    return dict(rows[0]) if rows else {}


def _lookup_user_profile(user_name):
    """Best-effort profile lookup. May fail if the active role cannot SHOW USERS."""
    first_name = ""
    last_name = ""
    display_name = ""
    if not user_name:
        return first_name, last_name, display_name

    try:
        pattern = user_name.replace("'", "''")
        rows = query(f"SHOW USERS LIKE '{pattern}'")
        if rows:
            row = {str(k).upper(): v for k, v in dict(rows[0]).items()}
            first_name = str(row.get("FIRST_NAME") or "").strip()
            last_name = str(row.get("LAST_NAME") or "").strip()
            display_name = str(row.get("DISPLAY_NAME") or "").strip()
    except Exception:
        pass

    return first_name, last_name, display_name


def session_context():
    """Best-effort current browser user context for the UI and audit logging.

    With caller's rights enabled, CURRENT_USER/CURRENT_ROLE reflect the signed-in Snowflake user.
    Without caller's rights, Snowflake returns the service user and service-owner role.
    """
    ctx = ping()
    user_name = str(ctx.get("USER_NAME") or ctx.get("user_name") or "")
    role_name = str(ctx.get("ROLE_NAME") or ctx.get("role_name") or "")
    first_name, last_name, snowflake_display_name = _lookup_user_profile(user_name)

    configured_name = os.getenv("KUMO_DISPLAY_NAME", "").strip()
    display_name = configured_name or snowflake_display_name
    if not display_name and (first_name or last_name):
        display_name = f"{first_name} {last_name}".strip()
    if not display_name:
        display_name = os.getenv("KUMO_USER_NAME", "").strip() or user_name or "KUMO user"

    return {
        "displayName": display_name,
        "firstName": first_name,
        "lastName": last_name,
        "userName": user_name,
        "roleName": role_name,
        "warehouseName": str(ctx.get("WAREHOUSE_NAME") or ""),
        "mode": connection_mode(),
        "callerRightsActive": connection_mode() == "spcs-caller-oauth",
        "callerTokenPresent": caller_token_present(),
        "raw": ctx,
    }
