# KUMO Monitor

The maintained application is the current DEV/local Flask backend in `backend/`
and React app in `frontend/`. TST will use this same code with environment
configuration. Older application copies and completed legacy migration tools
are available in Git history; do not maintain parallel versions.

## Local configuration

Runtime secrets are loaded from `.env`. This file is ignored by Git and should
stay local to each developer machine.

Use `.env.example` as the non-secret template for required variables.

## DEV/TST configuration and migration

Set the full parameter table path when starting the service:

```dotenv
KUMO_ENVIRONMENT_TABLE=KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS
KUMO_ENV=DEV
```

`KUMO_ENVIRONMENT_TABLE` is required for a connected service. The table can live
in any accessible database/schema and can have a different name. The app reads
active, non-secret `STRING` parameters for instance `DEFAULT`, group
`ENVIRONMENT`, before loading its repositories and pages. Missing or invalid
required parameters stop startup rather than routing queries to a fallback.
Mock mode keeps local defaults without connecting.

| Object group | Location from parameters |
| --- | --- |
| Workflow procedures and tasks | `[DATABASE].[MONITOR_APP_SCHEMA]` |
| Monitor configuration, workflow/job catalog, audit, queues and history | `[DATABASE].[META_SCHEMA]` |
| LIM loader, raw file metadata and both readiness logs | `[DATABASE].[META_SCHEMA]` |
| Stage, file format, batch control, progress and statuses | `[DATABASE].[APP_SCHEMA]` |
| RAW subject data | `[DATABASE].[RAW_SCHEMA]` |
| dbt project object | `[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]` |
| dbt profile target | `[DBT_TARGET]` (case preserved) |

The same app image supports different database and schema names. `KUMO_ENV`
sets the DEV/TEST label and mock defaults; it does not override the parameter
store's database. `KUMO_DEPLOYMENT_ENV` can label future services UAT or PROD.
The top bar shows the backend's environment label, and `/api/health` reports
both the parameter-table address and resolved object context.

The app loads routing at startup. Restart it after changing routing parameters
or the bootstrap table path. Old `KUMO_DB`, schema and per-object overrides must
be removed or agree with the parameter store. Snowflake-provided service
`SNOWFLAKE_DATABASE`/`SNOWFLAKE_SCHEMA` do not control application routing.
Credentials, account, roles and warehouses remain deployment settings.

In SPCS, put `KUMO_ENVIRONMENT_TABLE` under `spec.containers[].env`. The offline
[`render_service.py`](snowflake/spcs/render_service.py) accepts
`--environment-table DATABASE.SCHEMA.TABLE`; it only prepares deployment SQL.
The Snowflake service object itself has its own concrete deployment address.
See the [SPCS deployment guide](snowflake/spcs/README.md).

The Snowflake engine reads the same parameter table through
`SP_GET_ENVIRONMENT()` for each invocation. Its bootstrap pointer and the master
stream condition are bound by
[`apply_environment_routing.py`](backend/migrations/apply_environment_routing.py).
Run that migration against the new parameter table when moving the engine or
its configuration store. Native DDL object names and task conditions must have
concrete addresses at deployment; table access and execution routing inside
the engine use the parameters. The migration preserves grants and the master's
original running/suspended state, and updates only changed DDL copies.

TST provisioning remains a separate step: provision its objects and parameter
rows, bind its engine to that table, and start the same app image with that
full table path. No workflow/job definition edits are needed.

Workflow SQL and dbt fields use `[VARIABLE]` tokens from **Settings → Environment**.
For example, `[DATABASE].[MONITOR_APP_SCHEMA]` locates workflow procedures;
`[DATABASE].[APP_SCHEMA]` locates data-platform metadata. Use `'[run_id]'` for a
SQL string containing the current run ID. The command preview resolves the
current editor/run-dialog values without running SQL or dbt. See the
[command template guide](snowflake/README.md#portable-workflow-commands).

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

Use **Settings → Engine** to inspect, resume or suspend the persistent
`TASK_WF_MASTER_DISPATCHER` in the configured engine namespace. Resuming allows
queued runs to dispatch, including existing requests. Suspending pauses new
dispatches while work already running can finish; individual workflow schedules
are managed separately. Both actions use the app's service context and are
audited against the browser user. The service role needs OWNERSHIP or OPERATE
on the task; Snowflake also checks the task owner's execution privileges when
resuming. Status refreshes every 15 seconds while the Engine tab is open.
Demo mode simulates the controls; an unconfigured Snowflake connection disables
them. App startup never resumes the master task automatically.

DEV definitions and workflow configuration have been copied: 27 engine
workflows, 29 catalog workflows and 44 jobs, plus their dependent configuration.
Workflow/job command fields use parameter templates; DEV parameter values resolve
to the `KUMO_DEV.DBT_APP.KUMO_DEV` project and `kumo_DEV` target. Copied schedules are disabled and the dispatcher is suspended.
Workflow execution validation remains; `META.WORKFLOW_DEP_RULE` is intentionally
not created yet. The DEV SPCS service has been deployed as
`KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE`, owned by `KUMO_ADMIN`, using image
`build-215-1-022ffedd2bf2`. TST provisioning remains a later step.
The shared image repository and compute pool have been created using
[create_shared_infrastructure.sql](snowflake/spcs/create_shared_infrastructure.sql).
The maintained DEV migrations and app definitions are documented in
[snowflake/README.md](snowflake/README.md).

CI publishes a separate image for every build and rerun, tagged
`build-<run-number>-<attempt>-<12-character-commit>`, for example
`build-123-1-a1b2c3d4e5f6`. It does not push a moving `latest` tag. The version
is also baked into the image for the sidebar and `/api/health`. The Actions
summary shows both the complete Docker image URI and the SPCS image path.

The default repository is
`<registry>/kumo_shared/monitor_app/monitor_repository/kumo-monitor`.
Optional GitHub repository variables `KUMO_IMAGE_REPOSITORY` (that full path
without a tag) and `SNOWFLAKE_REGISTRY` (registry hostname) can override it.
The older `KUMO_IMAGE_URI` variable remains a fallback: its repository is reused,
but any old tag is replaced with the generated version. The registry hostname
must match the repository path. These variables are not needed with the defaults.

Choose an exact published version in each service specification. DEV can run
a newer build while TST keeps an earlier build; promote the same image tag to
TST after verification. Publishing alone does not update any service. UAT/PROD
will use repositories and pools in their own accounts.

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
`KUMO_IMAGE_VERSION`. CI sets it to the generated build tag. Local Docker builds
default to `latest`; to build and run a specific local version:

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
3. Push changes for review and merge to `main`.
4. Let `.github/workflows/deploy-image.yml` build and publish the image on merge.
   Routine work does not manually dispatch the workflow or update SPCS directly.

## Snowflake app definitions

Current table, procedure and persistent task definitions are stored under
[`snowflake/`](snowflake/README.md), together with supporting views, functions,
streams and file formats, an object inventory, a restore order and a repeatable
export script. Generated
workflow scheduler tasks and temporary workflow run tasks are excluded.

DDL copies are maintained only for DEV's `MONITOR_APP` and `MONITOR_META`.
When we change an object's DDL, update that object's saved definition and any
applicable migration. Routine app work and configuration-row changes do not
require an export. Refresh both schemas only when explicitly requested; see
[`snowflake/README.md`](snowflake/README.md) and [`AGENTS.md`](AGENTS.md).

## Orchestration access in SPCS

The Workflow, Job and Flow pages read the selected environment's `MONITOR_META`
schema. Local runs use
the configured `SNOWFLAKE_ROLE`, while SPCS service connections use the role
that owns the service. The Orchestration endpoints use service credentials,
as the existing admin pages do, even when a browser caller token is present.
Users with access to the application can view and edit this shared catalog.
Caller credentials continue to identify users for sessions and request logging;
`CURRENT_USER()` in catalog table audit columns records the database service user.

When provisioning TST, grant its service owner access to that environment's
database, `MONITOR_APP` procedures/tasks, and `MONITOR_META` tables, including
the table reads/writes used by the catalog. Prepare grants for the selected
environment rather than replaying the retired legacy grant migration.

Deploy the updated backend image so these endpoints use the service role.
In the service specification, leave `SNOWFLAKE_ROLE` unset to use the service
owner by default, or set it to that owner role. The owner also needs `USAGE`
on the configured `SNOWFLAKE_WAREHOUSE`, as for the other admin pages.

After deployment, check `/api/orchestration/catalog` and
`/api/orchestration/flow` from the signed-in application. If an object cannot
be found, check the `orchestrationNamespace` reported by `/api/health` and
verify that `WORKFLOW`, `JOB`, `WORKFLOW_JOB` and `WORKFLOW_DEPENDENCY` exist
there and are accessible to the service owner.
