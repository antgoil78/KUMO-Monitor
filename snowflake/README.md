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
