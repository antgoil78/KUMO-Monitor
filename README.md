# KUMO Monitor

## Local configuration

Runtime secrets are loaded from `.env`. This file is ignored by Git and should
stay local to each developer machine.

Use `.env.example` as the non-secret template for required variables.

## DEV/TST configuration and migration

Set one application environment selector at runtime:

```dotenv
KUMO_ENV=DEV
```

The top bar shows a persistent environment badge: yellow **DEV**, green
**TEST**, blue **UAT**, or red **PROD**. `TST` displays as `TEST`. The label
comes from the backend's startup configuration and remains visible when the
sidebar is collapsed or hidden on mobile. An unavailable label shows `UNKNOWN`.

`KUMO_DEPLOYMENT_ENV` optionally sets the service label independently of the
DEV/TST database selector. It defaults to `KUMO_ENV`, so DEV and TEST need no
additional setting. Future UAT and PROD deployments on their separate Snowflake
accounts can set `KUMO_DEPLOYMENT_ENV=UAT` or `PROD`; this does not change the
database selector or choose an account. Their account/database setup remains a
separate deployment step. The SPCS renderer accepts `--deployment-env` for this.

Use `TST` later, after its new layout has been provisioned. Both environments
use the same image and schema layout:

| Object group | Location derived from `KUMO_ENV` |
| --- | --- |
| Workflow procedures and tasks | `KUMO_<env>.MONITOR_APP` |
| Monitor tables, workflow/job catalog, configuration, audit, notifications and queue stream | `KUMO_<env>.MONITOR_META` |
| LIM loader procedure and `RAW_LOAD_RESULTS` | `KUMO_<env>.MONITOR_META` |
| `RAW_LIM_META`, `_SET_READY_LOG`, package/source mapping | `KUMO_<env>.MONITOR_META` |
| `_TMP_SET_READY_LOG`, stage, file format, batch control, dbt progress and statuses | `KUMO_<env>.META` |
| RAW subject data | `KUMO_<env>.RAW_LIM` |
| SPCS service | `KUMO_<env>.MONITOR_APP.KUMO_MONITOR_SERVICE` |
| Container image | `KUMO_SHARED.MONITOR_APP.MONITOR_REPOSITORY` within the deployment account |
| Compute pool | `KUMO_SHARED_MONITOR_POOL` (account-level, shared by new DEV/TST services) |

`backend/config.py` resolves these locations once at startup. Backend queries
use the resolved names, and `/api/health` supplies the same context to the
frontend for labels and generated worksheet SQL. The settings page cannot
switch the application's environment. Restart/redeploy after changing it.

In SPCS, place `KUMO_ENV` under `spec.containers[].env`. A service is itself a
Snowflake object with a database and schema; changing the container variable
does not relocate it. The offline
[`render_service.py`](snowflake/spcs/render_service.py) derives both the service
name and its container environment from the same selector. See the
[SPCS deployment guide](snowflake/spcs/README.md) for a complete example.
Snowflake supplies `SNOWFLAKE_DATABASE` and `SNOWFLAKE_SCHEMA` from the service
location. The backend checks that the connection database agrees with
`KUMO_ENV`; use the new environment's service rather than changing the old
service to another environment.

Clear old database/schema overrides in the local `.env`, Docker configuration
and SPCS specification when using the new defaults. Blank settings use the
selected layout. `KUMO_SCHEMA` overrides the procedure/task schema,
`KUMO_METADATA_SCHEMA` overrides the monitor table schema, and
`KUMO_ORCHESTRATION_SCHEMA` can override the workflow/job catalog schema.
Database-qualified overrides must belong to the selected environment;
conflicting settings stop startup with the setting name.

With `KUMO_ENV` unset, the current split deployment remains supported during
migration: engine/application tables in `KUMO_ADMIN.WORKFLOW_MANAGER`, catalog
in `KUMO_TST.MONITOR_APP`, and data/progress in `KUMO_TST`. This temporary path
can be removed after both replacement environments have been validated.

Account, warehouses, roles and image location are deployment resources.
SPCS uses Snowflake's service OAuth token; the service owner needs access to
the selected environment, image repository, compute pool and warehouses.
The old account role named `KUMO_ADMIN` is independent of the database with
that name. The SPCS renderer sets `KUMO_LIM_ROLE` to the selected service owner
for File Ingestion, without embedding user credentials.

Application-created scheduler tasks stay suspended in DEV/TST unless
`KUMO_AUTO_RESUME_TASKS=true` is explicitly set. This does not resume the
persistent master dispatcher. Generated execution tasks inside procedures
retain their own lifecycle.

DEV definitions and workflow configuration have been copied: 27 engine
workflows, 29 catalog workflows and 44 jobs, plus their dependent configuration.
Executable references use DEV, including `KUMO_DEV.DBT_APP.KUMO_DEV` and its
`kumo_DEV` target. Copied schedules are disabled and the dispatcher is suspended.
Grants and full execution validation remain; `META.WORKFLOW_DEP_RULE` is
intentionally not created yet. No DEV SPCS service has been deployed. TST
provisioning and shared infrastructure creation are later steps; their setup SQL is prepared in
[create_shared_infrastructure.sql](snowflake/spcs/create_shared_infrastructure.sql).
The reviewed migrations and
saved definitions are documented in [snowflake/README.md](snowflake/README.md).

CI accepts GitHub repository variables `KUMO_IMAGE_URI` (complete tagged image
URI) and `SNOWFLAKE_REGISTRY` (registry hostname). The default image destination
is now `<registry>/kumo_shared/monitor_app/monitor_repository/kumo-monitor:latest`.
The shared repository must be provisioned before publishing. Update an existing
`KUMO_IMAGE_URI` repository variable if it still points at the old repository.
Use a separate branch image tag for DEV validation before changing the running
service's image. UAT/PROD will use repositories and pools in their own accounts.

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

The Workflow, Job and Flow pages read the selected environment's `MONITOR_META`
schema (`KUMO_TST.MONITOR_APP` in the current split deployment). Local runs use
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
`MONITOR_META`, including future tables. This grant script targets the existing
deployment. Prepare grants for the new
service owner and the DEV layout separately; the new catalog tables are in
`KUMO_DEV.MONITOR_META`.

Deploy the updated backend image so these endpoints use the service role.
In the service specification, leave `SNOWFLAKE_ROLE` unset to use the service
owner by default, or set it to that owner role. The owner also needs `USAGE`
on the configured `SNOWFLAKE_WAREHOUSE`, as for the other admin pages.

After deployment, check `/api/orchestration/catalog` and
`/api/orchestration/flow` from the signed-in application. If an object cannot
be found, check the `orchestrationNamespace` reported by `/api/health` and
verify that `WORKFLOW`, `JOB`, `WORKFLOW_JOB` and `WORKFLOW_DEPENDENCY` exist
there and are accessible to the service owner.
