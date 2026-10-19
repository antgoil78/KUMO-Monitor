# Environment selection in SPCS

Set `KUMO_ENV` to `DEV` or `TST` at deployment time. The container receives it
under `spec.containers[].env`; the backend derives its database and object
locations from this selector. The frontend receives the same context from
`/api/health`. Use the same built image for either environment.

The top bar identifies the service with yellow DEV, green TEST, blue UAT, or
red PROD. `TST` is displayed as TEST. The template sets `KUMO_DEPLOYMENT_ENV`
from the selector by default. Use `--deployment-env UAT` or `PROD` for the
future deployments on their separate accounts. This label never changes
database references or selects an account; `KUMO_ENV` remains the DEV/TST
database selector. Those accounts' database layouts must be agreed separately.

The Snowflake service object also needs a database and schema. The renderer
derives `KUMO_<environment>.MONITOR_APP.KUMO_MONITOR_SERVICE` from the same
selector so the service location and container configuration agree.
Snowflake supplies `SNOWFLAKE_DATABASE` and `SNOWFLAKE_SCHEMA` from that service
location. These do not need to be set in the YAML. The application rejects a
database override that conflicts with `KUMO_ENV`.

Prepare SQL locally, substituting existing resource names and an image built
from this branch (the resource names below are examples):

```bash
KUMO_ENV=DEV python snowflake/spcs/render_service.py \
  --image /kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-123-1-a1b2c3d4e5f6 \
  --compute-pool KUMO_SHARED_MONITOR_POOL \
  --query-warehouse KUMO_ELT_GEN_1 \
  --owner-role KUMO_MONITOR_DEV_OWNER \
  --output /tmp/create-kumo-monitor-dev.sql
```

This only writes SQL. It never connects to Snowflake, creates resources,
alters the existing service, or enables tasks. Use `--env TST` later, after
the corresponding TST schemas and objects have been provisioned.

The service owner role needs access to its database, both monitor schemas,
the required `META`/`RAW_LIM`/data objects, procedures, stage, warehouse, compute
pool, image repository, and endpoint creation. Grant endpoint access to the
intended application users after deployment. The template keeps caller rights
for user identity; application reads/writes continue to use the service owner.
`KUMO_LIM_ROLE` is set to that same owner for File Ingestion's role selection.
Keep passwords out of the spec: the app uses the Snowflake-provided service
OAuth token.

## DEV worksheet scripts

[`create_dev_service.sql`](create_dev_service.sql) creates
`KUMO_DEV.MONITOR_APP.KUMO_MONITOR_SERVICE` in `KUMO_SHARED_MONITOR_POOL`.
[`alter_dev_service.sql`](alter_dev_service.sql) updates that same DEV service
for subsequent deployments. Both pin the verified published version
`build-215-1-022ffedd2bf2`; replace the image tag with the selected published
version when deploying another build. Setup and creation were executed on
19 October 2026; the DEV service is running with `KUMO_ADMIN` as owner. The
update script was not needed because creation applied the same specification
and query warehouse. The deployment receipt is recorded in
[`dev-service-deployment-result.json`](../../review/dev-service-deployment-result.json).
These scripts are not executed automatically by CI.

DEV endpoint: <https://mnztue-vymkbmw-ov24823.snowflakecomputing.app>. The
container reports `READY` and its health probe returns HTTP 200. The public URL
redirects to Snowflake sign-in. Automated authenticated verification received
HTTP 500 from Snowflake ingress before reaching the application; signed-in
browser verification is pending.

First run [`prepare_dev_service.sql`](prepare_dev_service.sql). It uses
`ACCOUNTADMIN` for the one-time compute pool `USAGE` and public endpoint grants
to `KUMO_ADMIN`, then resumes the shared pool. Check `DESCRIBE COMPUTE POOL`
and wait for `IDLE` or `ACTIVE` before running the create script. The create
script uses `KUMO_ADMIN` to create and own the DEV service, using the existing
DEV data access and shared repository ownership of that role. Resuming the pool
provisions compute for the new service. The update script uses that same owner.
Other application users need the appropriate service endpoint access.

Both specifications set `KUMO_ENV=DEV`, disable automatic scheduler resume,
preserve the supplied 7200-second statement/network timeouts, and use the
`/api/health` readiness probe. The service location supplies its database and
schema, so those environment variables do not need to be repeated. Image paths
use `/database/schema/repository/image:tag`, without a registry hostname or
Markdown link. Both scripts finish by showing service status and endpoints.

`CREATE SERVICE IF NOT EXISTS` leaves an existing DEV service unchanged. Use
the update script to apply a new specification. Neither script targets the
existing `KUMO_ADMIN.KUMO_MONITOR.KUMO_MONITOR_SERVICE` instance. The update
script does not move a service between compute pools.

## Shared runtime infrastructure

The new DEV and TST services share these resources within their account:

| Resource | Location |
| --- | --- |
| Image repository | `KUMO_SHARED.MONITOR_APP.MONITOR_REPOSITORY` |
| Compute pool | `KUMO_SHARED_MONITOR_POOL` (account-level object) |

Compute pools, SQL warehouses and account roles are account-level objects and
cannot be placed inside a database. The pool's shared prefix identifies its
purpose. Each service remains in its own environment's `MONITOR_APP` schema,
with separate application configuration and grants.

[`create_shared_infrastructure.sql`](create_shared_infrastructure.sql) creates
the shared schema, image repository and pool. It was applied on 19 October 2026:
`KUMO_ADMIN` created the schema and repository, and `ACCOUNTADMIN` created the
pool because `KUMO_ADMIN` lacked the account-level `CREATE COMPUTE POOL` privilege.
The script preserves existing objects and creates the pool initially suspended with
automatic resume disabled, using the same `CPU_X64_XS`, one-node sizing as the
current monitor pool. The shared pool was resumed for the DEV deployment.
The existing ADMIN service
and `MONITOR_COMPUTE_POOL` are unchanged.

The service renderer defaults to `KUMO_SHARED_MONITOR_POOL`; `--compute-pool`
can select another provisioned pool. CI publishes to
`<registry>/kumo_shared/monitor_app/monitor_repository/kumo-monitor:build-<run-number>-<attempt>-<12-character-commit>`.
Each build and rerun gets its own tag, and no `latest` alias is pushed. The same
version is baked into the image so the application displays the deployed tag.
The Actions summary provides the exact SPCS image path to use with `--image`.
The example above uses a placeholder build tag; replace it with a published one.

DEV and TST can select different tags in their service specifications, or promote
the exact DEV image to TST after verification. Publishing a new image does not
change existing services. Keep `KUMO_ENV=DEV` or `TST` in each service regardless
of the image version. For an existing service, update its container `image` in
the service specification; the renderer above prepares only new services.

The repository is provisioned. Grant any additional publisher roles write access
and service owners read access before publishing/deploying. To change the CI
destination, set `KUMO_IMAGE_REPOSITORY` to the full repository/image URI without
a tag and set `SNOWFLAKE_REGISTRY` to its hostname. Legacy `KUMO_IMAGE_URI`
remains a fallback, but any supplied tag is replaced by the generated version.

UAT and PROD will need their own image repositories and compute pools in their
respective Snowflake accounts. Publish the image to each account's repository
and set the corresponding registry hostname in CI. `KUMO_SHARED` is shared
within an account; it is not a global repository across those accounts.

Generated scheduler tasks stay suspended by default in DEV/TST. Set
`KUMO_AUTO_RESUME_TASKS=true` only when scheduling should be enabled. This
setting governs application-created schedulers; persistent dispatcher and
procedure-generated execution tasks retain their separate lifecycle.

Current readiness: DEV definitions and workflow/job configuration have been
copied, with executable references directed to DEV and copied schedules disabled.
DEV service owner grants and deployment are in place; application user access
and workflow execution validation remain. `META.WORKFLOW_DEP_RULE` is intentionally absent.
The renderer does not create any of these prerequisites or shared resources.

References:

- [Service specification environment variables](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/specification-reference#containers-env-field)
- [Service database/schema context and authentication](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/spcs-execute-sql)
