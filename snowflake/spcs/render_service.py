"""Render a new environment's SPCS CREATE SERVICE SQL; never connect or deploy."""

import argparse
import os
from pathlib import Path
import re
from string import Template


TEMPLATE = Path(__file__).with_name("service.yaml.template")
SHARED_COMPUTE_POOL = "KUMO_SHARED_MONITOR_POOL"


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Expected an unquoted Snowflake resource identifier")
    return value.upper()


def render(environment, image, compute_pool, query_warehouse, owner_role, task_warehouse=None, deployment_env=None, environment_table=None):
    environment = (environment or "").strip().upper()
    if environment not in ("DEV", "TST"):
        raise ValueError("KUMO_ENV must be DEV or TST")
    deployment_env = (deployment_env or "").strip().upper() or environment
    deployment_env = {"TST": "TEST", "PRD": "PROD"}.get(deployment_env, deployment_env)
    if deployment_env not in ("DEV", "TEST", "UAT", "PROD"):
        raise ValueError("Deployment environment must be DEV, TEST, UAT or PROD")
    # Accept repository paths with a tag or digest, without SQL/YAML metacharacters.
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+|@sha256:[a-fA-F0-9]{64})", image):
        raise ValueError("Image must be /database/schema/repository/image:tag or use a sha256 digest")
    compute_pool = identifier(compute_pool)
    query_warehouse = identifier(query_warehouse)
    owner_role = identifier(owner_role)
    task_warehouse = identifier(task_warehouse or query_warehouse)
    environment_table = environment_table or f"KUMO_{environment}.MONITOR_META.APPLICATION_PARAMETERS"
    environment_table = '.'.join(identifier(part) for part in environment_table.split('.'))
    if len(environment_table.split('.')) != 3:
        raise ValueError("Environment table must be database.schema.table")
    spec = Template(TEMPLATE.read_text()).substitute(
        ENVIRONMENT=environment, DEPLOYMENT_ENVIRONMENT=deployment_env, ENVIRONMENT_TABLE=environment_table,
        IMAGE=image, QUERY_WAREHOUSE=query_warehouse,
        TASK_WAREHOUSE=task_warehouse, OWNER_ROLE=owner_role,
    )
    service = f"KUMO_{environment}.MONITOR_APP.KUMO_MONITOR_SERVICE"
    return (
        "-- Prepared SQL only; the renderer does not connect to Snowflake.\n"
        "-- Use an image built from this branch. Review grants and resources before deployment.\n"
        "-- CREATE fails if this service already exists; it cannot replace another service.\n"
        f"USE ROLE {owner_role};\n"
        f"CREATE SERVICE {service}\n"
        f"  IN COMPUTE POOL {compute_pool}\n"
        "  FROM SPECIFICATION $$\n" + spec + "$$\n"
        "  MIN_INSTANCES = 1\n"
        "  MAX_INSTANCES = 1\n"
        f"  QUERY_WAREHOUSE = {query_warehouse};\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.getenv("KUMO_ENV"), help="DEV or TST; defaults to KUMO_ENV")
    parser.add_argument("--deployment-env", default=os.getenv("KUMO_DEPLOYMENT_ENV"), help="Optional DEV/TEST/UAT/PROD service label; defaults to --env")
    parser.add_argument("--environment-table", default=os.getenv("KUMO_ENVIRONMENT_TABLE"), help="Full environment parameter table path")
    parser.add_argument("--image", required=True, help="Snowflake repository image path with tag or digest")
    parser.add_argument("--compute-pool", default=SHARED_COMPUTE_POOL, help="Account-level pool; defaults to KUMO_SHARED_MONITOR_POOL")
    parser.add_argument("--query-warehouse", required=True)
    parser.add_argument("--owner-role", required=True)
    parser.add_argument("--task-warehouse")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        sql = render(args.env, args.image, args.compute_pool, args.query_warehouse,
                     args.owner_role, args.task_warehouse, args.deployment_env, args.environment_table)
    except ValueError as error:
        parser.error(str(error))
    if args.output:
        args.output.write_text(sql)
    else:
        print(sql, end="")


if __name__ == "__main__":
    main()
