# CAOS on Firebase — design (2026-10-04)

Status: approved in conversation 2026-10-04, pending the owner's review of this file.
Input: `2026-10-04-firebase-spec-input.md` (the owner's Firebase document, converted
as given). This design keeps that document's shape and corrects it where it would
break CAOS (section 2).

## 1. Goal and scope

A non-enterprise CAOS online and working for a presentation on 2026-10-05: sign in,
upload public filings, start a governed run, watch it stream, read the result. Built
by forking `caos-databricks` and unplugging the Databricks platform (Phase 1). Removing
the dead Databricks code is Phase 2, after the demo.

Out of scope for Phase 1: Firebase Hosting, Google sign-in (section 4.3), Gemini, a
native Cloud Storage client, custom domains, more than one instance.

## 2. Review of the input document

| # | Input says | Problem | Resolution |
|---|---|---|---|
| R1 | Paths `server/`, `requirements.txt`, an existing Dockerfile, roles analyst / approver / committee_chair, `pypdf` | Describes `caos-v2`, the legacy snapshot (`b5a3932f`) that `caos-databricks` replaced. The fork is `caos-databricks`: `caos/`, uv, no Dockerfile, global roles READER / ANALYST / ADMIN plus per-case standing READER / WRITER / APPROVER / ADMIN, pdfminer. | Mapping kept, paths swapped. |
| R2 | Firebase Hosting serves the SPA and rewrites `/api/**` to Cloud Run; SSE rated "Excellent" | Hosting cuts every request at 60 s (HTTP 504), so the run stream would drop every minute. | Cloud Run serves SPA, API and stream from one origin (the app already serves `frontend/dist`). Hosting deferred. |
| R3 | — | Not covered: the LangGraph worker runs inside the API process. Cloud Run throttles CPU outside requests and scales to zero, so runs would stall and park. | CPU always allocated, min = max = 1 instance. |
| R4 | — | Not covered: where model calls go. The Databricks serving endpoints are gone. | OpenRouter becomes the production provider (section 4.4). |
| R5 | Firebase Auth + custom claims | Workable, but the run stream uses `EventSource`, which cannot send an `Authorization` header; custom claims need an admin tool to set. | Token in a cookie for the stream only; roles from a server-side email map (section 4.3). |
| R6 | Cloud Storage for Firebase | Needs a new storage client unless mounted. | Cloud Run mounts the bucket at `/data/blobs`; the existing filesystem backend works unchanged. |
| R7 | Cloud SQL or Firebase Data Connect | Data Connect is Cloud SQL behind GraphQL, which the app does not use. | Cloud SQL for PostgreSQL 17 over the Cloud Run Unix socket. |
| R8 | `[span_n](start_span)` markers | Citation residue from the tool that wrote the input. | Ignored. |

## 3. Architecture

```text
Browser (React workspace + Firebase Auth JS, email/password)
   │  https://<service>.<region>.run.app   (one origin: SPA, /api, run stream)
   ▼
Cloud Run service "caos" (europe-west2, 1 instance, CPU always on, timeout 3600 s)
   python -m caos.serve  (API + in-process worker, 0.0.0.0:8000)
   ├── Cloud SQL Postgres 17   via /cloudsql/<project>:<region>:<instance>
   ├── GCS bucket              mounted at /data/blobs   (CAOS_BLOB_ROOT)
   ├── Secret Manager          OPENROUTER_API_KEY, CAOS_DATABASE_URL
   └── OpenRouter              every model call, via caos.models
Firebase project (same GCP project): Authentication only (email/password users)
```

## 4. Components

### 4.1 Container

`Dockerfile`: multi-stage. Stage 1 builds `frontend/dist` with Node (version from
`.node-version`), passing the public Firebase web config as `VITE_FIREBASE_*` build
args. Stage 2 is Python 3.13 slim with uv, `uv sync --locked --no-dev`, the app, the
built frontend and `vendor/`. Runs `python -m caos.serve` as a non-root user. Container
port 8000 (`--port 8000` on deploy), so `serve.py` needs no change.

### 4.2 Store and blobs

- `CAOS_DATABASE_URL=postgresql://caos:<pw>@/caos?host=/cloudsql/<conn>` from Secret
  Manager. The app creates `caos_store` and `caos_graph` on first boot; the `caos`
  role owns the database.
- `CAOS_BLOB_ROOT=/data/blobs` on a Cloud Storage volume mount (gen2 execution
  environment). Writes go to a temporary name and are renamed into place; on the FUSE
  mount a rename is copy-then-delete, so the final name holds the whole object or
  nothing, and every read re-hashes.

### 4.3 Identity (security-critical)

A third edge mode, `firebase`, selected by `CAOS_FIREBASE_PROJECT=<project id>`. Boot
refuses `EDGE_CONFIG_INVALID` if it is set together with `DATABRICKS_APP_NAME`, or
without `CAOS_PUBLIC_ORIGIN` and `CAOS_ROLE_MAP`.

- **Token source.** `Authorization: Bearer <ID token>` when present, else the
  `__session` cookie (`SameSite=Strict`), on every method, so `EventSource` and every
  existing `fetch` work unchanged. Cross-site unsafe requests are already refused by
  the Origin and `Sec-Fetch-Site` checks, which stay as they are, with
  `CAOS_PUBLIC_ORIGIN` required in this mode.
- **Verification** (`pyjwt` + `cryptography`, both already locked): RS256 only; `kid`
  among Google's certificates at
  `https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com`,
  cached for the response's `max-age`, fetched with a bounded timeout; `aud` = the
  project id; `iss` = `https://securetoken.google.com/<project id>`; `exp` in the
  future, `iat` and `auth_time` in the past (60 s leeway); `sub` 1–128 characters;
  `email_verified` true. Any failure is `401 NOT_AUTHENTICATED`, typed, with nothing
  logged but the code.
- **Actor.** `user_id = uuid5(<fixed namespace>, "firebase:" + sub)`. Global role from
  `CAOS_ROLE_MAP` (`email=ROLE;email=ROLE`, compared lower-case); an email not in the
  map is `403 NOT_AUTHORISED`. The identity headers (`x-caos-user`, `x-caos-role`,
  `x-forwarded-groups`) are dropped before identity, as platform mode does; this mode
  reads only `authorization` and `cookie`, so no other forwarded header decides anyone.
- **Sign-in method: email and password**, not Google sign-in. The app's policy sets
  `require-trusted-types-for 'script'; trusted-types 'none'` and
  `cross-origin-opener-policy: same-origin`; Google's popup and redirect flows load
  `apis.google.com` scripts and need `same-origin-allow-popups`, which would weaken
  both. Email/password is plain HTTPS to Google, so only `connect-src` grows
  (`identitytoolkit.googleapis.com`, `securetoken.googleapis.com`). The owner creates
  the accounts in the Firebase console: one ANALYST (the owner), READER logins for
  viewers. Google sign-in is a later change with its own policy review.
- **Frontend.** `firebase` (modular `app` + `auth` only; a new dependency, FD7) with
  `initializeAuth(app, {persistence: indexedDBLocalPersistence})` and no popup
  resolver, so no third-party script or frame loads. A sign-in screen gates the
  workspace; `onIdTokenChanged` refreshes the bearer token and the `__session` cookie
  (`Secure; SameSite=Strict; Path=/; Max-Age=3600`). The sidebar's Sign out
  becomes live (sign out, clear the cookie).
- **Accepted limits.** No revocation check (ID tokens live one hour); one cookie name
  kept for Hosting compatibility later.

### 4.4 Model

`caos.models.chat_model` builds `ChatOpenAI` against `https://openrouter.ai/api/v1`
(the logic of `tests/openrouter_adapter.py` at the fork point, including F479's
reasoning effort and provider pin), key `OPENROUTER_API_KEY` read at call time. Model
`openai/gpt-6-luna`, effort high, pinned to the `openai` tier (owner, 2026-10-03).
`CAOS_MODEL_ENDPOINT`, `CAOS_MODEL_PRICE` and `CAOS_RUN_CEILING` as at the fork point.
The key is the existing `caos-qualification` key (owner's choice): its $50 limit is
the hard spend stop, because the price pin understates OpenRouter's bill 1.6–1.9×.

### 4.5 Deploy

`docs/DEPLOY.md` runbook, one command per step: enable APIs (Run, SQL Admin, Secret
Manager, Cloud Build, Artifact Registry, Identity Toolkit); create the Cloud SQL
instance, database and role; the bucket; the two secrets; then
`gcloud run deploy caos --source . --region europe-west2 --port 8000
--no-cpu-throttling --min-instances 1 --max-instances 1 --timeout 3600
--execution-environment gen2 --add-cloudsql-instances <conn>
--add-volume name=blobs,type=cloud-storage,bucket=<bucket>
--add-volume-mount volume=blobs,mount-path=/data/blobs
--set-secrets OPENROUTER_API_KEY=openrouter-key:latest,CAOS_DATABASE_URL=caos-database-url:latest
--set-env-vars CAOS_BIND_HOST=0.0.0.0,CAOS_WORKER_IN_PROCESS=1,CAOS_SITE_ROOT=frontend/dist,CAOS_BLOB_ROOT=/data/blobs,CAOS_FIREBASE_PROJECT=<project id>,CAOS_ROLE_MAP=<map>,CAOS_MODEL_ENDPOINT=...,CAOS_MODEL_PRICE=...,CAOS_RUN_CEILING=...`
(the three model values exactly as at the fork point).
`CAOS_PUBLIC_ORIGIN` is the service URL, set on a second deploy once known. The
service URL is added to Firebase Auth's authorised domains.

## 5. Verification

1. Unit tests for the firebase edge mode with a locally generated RSA key and a stubbed
   certificate fetch: valid token accepted; wrong `aud`, `iss`, `alg`, expired,
   unverified email, unknown `kid`, unmapped email, forged `x-caos-*` header, cookie on
   an unsafe request — each refused with its code.
2. The retained gate suite passes on the fork.
3. Container smoke: `docker build`, boot against local Postgres, `/api/health` OK.
4. Deployed: health OK; no token → 401; READER cannot start a run; ANALYST uploads a
   small public filing and a LITE run completes on OpenRouter; the stream stays open
   past 60 s. Then one case is run in advance for the presentation.

## 6. Owner actions

Create the Firebase project and link billing (Blaze); `firebase login`;
`gcloud auth login`; create the email/password accounts; paste the OpenRouter key into
Secret Manager. Name the fork point. Check the methodology bundle's ownership before
the code is pushed to the public repo (no LICENSE exists in `caos-databricks` or
`vendor/deploy-v`).

## 7. Risks

- Deadline: identity (4.3) is the largest and riskiest change.
- Cost: an always-on instance plus Cloud SQL runs every hour (estimate tens of dollars
  a month; not checked against current prices).
- Live runs can fail on model output; one case is completed in advance.
- Public repo: the methodology bundle and the demo fixtures (illustrative figures about
  a real issuer) become public with the fork.
