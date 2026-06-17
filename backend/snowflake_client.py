from contextlib import contextmanager
import os

import snowflake.connector
from snowflake.connector import DictCursor

import config

SPCS_TOKEN_FILE = "/snowflake/session/token"


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
        return "spcs-oauth"
    if _is_password_configured():
        return "password"
    return "not-configured"


def is_configured():
    return connection_mode() != "not-configured"


@contextmanager
def connection():
    mode = connection_mode()

    if mode == "not-configured":
        raise RuntimeError(
            "Snowflake connection is not configured. In SPCS, Snowflake must provide "
            "SNOWFLAKE_HOST, SNOWFLAKE_ACCOUNT and /snowflake/session/token. "
            "Outside SPCS, set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and SNOWFLAKE_PASSWORD, "
            "or use KUMO_USE_MOCK=true."
        )

    if mode == "spcs-oauth":
        kwargs = {
            "host": os.getenv("SNOWFLAKE_HOST"),
            "account": os.getenv("SNOWFLAKE_ACCOUNT"),
            "token": _read_spcs_token(),
            "authenticator": "oauth",
            "database": config.SNOWFLAKE_DATABASE,
            "schema": config.SNOWFLAKE_SCHEMA,
        }
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

    conn = snowflake.connector.connect(**kwargs)

    try:
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
