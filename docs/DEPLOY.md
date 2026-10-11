# Deploying CAOS to Cloud Run

One service, one origin: Cloud Run serves the workspace, the API and the run stream.
Nothing in this file is a secret. The two secret values are created by the owner in
their own terminal and are never echoed or written to a file.

Prerequisites (owner): a Firebase project (`caos-firebase-prod`) on the Blaze plan;
`gcloud auth login`; `firebase login`.

## 1. Authentication (CLI config, then console settings)

Email/Password sign-in is committed config (`firebase.json`, `.firebaserc`):

```bash
npx -y firebase-tools@latest deploy --only auth --project caos-firebase-prod
```

The `auth` deploy target ships in firebase-tools (`lib/deploy/auth`), and Firebase's own
firebase-auth-basics agent skill documents the `firebase.json` `auth.providers` block and
this command; it is unverified here until the first deploy (FD18). If the CLI rejects the
target, enable Email/Password in the console instead: Authentication > Sign-in method >
Email/Password.

The web app's public config comes from the Firebase MCP `firebase_get_sdk_config`
(platform `web`) after `firebase_create_app` (platform `web`, display name `caos-web`).
Its `apiKey` and `authDomain` feed `_FIREBASE_API_KEY` and `_FIREBASE_AUTH_DOMAIN` in
`cloudbuild.yaml`. The SPA authenticates every request with the `__session` cookie; it
never sends an `Authorization` header.

Console settings the config does not cover:

- Authentication, Settings, User actions: uncheck "Enable create (sign-up)" so only
  owner-created users exist.
- Do NOT enable reCAPTCHA or email-enumeration protection: they load third-party
  scripts the app's CSP blocks.
- Do not add users in the console: they start with emailVerified=false. Section 7's
  script creates them verified (`create`), or fixes one already made (`verify`).

## 2. Project, APIs, registry, database, bucket

```bash
PROJECT=<firebase project id>; REGION=europe-west2; gcloud config set project $PROJECT
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com identitytoolkit.googleapis.com \
  containeranalysis.googleapis.com
gcloud artifacts repositories create caos --repository-format=docker --location=$REGION
gcloud storage buckets create gs://${PROJECT}_cloudbuild --location=$REGION --uniform-bucket-level-access
gcloud sql instances create caos-pg --database-version=POSTGRES_17 --edition=ENTERPRISE \
  --cpu=1 --memory=3840MiB --region=$REGION --backup-start-time=02:00 \
  --enable-point-in-time-recovery --retain-backups-on-delete --deletion-protection \
  --insights-config-query-insights-enabled --ssl-mode=ENCRYPTED_ONLY
gcloud sql instances describe caos-pg \
  --format='yaml(settings.backupConfiguration,settings.insightsConfig,settings.deletionProtectionEnabled)'
gcloud sql databases create caos --instance=caos-pg
gcloud storage buckets create gs://$PROJECT-caos-blobs --location=$REGION \
  --uniform-bucket-level-access --public-access-prevention
```

`--cpu=1 --memory=3840MiB` is the 1-vCPU, 3.75 GB dedicated-core Enterprise machine;
the gcloud reference says not to pass `--tier` for dedicated-core Enterprise instances,
so `db-custom-1-3840` is not used as a flag value.

`--retain-backups-on-delete` keeps automated and on-demand backups after the instance is
deleted. `--ssl-mode=ENCRYPTED_ONLY` refuses unencrypted connections. No authorized
networks are added: the only path in is Cloud Run's Cloud SQL socket and the Auth Proxy.
`--connector-enforcement=REQUIRED` is deliberately not set: no fetched doc confirms that
Cloud Run's built-in `--add-cloudsql-instances` socket counts as a connector, and the flag
disables authorized networks and rejects direct connections. Test it on a scratch
instance before ever enabling it on `caos-pg`.

Proves: each command exits 0; `gcloud sql instances list` shows `caos-pg` RUNNABLE; the
`describe` shows `enabled: true` for backups, `pointInTimeRecoveryEnabled: true`,
`queryInsightsEnabled: true` and `deletionProtectionEnabled: true`.

## 3. Secrets (owner, own terminal)

The database password is generated once and never echoed. gcloud logs command
arguments to `~/.config/gcloud/logs` at DEBUG, so the one command that takes the
password as an argument runs with file logging disabled
(`CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true`). The URL secret is built with `printf` and
read from stdin, so the password is never in argv there.

```bash
DBPW=$(openssl rand -hex 24)
CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true gcloud sql users create caos --instance=caos-pg --password="$DBPW"
printf 'postgresql://caos:%s@/caos?host=/cloudsql/%s:%s:caos-pg' "$DBPW" $PROJECT $REGION \
  | gcloud secrets create caos-database-url --data-file=-; unset DBPW
read -rs KEY && printf %s "$KEY" | gcloud secrets create openrouter-key --data-file=-; unset KEY   # paste the key, Enter; nothing is echoed
printf '%s' '<owner email>=ANALYST;<viewer email>=READER' | gcloud secrets create caos-role-map --data-file=-
```

Proves: `gcloud secrets list` shows the three names (it never shows a value).

Recovery: if the first deploy fails its startup probe and `/api/health` reports
`store: STORE_UNAVAILABLE`, the URL secret and the user's password disagree. Generate a
new value and set both, then bump the pinned `caos-database-url` version in §6's
`--set-secrets` to the new version number (`gcloud secrets versions list caos-database-url`):

```bash
DBPW=$(openssl rand -hex 24)
CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true gcloud sql users set-password caos --instance=caos-pg --password="$DBPW"
printf 'postgresql://caos:%s@/caos?host=/cloudsql/%s:%s:caos-pg' "$DBPW" $PROJECT $REGION \
  | gcloud secrets versions add caos-database-url --data-file=-; unset DBPW
```

## 4. Service account permissions

```bash
gcloud iam service-accounts create caos-run --display-name "CAOS Cloud Run"
gcloud iam service-accounts create caos-build --display-name "CAOS Cloud Build"
BSA=caos-build@$PROJECT.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$BSA --role roles/artifactregistry.writer
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$BSA --role roles/logging.logWriter
gcloud storage buckets add-iam-policy-binding gs://${PROJECT}_cloudbuild --member serviceAccount:$BSA --role roles/storage.objectViewer
SA=caos-run@$PROJECT.iam.gserviceaccount.com
gcloud secrets add-iam-policy-binding openrouter-key --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding caos-database-url --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding caos-role-map --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
gcloud storage buckets add-iam-policy-binding gs://$PROJECT-caos-blobs --member serviceAccount:$SA --role roles/storage.objectUser
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role roles/cloudsql.client
```

## 5. Build

The web `apiKey` is the one §1 read from the Firebase MCP `firebase_get_sdk_config`.

The build uploads the working directory, so it must be clean (`.gcloudignore` also keeps
`.superpowers/` and `.gitnexus/` out of the upload):

```bash
IMAGE=$REGION-docker.pkg.dev/$PROJECT/caos/caos:$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain)" ]; then
  echo 'working tree not clean: commit or stash first'
else
  gcloud builds submit --config cloudbuild.yaml \
    --substitutions=_IMAGE=$IMAGE,_FIREBASE_API_KEY=<web apiKey>,_FIREBASE_AUTH_DOMAIN=$PROJECT.firebaseapp.com
fi
```

`cloudbuild.yaml` runs the build as `caos-build` (a user-specified service account, so
logs go to Cloud Logging only), pins the builder image by digest, and sets
`requestedVerifyOption: VERIFIED` so a build succeeds only if provenance is generated.
Every image is pinned as `tag@sha256:digest`: node, python, uv, the Dockerfile frontend
(`docker/dockerfile`) and the cloud-builders docker builder; refresh the digests
deliberately with `docker buildx imagetools inspect <tag>`.

Not found in the fetched docs: whether `VERIFIED` works with a user-specified service
account, any extra role it needs (`roles/artifactregistry.writer` includes
`artifactregistry.attachments.create`), and the minimal role for the source upload of
`gcloud builds submit` (the Go guide lists Storage Admin). If the first build fails on
either, report the error text rather than widening roles blindly.

Deploy by digest, and check the provenance:

```bash
IMAGE_DIGEST=$(gcloud artifacts docker images describe $IMAGE --format 'value(image_summary.fully_qualified_digest)')
gcloud artifacts docker images describe $IMAGE_DIGEST --show-provenance
```

Apply the registry cleanup policy (keep the 10 most recent versions, delete untagged
versions older than 30 days). Dry-run first, then apply; the docs' examples disagree on
the default, so pass the flag explicitly. Changes take effect within about a day:

```bash
gcloud artifacts repositories set-cleanup-policies caos --location=$REGION --policy=ops/ar-cleanup.json --dry-run
gcloud artifacts repositories set-cleanup-policies caos --location=$REGION --policy=ops/ar-cleanup.json --no-dry-run
```

Proves: the image builds from the clean working tree and is in Artifact Registry.

## 6. Deploy

Set `CAOS_PUBLIC_ORIGIN` on this first deploy from the deterministic service URL
`https://caos-<project-number>.<region>.run.app`. Firebase mode refuses to boot
without it, so there is no placeholder second deploy. Requests through the legacy
`*.a.run.app` URL get 403 ORIGIN_REFUSED on writes; always use the URL below.
`CAOS_ROLE_MAP` is `email=ROLE;email=ROLE`; an empty entry (a trailing `;`) is
skipped, anything else malformed refuses the boot; it arrives from the `caos-role-map`
secret (FD24). The `^@^` prefix makes `@` the env separator so the commas in
`CAOS_MODEL_PRICE` survive. `--allow-unauthenticated`
lets the browser reach the service; sign-in is the app's own. `CAOS_RUN_CEILING=120.00`
is set explicitly, as at the fork point (FD11): it bounds each run's reservation, and the
OpenRouter key's own limit is the hard spend stop.

Deploy only while no run is in flight: a deploy restarts the only instance (spec §3a H1).

`--concurrency 40` is `STREAM_LIMIT` (24, `caos/api/stream.py`) plus 16 for the API.
`--cpu-boost` gives extra CPU at startup. The startup probe allows 24 failures at a 5 s
period (about 2 minutes) for `/api/health` to answer. The secrets are pinned to
versions (`:1`); bump the number explicitly on rotation, never use `:latest`.
`--startup-probe` and `--cpu-boost` syntax is from the gcloud reference; the docs do not
state whether either flag restarts anything beyond the new revision every deploy creates.

```bash
NUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
SA=caos-run@$PROJECT.iam.gserviceaccount.com
ORIGIN=https://caos-$NUM.$REGION.run.app
gcloud run deploy caos --image $IMAGE_DIGEST --region $REGION --port 8000 --no-cpu-throttling \
  --min-instances 1 --max-instances 1 --timeout 3600 --memory 2Gi --cpu 1 \
  --execution-environment gen2 --allow-unauthenticated \
  --service-account $SA --cpu-boost --concurrency 40 \
  --startup-probe=httpGet.path=/api/health,initialDelaySeconds=0,periodSeconds=5,failureThreshold=24,timeoutSeconds=4 \
  --add-cloudsql-instances $PROJECT:$REGION:caos-pg \
  --add-volume name=blobs,type=cloud-storage,bucket=$PROJECT-caos-blobs \
  --add-volume-mount volume=blobs,mount-path=/data/blobs \
  --set-secrets OPENROUTER_API_KEY=openrouter-key:1,CAOS_DATABASE_URL=caos-database-url:1,CAOS_ROLE_MAP=caos-role-map:1 \
  --set-env-vars "^@^CAOS_BLOB_ROOT=/data/blobs@CAOS_FIREBASE_PROJECT=$PROJECT@CAOS_MODEL_ENDPOINT=openai/gpt-6-luna@CAOS_MODEL_PRICE=openai/gpt-6-luna,0.0000002,0.000001,2026-10-04@CAOS_REASONING_EFFORT=high@CAOS_PROVIDER_ORDER=openai@CAOS_RUN_CEILING=120.00@CAOS_PUBLIC_ORIGIN=$ORIGIN"
URL=$(gcloud run services describe caos --region $REGION --format='value(status.url)')
echo "$URL"; echo "$ORIGIN"
```

After the first successful deploy, `service.yaml` becomes the source of truth:

```bash
gcloud run services describe caos --region $REGION --format export > service.yaml
```

Review it. The role map is a secret, not an env var, so no email should be in the file; this must print nothing before you commit (if it prints anything, such as creator or lastModifier annotations, remove those lines first):

```bash
grep -nE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' service.yaml | grep -v 'iam.gserviceaccount.com'
```

Then commit it (`--format export` already omits status and generated labels, so
there is no field-deletion step; the docs name none for a Cloud Run to Cloud Run round
trip). Later deploys use `gcloud run services replace service.yaml --region $REGION`
(`--dry-run` validates only). Rollback means replacing with the previous image digest in
`service.yaml`. To change the role map, add a `caos-role-map` secret version and bump its pin. Never split traffic: two revisions run two in-process workers.

Both URLs serve. Use `$ORIGIN` in the browser: writes through the legacy
`*.a.run.app` URL get 403 ORIGIN_REFUSED, and `CAOS_PUBLIC_ORIGIN` stays `$ORIGIN`.

Email/password sign-in should not need the service host among Firebase's authorised
domains (spec 2026-10-11 §5, V4; confirm on the first sign-in). Only if a sign-in is
refused with `auth/unauthorized-domain`, add the host (`$ORIGIN` without `https://`, i.e.
`caos-$NUM.$REGION.run.app`) under Firebase console, Authentication, Settings,
Authorised domains.

If `/api/health` reports `blobs=BLOB_ROOT_UNAVAILABLE`, the Cloud Storage mount is not
writable by the image's user (uid 10001). Rerun the deploy above with the volume line
replaced by:

```bash
  --add-volume name=blobs,type=cloud-storage,bucket=$PROJECT-caos-blobs,mount-options="uid=10001;gid=10001" \
```

If health is OK but uploads fail on the mount with `STORE_UNAVAILABLE` or
`INTERNAL_FAULT`, the suspect is the directory fsync after each blob's rename
(`_sync_directory` in `caos/blobs.py`) on Cloud Storage FUSE; report it rather than
patching it in place.

The model price is the owner's 2026-10-03 figure; change it only on the owner's word.

## 7. Create the users, email verified

Users created in the Firebase console start with `emailVerified=false`, and the server
refuses them. `scripts/firebase_users.py` creates each user with the email already
verified through the Firebase Admin SDK (FD19). It runs on the owner's machine only
and is not in the image. It reads the password without echo, sends it once to
Firebase, and never prints or logs it; a refusal prints only the error's class.

Firebase Auth refuses gcloud's default OAuth client for application-default
credentials, so create a Desktop OAuth client once: in the console open APIs &
Services > Credentials > Create credentials > OAuth client ID > Desktop app, then
download the JSON. Keep it outside the repo, for example
`~/.config/caos/oauth-desktop.json`, and never commit it.

```bash
gcloud auth application-default login --client-id-file=$HOME/.config/caos/oauth-desktop.json
gcloud auth application-default set-quota-project "$PROJECT"
uv run --group ops python scripts/firebase_users.py --project "$PROJECT" create <owner email>
uv run --group ops python scripts/firebase_users.py --project "$PROJECT" create <viewer email>
```

The script passes `projectId` explicitly. To verify a user created in the console, run
`... --project "$PROJECT" verify <email>`. Every user must also be in the
`caos-role-map` secret; to add one, add a new secret version (from stdin, placeholders
only) and bump the pinned version in section 6's `--set-secrets`.

## 8. Smoke through the service origin

- `curl -s $ORIGIN/api/health`: status ready, every code OK.
- `curl -s -o /dev/null -w '%{http_code}' $ORIGIN/api/v1/book`: 401 NOT_AUTHENTICATED without a token.
- Sign in as the Reader: cannot start a run.
- Sign in as the owner: upload one small public filing (for example from
  `qualification/czr-*`), start a LITE run; the run stream stays open past 60 s to
  completion.
- Grant the Reader READER standing on the demo case (Admin section).
- On-mount blob check (spec §3a H4): upload one small public filing in the UI, open it,
  and confirm the document view renders. Then:

```bash
gcloud storage ls -r gs://$PROJECT-caos-blobs/ | head
```

  Expected: one object under a two-character shard directory, and no `tmp*` objects
  left behind. A `STORE_UNAVAILABLE` or `BLOB_*` refusal here triggers spec §3a H4's
  fallback, which gets its own plan.

## 9. Operations

An uptime check on `/api/health` matching the health document's top-level field
`"status":"ready"` (`caos/api/health.py`: `status` is `ready` or `not_ready`; the body is
compact JSON), an alerts-only budget, and Artifact Registry vulnerability scanning.

```bash
HOST=caos-$NUM.$REGION.run.app
gcloud monitoring uptime create "caos-health" --resource-type=uptime-url \
  --resource-labels=host=$HOST,project_id=$PROJECT --protocol=https --path=/api/health \
  --matcher-content='"status":"ready"' --matcher-type=contains-string \
  --period=5 --timeout=10 --validate-ssl=true --regions=usa-iowa,europe,asia-pacific
gcloud services enable containerscanning.googleapis.com
gcloud billing budgets create --billing-account=<BILLING_ACCOUNT> --display-name="caos monthly" \
  --budget-amount=<AMOUNT>GBP --calendar-period=month --filter-projects=projects/$PROJECT \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

Not found in the fetched docs: the new uptime check's ID in the command output (read it
with the console or a list command before building an alert on it), and the IAM role to
create the check. The budget is alerts-only: it notifies the billing account's
administrators and never stops spend.

Alerts. Create the email channel (the `beta` command is the only documented form; the
policies command is GA), then substitute its resource name into the policy files, which
carry `PROJECT_ID` and `CHANNEL_ID` placeholders. The owner ignores the restart alert
during their own deploys. Validate each filter in Metrics Explorer before creating its policy.

```bash
gcloud beta monitoring channels create --display-name="caos owner" --type=email \
  --channel-labels=email_address=<owner email>
gcloud monitoring channels list
CH=<channel id from the list>
for f in restart memory sql-refused; do
  sed -e "s/PROJECT_ID/$PROJECT/g" -e "s/CHANNEL_ID/$CH/g" ops/monitoring/$f.json > /tmp/$f.json
  gcloud monitoring policies create --policy-from-file=/tmp/$f.json
done
```

Not found in the fetched docs: that `cloudsql.googleapis.com/database/network/connection_refused_count`
and `cloudsql_database` / `database_id` are the right metric and labels for `sql-refused.json`
(the metric name is the brief's; confirm it in Metrics Explorer before creating the policy),
that a new email channel needs verification before it notifies, and the role to create a policy.

Lease-reclaim alert: not provided. `caos/graph/worker.py` emits no log line when a run's
lease is reclaimed (the word appears only in comments), so there is no stable text for a
log-based metric (`caos_lease_reclaims`) or `lease-reclaim.json`. Add both when the worker
logs a reclaim.

Secret access audit (DATA_READ for Secret Manager only). Read the policy, add only the
`auditConfigs` block below, keep `bindings:` and `etag:` unchanged, write it back. If
`set-iam-policy` reports a concurrent change, repeat all three steps. The logs are in the
`_Default` bucket, may cost, and are read with `roles/logging.privateLogViewer`.

If `auditConfigs:` already exists in the policy, append the new entry to that list; never
add a second `auditConfigs:` key, because gcloud's loader silently drops a duplicate key
and `set-iam-policy` would then remove the existing audit configs. The policy lists every
member's email, so it goes in a temp file that is removed afterwards, never in the repo.

```bash
POLICY=$(mktemp)
gcloud projects get-iam-policy $PROJECT > "$POLICY"
# add (or append to the existing auditConfigs list):
#   auditConfigs:
#   - auditLogConfigs:
#     - logType: DATA_READ
#     service: secretmanager.googleapis.com
gcloud projects set-iam-policy $PROJECT "$POLICY"
rm -f "$POLICY"
```

## 10. Restore drill (quarterly, and before a sync deploy that adds migrations)

```bash
gcloud sql backups list --instance=caos-pg
gcloud sql backups describe <backup id> --instance=caos-pg --format='value(startTime,endTime)'
gcloud sql instances create caos-pg-drill --database-version=POSTGRES_17 --edition=ENTERPRISE \
  --cpu=1 --memory=3840MiB --region=$REGION
gcloud sql backups restore <backup id> --restore-instance=caos-pg-drill --backup-instance=caos-pg
```

The drill needs no production password. Set a throwaway one on the drill instance only,
connect through the Auth Proxy (`cloud-sql-proxy --port 5432 $PROJECT:$REGION:caos-pg-drill`,
`psql` to 127.0.0.1 as user `caos`), then discard it:

```bash
DBPW=$(openssl rand -hex 24)
CLOUDSDK_CORE_DISABLE_FILE_LOGGING=true gcloud sql users set-password caos --instance=caos-pg-drill --password="$DBPW"
# connect with $DBPW, run the checks below, then:
unset DBPW
```

Pass criteria (none needs a production credential): schemas `caos_store` and `caos_graph`
exist; `select count(*) from caos_store.runs;` and `... caos_store.sources;` are above 0;
`select max(created_at) from caos_store.runs;` is no later than the backup's `endTime`
(`gcloud sql backups describe <backup id> --instance=caos-pg`) and no more than 24 h
before it (one daily backup interval, `--backup-start-time=02:00`), unless no run was
started in those 24 h. A later timestamp means the wrong backup was restored. The owner then deletes
`caos-pg-drill`; deleting instances is denied to agents. Record the date and results below.
The restore overwrites the target and restarts it; the drill target is scratch, never
`caos-pg`. A restore to an existing instance needs it to exist first (the reference
does not confirm that a same-tier instance is required).

A real restore needs a new `caos-database-url` secret version and a deploy; blobs are
content-addressed and need no restore.

| Date | Backup id | schemas present | runs / sources rows | newest runs.created_at vs backup end | Result |
| :--- | :--- | :--- | :--- | :--- | :--- |
| | | | | | |

## Local container smoke

`bash scripts/container_smoke.sh` builds the image and boots it against a throwaway
database on the local test Postgres (`docker compose up -d --wait`, port 55437),
with firebase mode and dummy credentials; it needs no real credential, but it does need
outbound internet access: the identity health probe fetches Google's signing certificates,
and an offline run ends "health never answered ready". The Docker VM disk is shared with
other containers, so before building the smoke measures the VM's free space and refuses
to build under `SMOKE_MIN_FREE_GB` (default 5): never fill the shared Docker disk.
