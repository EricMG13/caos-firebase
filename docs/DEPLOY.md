# Deploying CAOS to Cloud Run

One service, one origin: Cloud Run serves the workspace, the API and the run stream.
Nothing in this file is a secret. The two secret values are created by the owner in
their own terminal and are never echoed or written to a file.

Prerequisites (owner): a Firebase project on the Blaze plan; `gcloud auth login`;
`firebase login`; Authentication, Sign-in method, Email/Password enabled.

## 1. Authentication settings (console)

- Authentication, Settings, User actions: uncheck "Enable create (sign-up)" so only
  owner-created users exist.
- Do NOT enable reCAPTCHA or email-enumeration protection: they load third-party
  scripts the app's CSP blocks.
- Add the users (owner ANALYST, viewers READER) under Authentication, Users.

## 2. Project, APIs, registry, database, bucket

```bash
PROJECT=<firebase project id>; REGION=europe-west2; gcloud config set project $PROJECT
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com identitytoolkit.googleapis.com
gcloud artifacts repositories create caos --repository-format=docker --location=$REGION
gcloud sql instances create caos-pg --database-version=POSTGRES_17 --edition=ENTERPRISE \
  --tier=db-f1-micro --region=$REGION
gcloud sql databases create caos --instance=caos-pg
gcloud storage buckets create gs://$PROJECT-caos-blobs --location=$REGION \
  --uniform-bucket-level-access --public-access-prevention
```

Proves: each command exits 0; `gcloud sql instances list` shows `caos-pg` RUNNABLE.

## 3. Secrets (owner, own terminal)

```bash
DBPW=$(openssl rand -hex 24); gcloud sql users create caos --instance=caos-pg --password="$DBPW"
printf 'postgresql://caos:%s@/caos?host=/cloudsql/%s:%s:caos-pg' "$DBPW" $PROJECT $REGION \
  | gcloud secrets create caos-database-url --data-file=-; unset DBPW
read -rs KEY && printf %s "$KEY" | gcloud secrets create openrouter-key --data-file=-; unset KEY   # paste the key, Enter; nothing is echoed
```

Proves: `gcloud secrets list` shows both names (it never shows a value).

## 4. Service account permissions

```bash
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/cloudsql.client
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
gcloud storage buckets add-iam-policy-binding gs://$PROJECT-caos-blobs \
  --member=serviceAccount:$SA --role=roles/storage.objectUser
```

## 5. Build

The web `apiKey` comes from `firebase apps:create web caos --project "$PROJECT"` then
`firebase apps:sdkconfig web --project "$PROJECT"`.

```bash
IMAGE=$REGION-docker.pkg.dev/$PROJECT/caos/caos:$(git rev-parse --short HEAD)
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_IMAGE=$IMAGE,_FIREBASE_API_KEY=<web apiKey>,_FIREBASE_AUTH_DOMAIN=$PROJECT.firebaseapp.com
```

Proves: the image builds from the tracked tree and is in Artifact Registry.

## 6. Deploy

Set `CAOS_PUBLIC_ORIGIN` on this first deploy from the deterministic service URL
`https://caos-<project-number>.<region>.run.app`. Firebase mode refuses to boot
without it, so there is no placeholder second deploy. Requests through the legacy
`*.a.run.app` URL get 403 ORIGIN_REFUSED on writes; always use the URL below.
`CAOS_ROLE_MAP` is `email=ROLE;email=ROLE`; an empty entry (a trailing `;`) is
skipped, anything else malformed refuses the boot. The `^@^` prefix
makes `@` the env separator so the role map's `;` survives. `--allow-unauthenticated`
lets the browser reach the service; sign-in is the app's own. `CAOS_RUN_CEILING=120.00`
is set explicitly, as at the fork point (FD11): it bounds each run's reservation, and the
OpenRouter key's own limit is the hard spend stop.

```bash
NUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
ORIGIN=https://caos-$NUM.$REGION.run.app
gcloud run deploy caos --image $IMAGE --region $REGION --port 8000 --no-cpu-throttling \
  --min-instances 1 --max-instances 1 --timeout 3600 --memory 2Gi --cpu 1 \
  --execution-environment gen2 --allow-unauthenticated \
  --add-cloudsql-instances $PROJECT:$REGION:caos-pg \
  --add-volume name=blobs,type=cloud-storage,bucket=$PROJECT-caos-blobs \
  --add-volume-mount volume=blobs,mount-path=/data/blobs \
  --set-secrets OPENROUTER_API_KEY=openrouter-key:latest,CAOS_DATABASE_URL=caos-database-url:latest \
  --set-env-vars "^@^CAOS_BLOB_ROOT=/data/blobs@CAOS_FIREBASE_PROJECT=$PROJECT@CAOS_ROLE_MAP=<owner email>=ANALYST;<viewer email>=READER@CAOS_MODEL_ENDPOINT=openai/gpt-6-luna@CAOS_MODEL_PRICE=openai/gpt-6-luna,0.0000002,0.000001,2026-10-04@CAOS_REASONING_EFFORT=high@CAOS_PROVIDER_ORDER=openai@CAOS_RUN_CEILING=120.00@CAOS_PUBLIC_ORIGIN=$ORIGIN"
URL=$(gcloud run services describe caos --region $REGION --format='value(status.url)')
echo "$URL"; echo "$ORIGIN"
```

Both URLs serve. Use `$ORIGIN` in the browser: writes through the legacy
`*.a.run.app` URL get 403 ORIGIN_REFUSED, and `CAOS_PUBLIC_ORIGIN` stays `$ORIGIN`.

Then add the service host (`$ORIGIN` without `https://`, i.e.
`caos-$NUM.$REGION.run.app`) under Firebase console, Authentication, Settings,
Authorised domains, or sign-in from the service is refused.

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

## 7. Verify the users' email

Users created in the Firebase console start with `emailVerified=false`, and the server
refuses them. The owner sets it for each owner-created user (the call carries no
secret; the token comes from the owner's own gcloud login):

```bash
curl -sS -X POST "https://identitytoolkit.googleapis.com/v1/projects/$PROJECT/accounts:update" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "x-goog-user-project: $PROJECT" -H "Content-Type: application/json" \
  -d '{"localId": "<uid>", "emailVerified": true}'
```

The uid is on the Authentication, Users page. Repeat per user.

## 8. Smoke through the service origin

- `curl -s $ORIGIN/api/health`: status ready, every code OK.
- `curl -s -o /dev/null -w '%{http_code}' $ORIGIN/api/v1/book`: 401 NOT_AUTHENTICATED without a token.
- Sign in as the Reader: cannot start a run.
- Sign in as the owner: upload one small public filing (for example from
  `qualification/czr-*`), start a LITE run; the run stream stays open past 60 s to
  completion.
- Grant the Reader READER standing on the demo case (Admin section).

## Local container smoke

`bash scripts/container_smoke.sh` builds the image and boots it against a throwaway
database on the local test Postgres (`docker compose up -d --wait`, port 55437),
with firebase mode and dummy credentials; it needs no real credential, but it does need
outbound internet access: the identity health probe fetches Google's signing certificates,
and an offline run ends "health never answered ready". The Docker VM disk is shared with
other containers, so before building the smoke measures the VM's free space and refuses
to build under `SMOKE_MIN_FREE_GB` (default 5): never fill the shared Docker disk.
