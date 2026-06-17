from contextlib import contextmanager
import snowflake.connector
from snowflake.connector import DictCursor

import config


def is_configured():
    return bool(config.SNOWFLAKE_ACCOUNT and config.SNOWFLAKE_USER and config.SNOWFLAKE_PASSWORD)


@contextmanager
def connection():
    if not is_configured():
        raise RuntimeError("Snowflake connection is not configured. Set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and SNOWFLAKE_PASSWORD or use KUMO_USE_MOCK=true.")

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
