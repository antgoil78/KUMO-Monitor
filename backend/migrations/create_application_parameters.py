"""Create the application environment/system parameter store."""

import snowflake_client as sf


TABLE = "KUMO_ADMIN.WORKFLOW_MANAGER.APPLICATION_PARAMETERS"


def apply():
    with sf.connection(use_warehouse=True, include_context=True, force_service=True) as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"""
                CREATE HYBRID TABLE IF NOT EXISTS {TABLE} (
                  INSTANCE_NAME VARCHAR(100) NOT NULL DEFAULT 'DEFAULT',
                  PARAMETER_GROUP VARCHAR(30) NOT NULL,
                  PARAMETER_KEY VARCHAR(256) NOT NULL,
                  PARAMETER_VALUE VARCHAR,
                  VALUE_TYPE VARCHAR(30) NOT NULL DEFAULT 'STRING',
                  DESCRIPTION VARCHAR,
                  IS_SECRET BOOLEAN NOT NULL DEFAULT FALSE,
                  ACTIVE_FL BOOLEAN NOT NULL DEFAULT TRUE,
                  CREATED_DTTM TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
                  CREATED_BY VARCHAR NOT NULL DEFAULT CURRENT_USER(),
                  UPDATED_DTTM TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
                  UPDATED_BY VARCHAR NOT NULL DEFAULT CURRENT_USER(),
                  PRIMARY KEY (INSTANCE_NAME, PARAMETER_GROUP, PARAMETER_KEY)
                )
            """)
            return "ready"
        finally:
            cur.close()


if __name__ == "__main__":
    print(apply())
