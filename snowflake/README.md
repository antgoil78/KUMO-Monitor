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

`backend/migrations/` keeps the current DEV readiness change and the environment
parameter migration, with `migration_support.py` providing shared helpers.
The readiness migration verifies the three procedures it changes and reads
back only those changed definitions. It never performs a full DDL export.
The optional live readiness checks in `backend/tests/live_dev_lim_readiness*.py`
use disposable synthetic fixtures; they are not routine checks or backups.

The DEV readiness implementation registers nonempty payload files in
`META.LIM_BATCH_CTRL` and records validated empty deliveries in the current and
historical readiness logs. It does not require RAW readiness flag columns.
The loader and readiness procedures remain restricted to DEV. TST provisioning
must use the reviewed DEV implementation and its environment configuration.

## Workflow environment parameters

`backend/migrations/apply_workflow_environment_parameters.py --apply` renames
DEV's old `APP_SCHEMA=MONITOR_APP` to `MONITOR_APP_SCHEMA` and adds:

| Parameter | DEV value |
| --- | --- |
| `DATABASE` | `KUMO_DEV` |
| `MONITOR_APP_SCHEMA` | `MONITOR_APP` |
| `META_SCHEMA` | `MONITOR_META` |
| `APP_SCHEMA` | `META` |
| `DBT_SCHEMA` | `DBT_APP` |
| `DBT_PROJECT` | `KUMO_DEV` |
| `DBT_TARGET` | `kumo_DEV` |

All seven use instance `DEFAULT`, group `ENVIRONMENT`, type `STRING`, and are
active and non-secret. `DATABASE` and `META_SCHEMA` retain their earlier meanings.
Without `--apply`, the migration performs a read-only preflight. It checks the
live dbt project/default target, commits changes in one transaction, refuses
conflicting values, and preserves matching rows on repeat runs.

`DBT_SCHEMA` locates the project object; model output schemas remain in the dbt
profile. Runtime token substitution is not implemented yet. These are row-only
changes and never require a DDL refresh.
