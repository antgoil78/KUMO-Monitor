# Environment selection in SPCS

App releases go through Git and
[`deploy-image.yml`](../../.github/workflows/deploy-image.yml). Push to `main`
to trigger it automatically, or use its manual dispatch to publish a feature
branch. The workflow builds and publishes the versioned container image. It
does not execute `ALTER SERVICE`; updating the running service is a separate
promotion step. Use the GitHub Actions path for releases unless a direct service
update is explicitly requested.

Set `KUMO_ENVIRONMENT_TABLE` to the full environment parameter table path in
`spec.containers[].env`. This is required for a connected service. The backend
loads its database and schemas from that table before importing repositories.
The table can be in a different database/schema from the monitored objects.
Use the same built image for every environment.

`KUMO_ENV=DEV` or `TST` selects the display label and mock defaults;
`KUMO_DEPLOYMENT_ENV` can label a service DEV, TEST, UAT or PROD. These labels
do not override database/schema parameters. `/api/health` exposes the bootstrap
address and actual resolved namespaces. Restart after changing the address or
routing parameters. Bind the engine to the same table with the
[environment-routing migration](../README.md#app-and-engine-routing).

The renderer defaults the service object to
`KUMO_<environment>.MONITOR_APP.KUMO_MONITOR_SERVICE`; that deployment address
is separate from application routing. Snowflake's `SNOWFLAKE_DATABASE` and
`SNOWFLAKE_SCHEMA` values do not redirect the application away from its table.
The renderer accepts `--environment-table` and includes it in the specification.

Prepare SQL locally, substituting existing resource names and an image built
from this branch (the resource names below are examples):

```bash
KUMO_ENV=DEV python snowflake/spcs/render_service.py \
  --environment-table KUMO_DEV.MONITOR_META.APPLICATION_PARAMETERS \
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
for subsequent deployments. The create script records the initial published
version `build-215-1-022ffedd2bf2`. The update script selects GitHub Actions image
`build-216-1-0ce9611cf217` and its verified digest. This update was executed on
20 October 2026 at the user's explicit request; the running container was verified
`READY` with the published digest. The promotion receipt is
[`dev-spcs-actions-promotion.json`](../../review/dev-spcs-actions-promotion.json).
Change both tag and digest together when selecting another
build: a stale digest can keep the previous image running despite a new tag.
The update preserves existing environment settings, endpoint configuration and
resources. Setup and creation were executed on 19 October 2026; the DEV service
is running with `KUMO_ADMIN` as owner. The initial deployment receipt is recorded
in [`dev-service-deployment-result.json`](../../review/dev-service-deployment-result.json),
and the readiness updates are recorded in
[`dev-lim-readiness-service-deployment.json`](../../review/dev-lim-readiness-service-deployment.json)
and [`dev-spcs-app-release.json`](../../review/dev-spcs-app-release.json).
These scripts are not executed automatically by CI.

DEV endpoint: <https://mnztue-vymkbmw-ov24823.snowflakecomputing.app>. The
container reports `READY` and its health probe returns HTTP 200. The public URL
redirects to Snowflake sign-in, and the user confirmed that the DEV monitor loads
in the browser. Automated session-token verification separately received HTTP
500 from Snowflake ingress before reaching the application; that check did not
verify browser authentication. The latest release also runs HTTP checks from a
temporary SPCS job against the monitor's internal endpoint. These checks exercise
the deployed React bundle, DEV/BATCH_CONTROL context, live Snowflake reads,
PAAR accepted/blocked boundaries, history, and invalid reload requests. Results
are recorded in the release receipt above. They submit no valid load or workflow
request. The one-off test job is removed after collecting its results.

The smoke payload is
[`dev_spcs_readiness_smoke.py`](../checks/dev_spcs_readiness_smoke.py). Supply
`KUMO_SMOKE_BASE_URL=http://<service-dns-name>:5000` and
`KUMO_SMOKE_IMAGE_VERSION=<published-tag>` to a job using the published image.
Set `KUMO_SMOKE_HISTORY_ROWS` to the live PAAR row count in
`KUMO_DEV.MONITOR_META._SET_READY_LOG` over the last 30 days, and execute the
payload with Python. An empty archive is valid when only the current run exists.
Its PAAR count and date assertions describe
the verified 20 October dataset; review them before reusing it after another
load. It emits a single `SPCS_SMOKE_RESULT` JSON record with aggregate results.

First run [`prepare_dev_service.sql`](prepare_dev_service.sql). It uses
`ACCOUNTADMIN` for the one-time compute pool `USAGE` and public endpoint grants
to `KUMO_ADMIN`, then resumes the shared pool. Check `DESCRIBE COMPUTE POOL`
and wait for `IDLE` or `ACTIVE` before running the create script. The create
script uses `KUMO_ADMIN` to create and own the DEV service, using the existing
DEV data access and shared repository ownership of that role. Resuming the pool
provisions compute for the new service. The update script uses that same owner.
Other application users need the appropriate service endpoint access.

Both specifications set `KUMO_ENV=DEV` and the full `KUMO_ENVIRONMENT_TABLE`
bootstrap address, disable automatic scheduler resume,
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
DEV service owner grants and deployment are in place. The PAAR readiness and
SDL/EDV workflow completed successfully on 20 October; its table verification is
recorded in [`paar-dev-load-verification.json`](../../review/paar-dev-load-verification.json).
Automated browser authentication remains unverified. `META.WORKFLOW_DEP_RULE` is intentionally absent.
The renderer does not create any of these prerequisites or shared resources.

References:

- [Service specification environment variables](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/specification-reference#containers-env-field)
- [Service database/schema context and authentication](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/spcs-execute-sql)
