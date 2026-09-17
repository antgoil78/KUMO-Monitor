"""Add the optional workflow result payload to workflow history."""

import snowflake_client as sf


TABLE = "KUMO_ADMIN.WORKFLOW_MANAGER.WORKFLOW_HISTORY"


def main():
    with sf.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS PAYLOAD_MESSAGE VARCHAR")
        finally:
            cur.close()


if __name__ == "__main__":
    main()
