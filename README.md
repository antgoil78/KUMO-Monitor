# KUMO Monitor

## Local configuration

Runtime secrets are loaded from `.env`. This file is ignored by Git and should
stay local to each developer machine.

Use `.env.example` as the non-secret template for required variables.

## Local Docker run

After Docker is installed:

```bash
docker compose up --build
```

Open:

```text
http://localhost:5000
```

`docker-compose.yml` automatically reads `.env` and passes the Snowflake
settings into the container at runtime. Do not pass Snowflake credentials as
Docker build args or commit them to Git.

### Image version

The image tag and the version displayed in the sidebar use
`KUMO_IMAGE_VERSION`. It defaults to `latest`, so the existing workflow keeps
working. To build and run a versioned image:

```bash
KUMO_IMAGE_VERSION=1.2.0 docker compose up --build -d
```

This creates `kumo-monitor:1.2.0`, exposes `imageVersion` from `/api/health`,
and displays `1.2.0` in the application sidebar. You can optionally provide
`GIT_SHA` alongside it to expose the source revision in the version tooltip.

### Snowflake account identifier

If Snowflake returns an error like:

```text
404 Not Found: post <account>.snowflakecomputing.com:443/session/v1/login-request
```

then `SNOWFLAKE_ACCOUNT` is probably not the full Snowflake account identifier
for the account's region/cloud.

Use the value shown in Snowsight under account details for connectors/drivers.
For the Python connector, use the account identifier without `https://` and
without `.snowflakecomputing.com`.

Examples:

```bash
SNOWFLAKE_ACCOUNT=myorg-myaccount
SNOWFLAKE_ACCOUNT=abc12345.west-europe.azure
SNOWFLAKE_ACCOUNT=abc12345.eu-central-1.aws
```

### MFA for local password auth

If Snowflake requires MFA for your user, set a current TOTP code before starting
the container:

```bash
SNOWFLAKE_PASSCODE=123456 docker compose up --build
```

The passcode is short-lived. Do not commit it, and do not rely on this pattern
for unattended deployments. For automated builds/deployments, prefer a
deployment-specific auth method such as Snowflake-managed service auth,
key-pair auth, OAuth, or a programmatic access token according to your Snowflake
security policy.

## Local development run

Backend:

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements.txt
python -m flask --app app run --host 0.0.0.0 --port 5000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open:

```text
http://localhost:3000
```

The Vite dev server proxies `/api` to the Flask backend on port `5000`.

## Development workflow

1. Code and test locally using Docker or the split backend/frontend dev servers.
2. Keep `.env` local only; commit `.env.example`, code, and docs.
3. Push the repo to Git.
4. Build and deploy the Snowflake container from the Git source.

## Snowflake definitions and account migration

Current table, procedure and persistent task definitions are stored under
[`snowflake/`](snowflake/README.md), together with supporting views, functions,
streams and file formats, an object inventory, a restore order and a repeatable
export script. Generated
workflow scheduler tasks and temporary workflow run tasks are excluded.

**Whenever a Snowflake object is updated, always update its SQL definition in
Git as part of the same change.** This also applies to edits made directly in
Snowsight. Refresh the definitions with `python snowflake/export_ddl.py`, review
the Git diff, and commit the changed SQL and any applicable migration script.
Repository agents follow the same rule in [`AGENTS.md`](AGENTS.md).

## Orchestration access in SPCS

The Workflow, Job and Flow pages read `KUMO_TST.MONITOR_APP`. Local runs use
the configured `SNOWFLAKE_ROLE`, while SPCS service connections use the role
that owns the service. The Orchestration endpoints use service credentials,
as the existing admin pages do, even when a browser caller token is present.
Users with access to the application can view and edit this shared catalog.
Caller credentials continue to identify users for sessions and request logging;
`CURRENT_USER()` in catalog table audit columns records the database service user.

After creating the new schemas and tables, run
[`backend/migrations/grant_monitor_orchestration_access.sql`](backend/migrations/grant_monitor_orchestration_access.sql)
in a Snowflake worksheet as `SECURITYADMIN`, or another role authorized to
manage existing and future grants in both schemas. Run its statements in order
in the same session. It discovers the owner of
`KUMO_ADMIN.KUMO_MONITOR.KUMO_MONITOR_SERVICE`, then grants database/schema
`USAGE` and table `SELECT`, `INSERT`, `UPDATE` and `DELETE` in `MONITOR_APP` and
`MONITOR_META`, including future tables. The included schema migration creates
no tables in `MONITOR_META`; it is not queried by the Orchestration pages.

Deploy the updated backend image so these endpoints use the service role.
In the service specification, leave `SNOWFLAKE_ROLE` unset to use the service
owner by default, or set it to that owner role. The owner also needs `USAGE`
on the configured `SNOWFLAKE_WAREHOUSE`, as for the other admin pages.

After deployment, check `/api/orchestration/catalog` and
`/api/orchestration/flow` from the signed-in application. If an object still
cannot be found, check that the service is in the account containing
`KUMO_TST.MONITOR_APP` and that the schema migration created `WORKFLOW`, `JOB`,
`WORKFLOW_JOB` and `WORKFLOW_DEPENDENCY` there.
