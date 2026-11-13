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

Saving or enabling a workflow schedule resumes its Snowflake scheduler when
the workflow is enabled. Disabling the schedule removes its scheduler. The
former `KUMO_AUTO_RESUME_TASKS` setting is ignored, including in existing service
specifications. Starting the app does not activate copied schedules or resume
the persistent master dispatcher. Generated execution tasks inside procedures
retain their own lifecycle.

In environments retaining the old engine (currently TST), use **Settings → Engine** to inspect, resume or suspend the persistent
`TASK_WF_MASTER_DISPATCHER` in the configured engine namespace. Resuming allows
queued runs to dispatch, including existing requests. Suspending pauses new
dispatches while work already running can finish; individual workflow schedules
are managed separately. Both actions use the app's service context and are
audited against the browser user. The service role needs OWNERSHIP or OPERATE
on the task; Snowflake also checks the task owner's execution privileges when
resuming. Status refreshes every 15 seconds while the Engine tab is open.
Demo mode simulates the controls; an unconfigured Snowflake connection disables
them. App startup never resumes the master task automatically.

DEV has 27 existing execution workflows. The new catalog was rebuilt from
26 of them, excluding `RUN DEPENDENCIES`: 14 workflows, 38 jobs and 52 dependency
rows. Each subject's readiness, SDL and EDV steps form one `… LOAD` workflow;
`LOAD RAW DAILY` and `MAINTENANCE - CLEANUP` each contain one job.
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

### Checks before opening a PR

Run these checks from the repository root after installing the backend and
frontend dependencies. Use Node 22, matching the image builder and PR CI:

```bash
KUMO_USE_MOCK=true python -m unittest discover -s backend/tests -p 'test_*.py'
npm --prefix frontend test
npm --prefix frontend run build
docker compose --env-file .env.example config --quiet
cd frontend
npx playwright install chromium
npm run test:e2e
```

The browser suite starts its own Vite server on port 3100 and intercepts API
requests with fixtures. It needs no backend, Snowflake credentials, or running
workloads. It checks timeline scrolling, relationship lines, selected-run
actions, history retention across refresh/navigation, and dependency draft
protection. On machines with a system Chrome installation, you can use
`PLAYWRIGHT_CHANNEL=chrome npm run test:e2e` instead of installing Chromium.
Pull requests to `main` run these checks in `validate-pr.yml`.
That workflow does not publish images or change a service; the existing image
workflow continues to publish on merges to `main`.

The new monitor and engine still have three known legacy parity gaps:
automatic downstream workflow dispatch, completion notifications, and dbt
warning/artifact classification. This change does not implement those engine
features. Before promoting the new engine for workloads relying on them,
complete their integration and DEV validation. Fixture browser tests also do
not replace live DEV checks for accepted run IDs, task permissions, workload
execution, and schedule controls. See the
[engine validation instructions](snowflake/README.md).

### Start the application

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
verify that `WORKFLOW`, `JOB` and `DEPENDENCIES` exist
there and are accessible to the service owner.

**Workflow → Workflow** administers these three tables using the same
expandable workflow/job layout as the Monitor reference. Create or edit
workflow and job definitions, assign existing jobs, and drag jobs or use the
arrow buttons to change their order. Save changes to persist assignments and
parent links together. The first job's `PARENT_ID` is its workflow's ID; each
following job points to the preceding job. Existing status, SQL and ALLOW rules
are preserved when reordering. A job can be reused across workflows.

The page reports unmatched dependency references without deleting them or
importing assignments from `WORKFLOW_DEPENDENCY`. It does not change the
existing execution engine, schedules or tasks.

The parallel execution engine uses `WORKFLOW_EXECUTION_LOG` for both pending
requests and workflow/job results. Each manual run creates one temporary
workflow task bound to that execution and removes it afterward. Idle unscheduled
workflows have no task; scheduled workflows keep a persistent scheduler. Jobs
never own tasks. Workflow and Monitor pages both include Run controls.
Job Run offers **Run only this job** and **Run from this job**. Definitions,
conditions and commands are snapshotted when the request is accepted.

**Workflow → Monitor** reads the real `WORKFLOW`, `JOB`, `DEPENDENCIES`,
and `WORKFLOW_EXECUTION_LOG` tables through a read-only endpoint. It refreshes
automatically and keeps one execution's workflow/job statuses together: active
first, otherwise the oldest queued request, otherwise the latest result.
Each configured job receives a status. Disabled and out-of-scope jobs are
`SKIPPED`; later selected jobs are `PENDING` while the first goes through
`INITIATING`, `QUEUED` and `RUNNING`. Progress includes run-scoped dbt model/test
counts. Older partial executions can still show `NOT SELECTED`. Workflows without execution records show
`NOT RUN`. Click a workflow name for its execution history. Workflow Run executes
all enabled jobs in order; a job Run offers only that job or from that job onward.
Disabled jobs are omitted from the execution preview and recorded as skipped.
Right-aligned green play buttons and three-dot actions are in the Workflow / Job
column. Live activity indicators identify the workflow and current job.

Manual API execution requires `KUMO_ORCHESTRATION_RUNS_ENABLED=true` in the
running backend; the code default is false. Initial deployment prepares workflow
schedulers suspended; enable individual schedules from the Monitor. A static housekeeping task advances accepted
requests, recovers interrupted runs, and removes leftover temporary tasks.
TST retains its existing engine until the new engine has been tested and accepted. See the [parallel engine guide](snowflake/README.md#parallel-orchestration-engine).

The Dashboard uses the same shared workflow/job snapshot as Workflow Monitor,
including active submissions, job progress and enabled schedules. Last Run uses
Stockholm calendar days: blue **Today**, yellow **Yesterday**, otherwise a date.

Manual dbt job runs offer **Full refresh** for `build`, `run` and `seed`. It adds
`--full-refresh` to the selected job's execution only; **Run from this job** keeps
later jobs' saved commands. Job definitions and scheduled runs are unchanged.
Before releasing this UI, run the targeted procedure migration in each intended
environment (it inspects only unless `--apply` is supplied):

```bash
python backend/migrations/apply_manual_dbt_full_refresh.py --environment DEV
python backend/migrations/apply_manual_dbt_full_refresh.py --environment DEV --apply
```

Use `--environment TST` for TST. Finish pending workflow runs first and use the
procedure owner role. The migration updates only admission and command resolution,
preserves usage grants and retains existing five-argument calls via an optional
boolean. The signature change requires replacing the old overload, following
[Snowflake's optional argument rules](https://docs.snowflake.com/en/sql-reference/sql/create-procedure).

DEV now uses the new Workflow Monitor for manual workflow/job runs. The old
execution tasks, procedures, queue and stream are suspended/archived with `X_`
names; old history remains available. DEV's environment-specific retirement
parameter blocks the old controls and recovery worker. TST is unchanged.
See [DEV retirement](snowflake/README.md#dev-retirement-of-the-old-execution-engine)
for the targeted migration and remaining dispatch/notification integration.

The new Monitor includes Log shortcuts on workflow/job rows and Latest DAG Run
for DBT jobs, quick enable/disable controls, and a schedule editor. Latest DAG Run
uses the job's latest started execution, even when a newer workflow run skipped
that job. DAG Preview belongs to DBT jobs under Workflow configuration and uses
the saved selection with `dbt list` and `WRITEBACK = FALSE` without running models.
Last Run/Duration show the latest
started execution for each row. Schedule hides cron text when disabled; Next Run
comes from the active native workflow scheduler. Workflow disabling pauses its
schedule; enabling it resumes the configured schedule. Accepted executions keep
their saved selection. Job enable/disable applies globally wherever that job is
assigned. DAGs and logs read only the selected new workflow/job execution IDs.

The orchestration Monitor reads one shared Python snapshot. Python refreshes the
Snowflake execution/progress data once per backend process and pushes snapshots
through the existing `/api/events` SSE stream (`orchestration_update`). Initial
load, reconnect, and a disconnected-stream fallback read the shared cache; they
do not query Snowflake per browser. Manual refresh wakes the shared reader.

Manual Run returns HTTP 202 with a native Snowflake query receipt, request ID,
and `INITIATING` status. Snapshot preparation and temporary workflow task startup
continue in the background; SSE reports validation errors and execution states.
The idempotent request ID prevents duplicate executions on retry. Reconciliation
is handled by the static maintenance task, outside the submission path. Run
controls for the workflow and its jobs are disabled while submitting, queued,
initiating, or running and become available again after completion. No database
DDL changes are required for this transport/lifecycle change.

Background monitor/dashboard refreshes follow the instance's live SSE client
count: the first connection starts the shared readers and the last disconnect
stops them. Health checks, activity requests and cached snapshot reads do not
start polling. A refresh already in progress can finish; no further refresh
batches begin while the client count is zero. Reconnecting resumes fresh data.
Submission-confirmation workers also wait for a connected client before their
next Snowflake call. Accepted workflows and native scheduled tasks continue
independently in Snowflake, including recovery by the static maintenance task.

Workflow / History reads `WORKFLOW_EXECUTION_LOG`, with workflow/job,
status, period and text filters and cursor pagination. Workflow and job Actions
open the corresponding history scope. Each run expands into its saved job
order; Log and DAG Run open that exact execution and return to the same filters
and expanded rows. DBT jobs link to their model DAG; workflow rows link to the
executed job chain. History loads on demand, coalesces identical requests in a
bounded 15-second cache, and uses the Monitor's existing SSE snapshots for live
statuses. Historical Log/DAG views refresh on demand rather than polling.
