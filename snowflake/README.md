# Snowflake app definitions

The maintained DDL copy contains the current DEV application schemas:

| Schema | Contents |
| --- | --- |
| `KUMO_DEV.MONITOR_APP` | Procedures and static engine tasks |
| `KUMO_DEV.MONITOR_META` | Monitor configuration, workflow/job catalog, queues, history and app metadata |

These definitions and the current local application are the starting point
for TST. There is one maintained app, configured per environment. Legacy app
copies, completed copy/upgrade migrations, and wider account snapshots have
been removed from the working tree; their history remains in Git. `review/`
contains historical investigation and deployment evidence, not active setup
instructions.

## Save only the DDL we change

Do not refresh Snowflake DDL during ordinary application work, investigations,
tests, configuration-row changes, or releases. When we change an object in
one of the two app schemas, update just its saved full definition in
`ddl/<database>/<schema>/` and keep an incremental migration when needed.
Editing the saved SQL directly is sufficient when it represents our DDL change.

To read back individual changed objects from Snowflake:

```bash
python snowflake/export_ddl.py --object PROCEDURE 'KUMO_DEV.MONITOR_APP.SP_RUN_WORKFLOW(VARCHAR,VARCHAR)'
python snowflake/export_ddl.py --object TABLE KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS
```

Repeat `--object TYPE NAME` for multiple changed objects. Include the argument
signature for functions and procedures, including `()` for no arguments. The
exporter reads only the selected object types/schemas and fetches DDL only for
the selected objects; untouched saved definitions are preserved.

For an object deletion, remove its saved SQL and its inventory/restore-order
entry as part of that same change. Do not run a full export to discover an
object we already know we deleted.

## Refresh only when requested

A refresh of both maintained app schemas requires an explicit user request:

```bash
python snowflake/export_ddl.py --refresh
```

Running the exporter without an action prints help and does not connect to
Snowflake. It loads connection settings from the ignored local `.env`; an
optional `--role` selects the reading role. It never executes exported DDL or
changes live objects. Review the resulting Git diff.

`export_scope.json` limits the maintained schemas. `manifest.json` records the
saved objects and their restore order. `00_databases_schemas.sql` contains only
the app namespaces. Generated schedulers and `TMP_WF_*` execution tasks are
excluded; the static master dispatcher is retained. Credentials and table rows
are never included.

Data-platform schemas such as `META`, `RAW_LIM`, SDL, EDV and dbt project files
are external dependencies, outside this DDL copy. Provision their required
objects, permissions, stages, integrations and compute separately before using
the app in another environment. The copy is not an account-wide backup.

An offline bundle of the saved app definitions can be built without querying
Snowflake:

```bash
python snowflake/export_ddl.py --build-restore /tmp/kumo-monitor-app.sql
```

This bundle contains `CREATE OR REPLACE` definitions, no table data or grants.
Use it only for deliberate provisioning; review environment references first.
Tasks are created suspended. Recreate generated schedules through the app when
the destination is configured and ready.

## Current DEV migrations

The current orchestration administration uses `WORKFLOW`, `JOB` and
`DEPENDENCIES`. `DEPENDENCIES` stores `WORKFLOW_ID`, nullable `JOB_ID`,
`PARENT_ID`, `TRIGGER_TYPE`, `SUCCESS`, `WARNING`, `ERROR`, `SQL`, `ALLOW`,
and `PARENT_MUST_BE_NEWER` (Boolean, default FALSE).
Workflow rows have no job ID. Assignment rows identify a job within a workflow;
their parent IDs form the job order, starting with the containing workflow.
The Workflow page saves order changes in one transaction and rejects stale
revisions. Reordering preserves existing rules. Unmatched sample references
are reported and preserved. The page itself does not import older configuration.
The old `WORKFLOWS` / `WORKFLOW_TASKS` definitions remain available for historical reads. DEV retirement is described below; TST retains its existing engine.

## Workflow dependency dispatch after execution

[`SP_ORCH_EVALUATE_DEPENDENCIES.sql`](ddl/KUMO_DEV/MONITOR_APP/procedures/SP_ORCH_EVALUATE_DEPENDENCIES.sql)
defines `MONITOR_APP.SP_ORCH_EVALUATE_DEPENDENCIES(P_EXECUTION_ID VARCHAR)`.
The executor calls `SP_ORCH_DISPATCH_DEPENDENCIES` after saving each completed
job, then again after saving the final workflow status. The dispatcher uses the
read-only evaluator, admits eligible children, and wakes their native tasks. Native task failure/cancellation recovery evaluates
the recovered terminal records too. The report is saved under
`WORKFLOW_EXECUTION_LOG.RESULT.dependencyEvaluation`, preserving command results,
workflow counters, and execution status. Evaluator failures are recorded as
`ok: false` reports and warnings; they do not turn successful workloads into
failures. Skipped/out-of-scope jobs that never enter the executor do not need
separate checks; the workflow receives its final check.

Eligible children now start automatically. The dispatcher admits all ready
siblings before waking any of their tasks, so a fast sibling cannot release a
join using another sibling's old successful run. Admission rechecks eligibility
under the existing configuration/workflow locks and derives a `DEP:` request ID
from the sorted complete parent workflow/execution set. Repeated completion
checks reuse that request, including after it finishes. Accepted work is logged
as `TRIGGER_SOURCE='DEPENDENCY'`, with every parent execution frozen in
`CONFIG_SNAPSHOT.dependency.parents`. Failed task wakeups leave durable requests
for native reconciliation.

Install the four changed DEV procedures with the targeted migration (read-only
by default):

```bash
python backend/migrations/apply_dependency_dispatch.py
python backend/migrations/apply_dependency_dispatch.py --apply
```

The migration checks DEV routing, idle executions, procedure ownership and
source drift, and preserves existing execution/recovery grants. It changes only
`SP_ORCH_REQUEST_RUN`, `SP_ORCH_DISPATCH_DEPENDENCIES`,
`SP_ORCH_EXECUTE_WORKFLOW`, and `SP_ORCH_RECONCILE`;
no tasks, schedules, execution records, services, or TST objects are changed by
deployment. The standard engine installer includes both the evaluator and
dispatcher. The admission upgrade preserves five/six-argument manual/scheduled
calls; the optional seventh argument is the source execution for internal
`DEPENDENCY` requests. Native workload execution remains in the child's own
task, outside the admission transaction.

The input identifies a completed job or workflow execution in
`WORKFLOW_EXECUTION_LOG`. Its workflow identifies candidate children through
`DEPENDENCIES` rows with `JOB_ID IS NULL`, that workflow as `PARENT_ID`, and
`TRIGGER_TYPE = 'DEP'`. For each candidate, **every workflow dependency row must
pass**. Several distinct parent workflows are supported. Job assignment rows
and other children's external/runner triggers are excluded.

Rules use the latest requested run of each parent, with execution ID breaking
timestamp ties. Ranking includes unfinished and failed runs; an older success
cannot substitute for the latest run. Missing or unfinished parents wait.
`SUCCESS`, `WARNING`, and `FAILED`/`ERROR` select the corresponding status flag:
TRUE accepts, FALSE rejects, NULL imposes no restriction. Skipped/cancelled
parents cannot release a child. Partial manual executions (`JOB_ONLY` and
`FROM_JOB`) retain their existing behavior of not releasing downstream workflows.

Each workflow parent rule can require a fresh completion with
`PARENT_MUST_BE_NEWER=TRUE`. The latest parent must have `ENDED_AT` strictly
after the child's most recent `STARTED_AT` across workflow execution records.
Equality or an older completion reports `STALE_PARENT` and prevents dispatch.
A child that has never started has no freshness cutoff. Queued/cancelled
requests without a start timestamp do not erase the previous cutoff. Each
parent rule is checked independently before SQL conditions, including when
admission rechecks eligibility under its locks.

The dependency editor displays one row per workflow parent rule, preserving
multiple parents and allowing independent freshness/status/SQL settings.
Install the column and updated evaluator in DEV (read-only by default):

```bash
python backend/migrations/apply_dependency_freshness.py
python backend/migrations/apply_dependency_freshness.py --apply
```

On first installation this enables freshness for existing workflow DEP rules.
It leaves job assignments unchanged. Reapplying preserves explicit settings.
The migration checks DEV routing, idle runs, ownership and evaluator drift;
it replaces only the evaluator with COPY GRANTS and adds one column. Saved
DDL includes this column for new environments.

Workflow dependency admission also waits for active **parents and grandparents**.
This check is always applied and stops at those two levels. It checks all
workflow-root executions in `PENDING`, `INITIATING`, `QUEUED`, `CLAIMED` or
`RUNNING`, including an older active run hidden by a newer completion. Job
execution rows do not participate. Status/freshness/SQL conditions continue to
apply to the latest direct parent run; grandparent status is checked only for
activity. Decisions report `PARENT_BUSY` or `GRANDPARENT_BUSY`, with `parentBusy`
and `busyGrandparentCount` on each rule. SQL conditions wait for these checks.

Completion events re-evaluate direct children and grandchildren. The evaluator
orders affected parents before their children so the existing enqueue-before-
wakeup dispatcher and locked admission recheck see newly queued parent runs.
Partial source runs retain their existing no-downstream-release behavior.
Install only the updated DEV evaluator (read-only by default):

```bash
python backend/migrations/apply_dependency_activity.py
python backend/migrations/apply_dependency_activity.py --apply
```

The migration checks DEV routing, idle runs, evaluator ownership and source
drift, and replaces only that procedure with COPY GRANTS. No configuration
rows, execution records, tasks or services are changed by deployment.

The executor is now the single owner of the start decision for **all triggers
and run modes**, including manual, scheduled, dependency, partial and recovery
runs. `SP_ORCH_START_RUN` only prepares/wakes the task and never reserves or
claims an execution. The normal path is `QUEUED -> RUNNING -> terminal`;
`CLAIMED` is accepted only for compatibility with older records.

The executor opens a short transaction, acquires the same `DEPENDENCIES`
UPDATE lock as admission/configuration writers, then the workflow lock. It
checks FIFO/same-workflow activity, checks related runs, writes RUNNING and
commits. Workload SQL/dbt executes after the commit. Retried task invocations
must pass the same gate and cannot execute the same run twice. Eligibility and
the start gate share `MONITOR_APP.V_ORCH_WORKFLOW_RELATIONS`, which contains only
workflow parents/grandparents and excludes job sequencing.

A child waits for any active parent/grandparent, including queued upstream
work. In the reverse direction, a parent/grandparent waits for RUNNING
protected descendants; queued descendants yield priority to upstream work.
This avoids a queue deadlock when both sides are waiting to start. Checks stop
at two levels in either direction; siblings and unrelated workflows can run
in parallel. The lock is held only through the RUNNING transition, not for the
workload duration. A blocked root stays QUEUED with its reason and blocking
execution IDs under `RESULT.startGate`. Existing maintenance retries queued
work; admission still handles latest-parent status/freshness/SQL rules.

Install the shared relation view and three updated DEV procedures (read-only
by default):

```bash
python backend/migrations/apply_atomic_workflow_start.py
python backend/migrations/apply_atomic_workflow_start.py --apply
```

The migration checks DEV routing, idle runs, ownership and known source hashes.
It grants view SELECT to existing procedure usage roles and preserves procedure
grants with COPY GRANTS. It changes only the shared view, evaluator, launcher
and executor. Standard environment installers use the same view/procedures.
No schema-wide DDL export, app image publication or service update is needed.

Offline gate tests execute the saved SQL/view and procedure bodies:
`python3 -m unittest discover -s backend/tests -p 'test_atomic_workflow_start.py'`.
The opt-in live check is `python backend/tests/live_atomic_workflow_start.py --run`.
It uses isolated temporary fixture definitions and real native tasks to verify
both grandparent/child directions, queued priority, waiting diagnostics and
completed-run replay. Successful test fixtures/logs/tasks are removed; active
failed-test runs are retained for diagnosis. WF1-WF10 are not modified.

The DEV test graph can be extended to ten workflows with observable delays:

```bash
python backend/migrations/seed_dependency_test_workflows.py
python backend/migrations/seed_dependency_test_workflows.py --apply
```

The graph is `WF1 -> (WF2, WF3) -> WF4 -> (WF5, WF6) -> WF7 -> (WF8, WF9) -> WF10`.
Each join requires both parents. WF1/WF4/WF7/WF10 wait 20 seconds; WF2/WF5/WF8
wait 30 seconds; WF3/WF6/WF9 wait 60 seconds, using `SYSTEM$WAIT` in harmless SQL
jobs with 180-second statement timeouts. Native task and hook overhead adds to
the total elapsed time. Setup preserves existing rule settings, rejects name,
identity and graph conflicts, and changes only fixture configuration while
those workflows are idle. It starts no executions or schedules. Fixture
definitions share the `DEPENDENCY TEST` group; start WF1 manually to observe
the full chain.

Disabled children, ALLOW=false, active child requests, unknown references,
self-dependencies, duplicate parent rules, and mixed trigger configurations
prevent eligibility. After every parent/status rule passes, each configured
SQL condition is resolved through the environment command resolver and executed
as a SELECT subquery. It must return exactly one non-NULL Boolean cell; FALSE
skips and errors prevent eligibility. In these conditions, `[run_id]` identifies
that rule's parent workflow execution; no child execution exists yet.

The returned variant contains `evaluated`, `children[].eligible`, and a decision
per rule with its selected parent execution ID. It is an observation, not a
reservation: direct evaluator calls may report the same child READY. The
dispatcher returns `dispatches` and per-child errors alongside that report.
Workflow dependencies wait for workflow completion: job reports can say WAITING
while the final workflow report says READY. Admission supports multiple distinct
parent rows and rejects duplicate parents or ancestor cycles. The editor retains
WF4's two rules and saves changes to each parent independently.

Offline checks execute the actual relational SQL against fixtures and the
procedure body through the Snowflake JavaScript API test harness:

```bash
python3 -m unittest discover -s backend/tests -p 'test_workflow_dependency_eligibility.py'
python3 -m unittest discover -s backend/tests -p 'test_post_execution_dependencies.py'
python3 -m unittest discover -s backend/tests -p 'test_dependency_dispatch.py'
python3 -m unittest discover -s backend/tests -p 'test_dependency_evaluation_migration.py'
```

The evaluator was checked in Snowflake with read-only CTE fixtures and against
WF1's real completed workflow/job executions. Integration checks cover normal
completion, skipped/failed jobs, evaluator failures, and native task recovery.

## Parallel orchestration engine

`backend/migrations/apply_parallel_engine.py` deploys the parallel engine alongside
the existing engine. Default is a read-only preflight; `--apply` updates only
this engine's objects, removes its old suspended persistent manual tasks, and
prepares configured workflow schedulers suspended:

```bash
python backend/migrations/apply_parallel_engine.py
python backend/migrations/apply_parallel_engine.py --apply
```

Use `--environment-table DATABASE.SCHEMA.TABLE` for another explicitly selected
environment. Routing comes from its environment parameters. Workflow warehouse
selection uses `WORKFLOW.WAREHOUSE_NAME`, initialized from the deployment setting
or `--warehouse`. New workflows created in the app receive that deployment default.
Credentials and table contents are not saved with the definitions.

| Object | Responsibility |
| --- | --- |
| `MONITOR_META.WORKFLOW_EXECUTION_LOG` | Durable requests and workflow/job results |
| `MONITOR_APP.SP_ORCH_EVALUATE_DEPENDENCIES` | Report child workflow eligibility after terminal execution updates |
| `MONITOR_APP.SP_ORCH_DISPATCH_DEPENDENCIES` | Admit and start eligible children once per parent execution set |
| `MONITOR_APP.SP_ORCH_REQUEST_RUN` | Validate scope and atomically snapshot enabled jobs and resolved commands |
| `MONITOR_APP.SP_ORCH_START_RUN` | Serialize workflow requests and create/start a task for one recorded run |
| `MONITOR_APP.SP_ORCH_EXECUTE_WORKFLOW` | Execute only that run's selected jobs in order |
| `MONITOR_APP.SP_ORCH_RUN_SCHEDULED` | Snapshot and execute a full workflow inside its persistent scheduler |
| `MONITOR_APP.SP_ORCH_RECONCILE` | Recover interrupted runs, clean temporary tasks, and advance queues |
| `MONITOR_APP.TASK_ORCH_MAINTENANCE` | Run recovery/cleanup every minute, independently of browsers |

An idle unscheduled workflow has no native task. Every manual request, including
manual runs of a scheduled workflow, receives one
`MONITOR_APP.TMP_ORCH_WF_<workflow UUID>_<run UUID>` task. Its fixed body calls the
runner with that execution ID, so a one-job request cannot drain another request
or execute unrelated jobs. Jobs never own tasks. The task is created on demand
with automatic retries disabled and dropped after the workflow result is saved.
[Snowflake permits a running task to finish when it is dropped](https://docs.snowflake.com/en/sql-reference/sql/drop-task).

Scheduled workflows are the exception: their persistent
`TASK_ORCH_WF_<workflow UUID>_SCHEDULER` calls the scheduled wrapper. Each occurrence
snapshots all currently enabled jobs. A manual partial run creates a separate
temporary task and does not alter the scheduled task. The deployment prepares
configured schedulers suspended. The Monitor and Workflow editors now synchronize
schedule changes directly with their native workflow scheduler.
Activate or pause individual schedules explicitly through the new Monitor.
The initial deployment still prepares schedulers suspended. The static housekeeping task is resumed
by deployment and handles only requests already accepted into the new log.
Generated workflow run/scheduler tasks are excluded from saved DDL; the static
housekeeping definition is retained.

`WORKFLOW.TASK_NAME` stores only the optional persistent scheduler address.
Temporary task addresses belong to their execution rows. The caller-rights
runner validates the native task context and uses the existing command resolver
unchanged. The scheduled wrapper also uses caller's rights so both paths can
execute dbt without entering an owner-rights workload call chain.

The log has one workflow row (`JOB_ID IS NULL`) per request and one row per
configured job, including explicitly skipped jobs, linked by `WORKFLOW_RUN_ID` and `PARENT_EXECUTION_ID`.
`QUEUED` is durable waiting work, `CLAIMED` reserves a workflow while its task
is provisioned, and `RUNNING` begins when the native task claims the execution.
Requests for the same workflow are serialized. A completed run launches the
oldest waiting request; housekeeping resumes interrupted provisioning and queue
advancement. Repeated request IDs return the original run. Task creation is
idempotent and checks the existing task body before issuing `EXECUTE TASK`.

Supported manual scopes are `WORKFLOW`, `JOB_ONLY`, and `FROM_JOB`. Partial runs
bypass earlier jobs without inventing successful records for them. Disabled and out-of-scope jobs
are recorded as `SKIPPED` before command resolution and do not block later enabled jobs.
The first runnable job starts `INITIATING`, becomes `QUEUED` during task provisioning,
and then `RUNNING`. Later selected jobs remain `PENDING` until their turn.
ALLOW=false and false Boolean SQL conditions produce skipped results. Subsequent
selected jobs follow snapshotted parent-result rules. Each job has its own
execution ID, query context, and resolved dbt `runId`. A workflow fails if any
selected job fails. dbt warning/artifact classification remains a later integration.

Recovery uses matching native task history, never heartbeat age, to close failed
or cancelled executions. Unstarted jobs become skipped. It does not replay a
running/finished workload. Terminal temporary tasks are removed, including tasks
left behind by timeouts; persistent workflow schedulers are retained.

```text
GET       /api/orchestration/engine
GET       /api/orchestration/monitor
GET, POST /api/orchestration/workflows/<workflow_id>/runs
POST      /api/orchestration/workflows/<workflow_id>/reconcile
```

POST run requests specify `mode`, optional `jobId`, and a stable `requestId`.
Actor identity comes from the backend session. Both Workflow and Monitor pages
preview and run the selected scope and display execution history. Manual API
requests require `KUMO_ORCHESTRATION_RUNS_ENABLED=true` in the running backend;
the code default remains false for environments not yet enabled.

The Monitor GET reads the three catalog tables and execution log on one service
connection. It selects an active run, otherwise the oldest queued request,
otherwise the latest result. Job statuses/progress belong to that same run;
removed jobs remain visible using their snapshot names. Model and test counts are read from the existing data-platform progress tables,
keyed by the job execution ID passed to dbt as `runId`. Missing telemetry is shown
as waiting for progress. Monitoring GET does not
start tasks or reconcile state; explicit Run and Refresh status actions use POST.

The new engine automatically dispatches eligible downstream workflows through
its own admission and task lifecycle. It does not write existing workflow
history/queues or reuse the old dispatcher,
completion procedures, or locks. Known legacy engine/cleanup references in selected
commands are rejected. The retired cleanup call was removed from the migrated maintenance job. Business
workload tables, loaders, and dbt targets remain shared resources.

The explicit validation command creates disposable SELECT/dbt-parse jobs and
checks full/only/from temporary tasks, idempotency, queue advancement, enabled-job
order, the scheduled exception, cleanup, and timeout recovery:

```bash
python backend/tests/live_parallel_engine.py --run
```

It removes its own fixture rows/tasks and checks unchanged existing-engine DDL
and row fingerprints. It is not a routine startup or deployment step.

## Catalog migration history

On 10 November 2026, `backend/migrations/migrate_workflow_catalog.py --apply`
rebuilt DEV's three new catalog tables from `WORKFLOWS` and `WORKFLOW_TASKS`:
14 workflows, 38 jobs and 52 dependency rows. Twelve subject workflows contain
readiness → SDL → EDV jobs; `LOAD RAW DAILY` and `MAINTENANCE - CLEANUP` each
contain one job. `RUN DEPENDENCIES` is excluded. SQL text and dbt deployment
parameters are preserved; each original SDL/EDV dbt command is split into its
two path selectors. Existing success callbacks become parent links. The entry
step's schedule is retained, with all DEV schedules still disabled.

```bash
python backend/migrations/migrate_workflow_catalog.py
python backend/migrations/migrate_workflow_catalog.py --apply
```

The default previews the mapping. `--apply` deliberately clears and repopulates
only `WORKFLOW`, `JOB` and `DEPENDENCIES` in one transaction, replacing any later
catalog edits if rerun. It verifies all inserted values and unchanged source
definitions before committing. It changes no DDL, native tasks or execution
history and executes no workflow commands. No DDL export is needed.

The earlier orchestration dependency editor used `WORKFLOW` and `JOB` as definitions
and `WORKFLOW_DEPENDENCY` for both workflow rows and job assignments. It did
not read or write `WORKFLOW_JOB`. Both earlier dependency tables are retained;
the current application reads and writes `DEPENDENCIES`. Run preview remains
simulated. The migration below documents that earlier model and is not required
to use the current Workflow page.

In the unified store, `CHILD_WORKFLOW_ID` identifies the containing workflow.
`JOB_ID IS NULL` identifies its workflow configuration row; otherwise the row
assigns that job. `PARENT_WORKFLOW_ID` or `PARENT_JOB_ID` identifies the parent,
with job parents scoped to the same containing workflow. `TRIGGER_TYPE`, the
nullable `SUCCESS`/`WARNING`/`ERROR` flags, `SQL_CONDITION`, and `ALLOW` hold the
editable rules. The API validates membership uniqueness, parent types, cycles,
and the single entry job before saving all edits in one transaction. A revision
token rejects stale edits. SQL conditions are stored as text and are not
executed by configuration saves.

```bash
python backend/migrations/unify_orchestration_dependencies.py
python backend/migrations/unify_orchestration_dependencies.py --apply
```

This targeted migration was applied to DEV on 27 October 2026. It preserves
multiple workflow parents and entry jobs for explicit resolution in the editor,
rather than choosing new relationships. It does not execute SQL conditions,
change schedules, or start tasks. Repeating it after migration is a no-op.
For TST, use `--namespace KUMO_TST.MONITOR_META` when that promotion is requested.
Only `WORKFLOW_DEPENDENCY` changes its definition; refresh only that object's
saved DDL after applying the migration.

`backend/migrations/` keeps the current DEV readiness, parameter and command
template migrations, with `migration_support.py` providing shared helpers.
The readiness migration verifies the three procedures it changes and reads
back only those changed definitions. It never performs a full DDL export.
The optional live readiness checks in `backend/tests/live_dev_lim_readiness*.py`
use disposable synthetic fixtures; they are not routine checks or backups.

The DEV readiness implementation registers nonempty payload files in
`META.LIM_BATCH_CTRL` and records validated empty deliveries in the current and
historical readiness logs, both in `[DATABASE].[META_SCHEMA]`
(`MONITOR_META` in DEV/TST). It does not require RAW readiness flag columns.
The loader and readiness procedures resolve their database and schemas from the
shared environment parameters. An explicit database argument must match the
configured engine environment; omitted arguments use that environment.

## Resetting a LIM sequence baseline

`backend/migrations/apply_lim_sequence_reset.py --environment-table
KUMO_TST.MONITOR_META.APPLICATION_PARAMETERS` checks only the two readiness
procedures. Add `--apply` to deploy the saved corrections while preserving
grants. It refuses unreviewed procedure drift or active/queued workflows; it
does not change configuration rows, clear logs, or run workflows.

With a NULL `DLVY_PKG_YEAR_SEQ_NO` in `RAW_LIM_PKG_GROUP_SOURCE`, sequence
validation starts at the first unhandled delivery for that source/subject/package.
Deliveries whose payloads are all already accepted remain visible but do not
participate in sequence ordering. Partial deliveries still participate, and
subsequent deliveries retain normal gap checks. Successful readiness saves the
new year and sequence, including when only the sequence field was NULL.

The explicit regression check
`python3 backend/tests/live_lim_sequence_reset.py --database KUMO_TST` uses
disposable synthetic fixtures and removes its schema afterward. It verifies
baseline resets, preservation of processed registrations, later gaps, missing
payloads, accepted empty deliveries, and row-count validation.

## Workflow environment parameters

`backend/migrations/apply_workflow_environment_parameters.py --apply` renames
DEV's old `APP_SCHEMA=MONITOR_APP` to `MONITOR_APP_SCHEMA` and adds:

| Parameter | DEV value |
| --- | --- |
| `DATABASE` | `KUMO_DEV` |
| `MONITOR_APP_SCHEMA` | `MONITOR_APP` |
| `META_SCHEMA` | `MONITOR_META` |
| `APP_SCHEMA` | `META` |
| `RAW_SCHEMA` | `RAW_LIM` |
| `DBT_SCHEMA` | `DBT_APP` |
| `DBT_PROJECT` | `KUMO_DEV` |
| `DBT_TARGET` | `kumo_DEV` |

All eight use instance `DEFAULT`, group `ENVIRONMENT`, type `STRING`, and are
active and non-secret. `DATABASE` and `META_SCHEMA` retain their earlier meanings.
Without `--apply`, the migration performs a read-only preflight. It checks the
live dbt project/default target, commits changes in one transaction, refuses
conflicting values, and preserves matching rows on repeat runs.

`DBT_SCHEMA` locates the project object; model output schemas remain in the dbt
profile. Parameter row changes never require a DDL refresh.

## App and engine routing

Start the app with `KUMO_ENVIRONMENT_TABLE=database.schema.table`. This full
bootstrap address is independent of the monitored database/schema names.
The app reads the eight required parameters at startup before importing its
repositories. Restart the app after changing routing parameters. Missing,
inactive, secret, duplicate or unsafe required values fail explicitly.

Deploy the engine's environment routing with:

```bash
python backend/migrations/apply_environment_routing.py \
  --environment-table KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS
# Review the preflight, then apply the same command with --apply.
```

This migration adds `RAW_SCHEMA` when absent (default `RAW_LIM`, override with
`--raw-schema`), and uses the table's database/schema parameters to deploy the
current engine implementation. It binds `SP_GET_ENVIRONMENT()` to that exact
parameter-table path. Consumers read this helper at invocation time and use
validated identifiers for tables, callbacks, task placement, RAW context, LIM
stage/file format, dependency rules and dbt command resolution. Optional legacy
database arguments default to the configured database and cannot select a
different environment.

The master task's stream condition and consumers' helper address are deployment
bindings; native Snowflake DDL stores concrete names. Re-run the migration when
moving the engine database, monitor schemas, or parameter table. It checks live
DDL drift, refuses to migrate while workflows are active, preserves procedure
grants and the master's state, and alters the master in place. It does not run
workflows or enable previously suspended tasks. Only changed objects in the two
maintained DEV app schemas are exported; configuration rows are never saved.
Provision TST's tables/stream/task and parameters before applying its routing.
The helper needs USAGE for roles invoking CALLER procedures; the migration copies
those grants from the LIM loader. OWNER engine procedures use their owner rights.

## Portable workflow commands

Save workflow and job definitions with `[VARIABLE]` tokens:

```sql
CALL [DATABASE].[MONITOR_APP_SCHEMA].SP_LOAD_RAW_LIM_INCREMENTAL_PERSIST(
    '[DATABASE]'
);

SELECT * FROM [DATABASE].[META_SCHEMA].WORKFLOW_HISTORY
WHERE RUN_ID = '[run_id]';
```

`RAW_SCHEMA` locates raw subject tables, for example
`[DATABASE].[RAW_SCHEMA].RAW_LIM_PYMT`. `APP_SCHEMA` means data-platform metadata (`META`); `META_SCHEMA` means monitor
metadata (`MONITOR_META`). dbt project and target fields default to
`[DATABASE].[DBT_SCHEMA].[DBT_PROJECT]` and `[DBT_TARGET]`. Keep command selections
portable too. New definitions reject fixed DEV/TST/legacy names and the old
`#[run_id]#` syntax.

`SP_RESOLVE_WORKFLOW_COMMAND(VARIANT,VARCHAR)` is the shared resolver used by
both dispatcher overloads and the app's command preview. It reads active
`DEFAULT` / `ENVIRONMENT` parameters at resolution time and returns command
text without executing it. Missing/inactive/empty parameters, secret parameters,
invalid identifiers and an environment database that differs from the engine
database cause a clear error. In SQL, quote string values yourself; inserted
single quotes are escaped. SQL comments and numeric array subscripts are
preserved. Token names use letters/underscores followed by letters/digits/
underscores; bracketed names in command text are reserved for parameters.

dbt targets can appear in the target field or as `--target [DBT_TARGET]` in the
command, but conflicting targets fail. Existing `--vars` must be a JSON object;
the resolver preserves its values and always sets `runId` to the actual run ID.
The optional leading `dbt` executable name is removed before constructing
`EXECUTE DBT PROJECT`. DAG previews use the same resolution and variables.

All app-generated dbt executions include `WRITEBACK = FALSE` outside `ARGS`,
including DAG `list` requests. The resolved command preview displays the same
clause. dbt arguments, target selection and run variables are preserved.

`python backend/migrations/apply_dbt_writeback_default.py --environment-table
KUMO_TST.MONITOR_META.APPLICATION_PARAMETERS` performs a read-only preflight;
add `--apply` to update only that environment's shared command resolver while
preserving grants. It rejects unrelated procedure drift and verifies SQL/dbt
resolution without executing the returned commands. The DAG request change
ships with the next app image through the normal Git release workflow.

The editor, job editor and run dialog automatically show the complete resolved
command as the definition changes. **Refresh** reads current parameter values
again; **Copy** copies the displayed command. Resolution does not execute
SQL/dbt or create a workflow run. When it appears in the command, the all-zero
UUID is labelled as an example; actual runs resolve again with their assigned
ID and the then-current parameter values.

`apply_workflow_command_templates.py` performs a targeted preflight by default.
With `--apply`, it creates the shared resolver, upgrades both dispatchers while
preserving grants, validates distinct converted SQL/dbt definitions, then converts
DEV workflow/job configuration in one transaction. It preserves IDs and schedules
and never executes the returned workflow commands. Only the three changed DDL
objects are read back. Run this conversion before the environment-routing migration; that later migration
updates the resolver/dispatchers to the shared environment helper. Historical workflow runs/logs are preserved as recorded.

## Readiness log location correction

`backend/migrations/move_ready_log_to_monitor_meta.py --apply` moves the existing
DEV `_TMP_SET_READY_LOG` table from `META` into `MONITOR_META`, preserving its
rows and grants, and updates only the three procedures that reference it.
Both current and historical readiness logs use `META_SCHEMA` in the application.
The migration verifies the data fingerprint, preserves the master task state,
and refreshes only the moved table and changed procedures in the DDL copy.

A read-only compatibility view at `KUMO_DEV.META._TMP_SET_READY_LOG` lets the
previously deployed app continue reading until the corrected image is released
through Git. Remove that view after the deployed service uses the new routing.
The focused check `python backend/tests/live_ready_log_schema.py` verifies
refresh, current logging and archival using separate disposable data and monitor
schemas. The compatibility view is not part of the maintained app schema DDL.
The TST provisioning migration also relocates the earlier, empty TST `META`
table into `MONITOR_META`.

## TST monitor schemas

`backend/migrations/provision_tst_monitor_schemas.py` provisions
`KUMO_TST.MONITOR_APP` and `KUMO_TST.MONITOR_META` from the maintained DEV
definitions. Run it without arguments for a read-only preflight, or add
`--apply` to create missing objects. Existing definitions must match; the script
does not replace them or copy DEV configuration/data.

Provisioning completed with 22 tables, 18 procedures, one queue stream and one
master dispatcher. The eight TST environment parameters are stored in
`KUMO_TST.MONITOR_META.APPLICATION_PARAMETERS`. SQL/dbt command resolution and
TST foreign-key/stream bindings were verified. The master remains suspended;
the TST service is a separate setup step.

Use an app build containing the readiness-log routing correction before starting
the TST service. Set its `KUMO_ENVIRONMENT_TABLE` to the TST parameter-table path.
TST does not need a separate DDL snapshot: the DEV definitions and this migration
remain the source for both environments.

`backend/migrations/copy_dev_workflows_to_tst.py --apply` copies the current DEV
workflow containers, jobs, assignments, dependencies, engine definitions,
schedule rows, notifications and email groups into the TST monitor metadata.
Run without `--apply` for a read-only preflight. Commands, IDs, flags, timestamps
and notification settings are preserved as stored in DEV. TST environment
parameters are retained; run history, queue entries and generated tasks are not
copied. The script checks command resolution using TST parameters, refuses to
overwrite differing populated tables, and verifies row counts/fingerprints in
one transaction. It never resumes the master or creates native schedulers.

The 38 `RAW_LIM_PKG_GROUP_SOURCE` configuration rows were also copied unchanged
from DEV into TST and verified by count and fingerprint. All 12 active subjects
have their required TST RAW tables. `RAW_LIM_META` and readiness logs are derived
from TST loads/refreshes rather than copied from DEV.

## Reconcile missing TST workflows into DEV

`python backend/migrations/add_missing_tst_workflows_to_dev.py` compares TST's
legacy execution definitions (`WORKFLOWS`, `WORKFLOW_TASKS`) with DEV's current
`WORKFLOW`, `JOB`, and `DEPENDENCIES` catalog. It resolves all resulting job
commands using both environments' parameter stores without executing them.
Existing command/dependency differences require review; the retired temporary
task cleanup call is the only accepted SQL exception.

Add `--apply` to insert complete missing workflows, jobs, dependency chains,
and required package source mappings in DEV in one guarded transaction.
Existing DEV configuration is preserved. New workflows and schedules remain
disabled until their DEV prerequisites are verified. New package mappings
start with NULL year/sequence checkpoints rather than
copying TST processing state. No DDL, workloads, native tasks, or TST data are
changed. Repeating the migration inserts nothing once the catalogs match.

The CARD source named `CARD SDL EDV` currently selects SDL only; migration
therefore creates `CARD LOAD` with readiness and SDL jobs, without adding an
EDV execution. `RUN DEPENDENCIES` is old engine plumbing. The older TST
`WORKFLOW`/`JOB` prototype entries (`AAAA`, `BBBB`, `MART1`, `MART2`, placeholder
`dbr run xxx` commands, and the unassigned `TEST` SQL job) are outside the
executable business catalog and are not imported by this migration.

## TST promotion after DEV review

Promote only after the user has checked DEV. Deploy the same approved Git/image
release and its affected Snowflake objects together; changing the SPCS image
alone does not migrate the engine. Prepare a targeted TST migration from the
maintained DEV definitions, with all app/metadata bindings resolved to TST.
The earlier provisioning and configuration-copy scripts are not an upgrade
path for TST's populated, older catalog.

The promotion must cover the current `WORKFLOW` and `JOB` definitions, the
`DEPENDENCIES` parent chains, `WORKFLOW_EXECUTION_LOG`, all five `SP_ORCH_*`
engine procedures listed in `apply_parallel_engine.py`, and static
`TASK_ORCH_MAINTENANCE`. Review the shared command resolver and environment
helper against that release too. Preserve TST's environment parameters,
processing checkpoints, history and grants. Do not copy DEV execution state.

Archive the legacy objects with the same `X_` names used in DEV:

- `MONITOR_META.WORKFLOW_RUN_QUEUE` and
  `MONITOR_META.WORKFLOW_EXECUTION_QUEUE_STREAM`.
- The nine procedure overloads listed in `retire_legacy_engine_dev.py`:
  `SP_WORKFLOW_REQUEST_RUN`, `SP_WORKFLOW_DISPATCH`, `SP_RUN_WORKFLOW`,
  `SP_SQL_COMPLETE`, `SP_DBT_COMPLETE`, `SP_RULE_BASED_WORKFLOW_REQUEST_RUN`,
  and `SP_CLEANUP_OLD_TMP_TASKS` in `MONITOR_APP`.
- The old `TASK_WF_MASTER_DISPATCHER` and legacy workflow scheduler/run tasks
  in `MONITOR_APP`. Tasks and streams need the same guarded clone/drop
  treatment as DEV; native renames cover the queue and procedures.

The DEV retirement script deliberately cannot target TST. Implement and review
the TST cutover migration before applying these changes. Stop old admissions,
drain old executions, and suspend the old tasks before archiving. Set TST's
`ORCHESTRATION / LEGACY_ENGINE_RETIRED=true` only after the replacement engine
and catalog are verified. Prepare generated schedulers suspended; activate
only the schedules approved for TST after execution validation. Exclude these
generated tasks from saved DDL.

The reconciliation review records outstanding prerequisites: CARD's dbt files
are missing from DEV. Completion emails are restored in DEV using the targeted
notification migration below; their rollout to ACC remains separate. The DEV
dependency dispatcher supports downstream execution. Resolve outstanding prerequisites
before treating the new engine as a replacement for TST's scheduled pipeline.
The current DEV reconciliation and release do not change TST objects.

## DEV retirement of the old execution engine

`python backend/migrations/retire_legacy_engine_dev.py` performs a targeted
read-only preflight. `--apply` retires DEV only after the old engine has no
pending executions and unfinished new jobs have no references to retired procedures.
Independent new executions continue running. It suspends and `X_`-renames the old dispatcher and generated
execution tasks, nine procedure overloads, the old run queue, and its stream.
Tables/procedures use native renames. Tasks and the stream use clone/drop because
Snowflake does not support their renaming; the clones retain definitions, grants,
and the stream offset, and tasks remain suspended. Old native task history
remains associated with the former task identities. TST is never addressed by this migration.
Old catalog tables, history, and logs remain available for historical reads.

The migration stores `ORCHESTRATION / LEGACY_ENGINE_RETIRED=true` in DEV's
application parameters. Restart the local backend to load it. The maintained
app then blocks old run/configuration/engine-control writes with HTTP 410,
disables its old recovery worker, routes historical queue reads to the archive,
and opens the new Monitor by default. The sidebar groups the current pages under
Workflow; the legacy navigation group has been removed.
The old temporary-task cleanup invocation is removed from the new maintenance
job; new temporary tasks are already handled by `TASK_ORCH_MAINTENANCE`.

No business workloads or new schedules are activated by retirement. Downstream
workflow dispatch and completion notifications are supported in DEV. Schedule controls are now available in the Monitor. Source changes reach
SPCS through the normal Git/image release process; this migration does not deploy
an image or change a service.

`apply_execution_states.py --apply` updates only the four new engine procedures
changed for complete per-job state tracking. The disposable
`backend/tests/live_execution_states.py` checks full/only/from scopes, live states,
temporary task cleanup, and job-scoped model/test progress; `--progress-only`
checks the telemetry reader without running any jobs.

## Monitor controls and execution details

The Monitor provides direct DAG and Log shortcuts and quick workflow, global job,
and schedule enable/disable actions. Quick writes change only the selected flag,
check the previously displayed state, and preserve concurrent definition edits.
Accepted executions retain their snapshotted jobs and commands. Disabling a
workflow pauses its scheduler without changing its schedule-enabled preference;
reenabling the workflow resumes that schedule. Disabling a schedule suspends its
native scheduler, retaining it for reuse. Future accepted runs use the new flags.

Schedule editing creates or updates only the containing workflow's persistent
scheduler, validates its expected body and engine comment, and resumes it only
when both the workflow and schedule are enabled. Manual runs continue to create
temporary workflow tasks. Jobs never own native tasks. Catalog changes commit
before scheduler DDL; synchronization errors are reported and the Monitor reads
the actual native state. Saved intent is checked again after DDL to converge on
concurrent changes. Only generated tasks are affected; no DDL export is required.

Last Run and Duration describe each row's latest actually started execution,
independently of a newer skipped/queued request. Schedule hides stored cron text
when disabled. Next Run uses the selected scheduler's Snowflake TASK_HISTORY,
scoped by task, database and schema. Snowflake reports future occurrences within
its eight-day window; absent native timestamps are shown as not reported, never
as a locally guessed date. Disabled workflows/jobs have no displayed Next Run.

Workflow DAGs show the selected execution's frozen job chain and statuses, or
configured jobs before the first execution. DBT job DAGs use that job's own model
and test progress. Workflow logs include all job results and their model, test
and event sources; job logs restrict those sources to that job execution ID.
The maintained DAG and log components serve both new orchestration and archived
views through explicitly selected APIs. New execution details never read old
workflow history or use a parent workflow run ID to fetch job telemetry.

```text
PATCH /api/orchestration/workflows/<workflow_id>/state
PATCH /api/orchestration/workflows/<workflow_id>/schedule
PATCH /api/orchestration/jobs/<job_id>/state
GET   /api/orchestration/workflows/<workflow_id>/dag?runId=...&jobId=...
GET   /api/orchestration/workflows/<workflow_id>/log?runId=...&jobId=...
```

`backend/tests/live_monitor_controls.py` creates one disposable future-scheduled
SELECT job/workflow, checks the native scheduler lifecycle and Next Run through
the API, and removes its rows and scheduler. It never executes the job.

## DEV workflow notifications

Preview, then apply the DEV-only migration:

```bash
python backend/migrations/apply_workflow_notifications.py
python backend/migrations/apply_workflow_notifications.py --apply
python backend/tests/live_workflow_notifications.py --run
```

It maps `WORKFLOW_NOTIFICATIONS` through both workflow and job migration
identities, preserving the original rows and any settings already saved for
new workflow IDs. Matching recipient groups across readiness/SDL/EDV steps
become one workflow rule; conflicting groups or integrations stop the migration.
An old enabled flag with no recipient group remains effectively disabled.
DEV restores existing rules only. Do not enable previously unconfigured DEV
workflows. For the later ACC rollout, enable failure emails for **every workflow**
as requested; do not apply DEV's restore-only coverage there. This migration
deliberately has no ACC/TST apply option.

The migration checks idle DEV executions, routing, ownership and known procedure
bodies. It changes only `SP_GET_ENVIRONMENT`, `SP_ORCH_NOTIFY`,
`SP_ORCH_EXECUTE_WORKFLOW`, and `SP_ORCH_RECONCILE`, preserving grants. The
environment helper exposes its bootstrap parameter-table address so activation
uses that same configuration store. The initial activation timestamp is saved
as `ORCHESTRATION / NOTIFICATIONS_START_AT`; reruns preserve it. Completions
before that timestamp never generate catch-up mail. No schedules, business
workloads, services or notification integrations are changed by the migration.

Failures and cancellations use the failure rule; success and warning outcomes
use the optional success rule. A workflow run generates one email with its
frozen workflow name, run ID and failed job errors/query IDs, including partial
manual runs. The existing `SP_SEND_WORKFLOW_EMAIL` formats and sends it through
the configured Snowflake integration. Normal completion and engine failures
invoke the notifier; housekeeping also catches missing notification attempts
after interrupted completion or native task failure.

Each run stores its notification outcome in
`WORKFLOW_EXECUTION_LOG.RESULT.notification`. An atomic durable claim prevents
concurrent completion/recovery calls from sending duplicate emails. Mail failures
record `ERROR` without changing workflow/job status or blocking downstream
dispatch. `SENDING` or `ERROR` needs operator review: Snowflake may have accepted
an email before its reply or the final result write was lost, so recovery never
automatically resends an attempted email. `SENT` means Snowflake accepted the
send; mailbox delivery is not established by that status.

The opt-in live check uses a private transient execution table and disposable
procedure copies with an isolated sender. It validates Snowflake SQL, the
failure payload, duplicate suppression, and historical/disabled skip paths
without emailing anyone. It also runs one disposable division-by-zero job
through the real engine to verify its completion hook. That workflow has no
email rule and cannot send mail. All validation fixtures are removed afterwards.

Notifications → Workflow Config reads the current catalog when the old engine
is retired, and supports editing success/failure groups and the integration.
The app source changes follow the normal Git/image release process; applying
this database migration does not deploy the updated configuration screen.
