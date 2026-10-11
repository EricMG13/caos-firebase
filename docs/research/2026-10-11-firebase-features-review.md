# Firebase and Google Cloud features for CAOS (2026-10-11)

The owner asked which Firebase features are better than the fork's custom code, which
could be adapted to improve performance, and what the planned natural-language query
feature should be built on. An example question for that feature: "rank all technology
issuers rated B3 by leverage, and create a chart".

Method: the official docs, searched through the Firebase MCP (`developerknowledge_*`).
Three read-only research passes gathered the findings. The claims that change a decision
were re-checked directly in this session and are marked ✓.

The docs index now uses newer product names: Data Connect is "Firebase SQL Connect" and
Vertex AI is "Gemini Enterprise Agent Platform". URL prefixes are shortened as
FB = `firebase.google.com/docs`, GC = `docs.cloud.google.com`.

## 1. Defects found in the current setup

| # | Finding | Evidence | Fix |
|---|---|---|---|
| X1 ✓ | Cloud Run sends SIGKILL 10 s after SIGTERM. `caos/serve.py` (unchanged from upstream) budgets `GRACEFUL_SECONDS=2` + `LIMIT_JOIN_SECONDS=12` = 14 s. Shutdown work after the join can be cut off. Recovery is the same lease reclaim as today (a run stalls up to 900 s, its reservation held), so this is low severity. | GC/run/docs/container-contract (Shutdown) | Keep drain plus join under 10 s on Cloud Run, test first. Deploy while no run is in flight, because a deploy restarts the only instance. |
| X2 ✓ | `docs/DEPLOY.md` creates Cloud SQL on `db-f1-micro`. Shared-core tiers are outside the SLA, and IAM logins on them can time out. | GC/sql/faq, GC/sql/docs/postgres/operational-guidelines, /iam-authentication | Enterprise edition on a dedicated core, for example `db-custom-1-3840`. |
| X3 ✓ | Secrets are mounted as `:latest`. Google recommends pinned versions; env-var secrets resolve once at startup. | GC/run/docs/configuring/services/secrets | Pin the versions. |
| X4 | The runbook binds roles to the default compute service account, which may hold Editor. | GC/run/docs/securing/service-identity | Use a dedicated `caos-run` service account with secret-level and bucket-level grants. |

## 2. Adopt now (flags and runbook, no app code)

- **Cloud Run:**
  - `--cpu-boost` (GC/run/docs/configuring/services/cpu): CPU is doubled during startup, so restarts are shorter.
  - An explicit `--concurrency` sized to `STREAM_LIMIT=24` plus API headroom (GC/run/docs/about-concurrency).
  - An HTTP startup probe on `/api/health` (GC/run/docs/configuring/healthchecks).
  - No liveness probe: a failed liveness probe kills live runs.
- **Cloud SQL:**
  - Automated backups, point-in-time recovery and `--deletion-protection`; confirm them with `describe` after creation (GC/sql/docs/postgres/backup-recovery/manage-standard-backups, /configure-pitr).
  - Query Insights, free on the Enterprise edition (GC/sql/docs/postgres/using-query-insights).
  - Keep `--edition=ENTERPRISE`, because PostgreSQL 16 and later default to Enterprise Plus.
- **Observability:**
  - Uptime check on `/api/health` with a content match (GC/monitoring/uptime-checks).
  - Error Reporting picks up stack traces from stdout and stderr automatically (GC/run/docs/error-reporting).
- **Cost:** an alerts-only budget (GC/billing/docs/how-to/budgets-programmatic-notifications). Billing data is estimated and delayed, so the OpenRouter key limit stays the hard stop.
- **Supply chain:** Artifact Analysis scans images on push to Artifact Registry (GC/artifact-analysis/docs/artifact-analysis).
- **Users:** replace the Identity Toolkit REST call in DEPLOY.md §7 with a local ops script using the Admin SDK: `auth.create_user(email=…, password=…, email_verified=True)` and `auth.update_user(uid, email_verified=True)`. Docs: FB/auth/admin/manage-users. `firebase-admin` is a new dependency, so it needs an FD record. Keep the script out of the runtime image.

## 3. Adopt as their own reviewed change

- **Server-minted session cookies (Admin SDK `create_session_cookie` / `verify_session_cookie`).**
  - Docs: FB/auth/admin/manage-cookies, /manage-sessions.
  - This would replace about 250 of the 320 lines in `firebase_auth.py`, the JavaScript-written `__session` cookie and the 120 s refresh timer in `AuthGate.tsx`.
  - Gain: the cookie becomes HttpOnly. With client persistence set to `NONE`, no long-lived credential stays readable by JavaScript.
  - Costs:
    - A new dependency, which is synchronous and must run off the event loop.
    - The undocumented resilience goes: stale-if-error, single-flight, body cap.
    - A cookie stays valid until it expires unless `check_revoked=True` is used, which costs a round trip per call.
    - Roles are frozen at mint time. That doesn't affect CAOS, because roles come from `CAOS_ROLE_MAP`.
    - The sign-in flow is rewritten.
  - CSRF is already handled by the edge's Origin and `Sec-Fetch-Site` refusal.
  - The SDK does not check `email_verified`, so CAOS keeps that check.
  - Verdict: worth it for the HttpOnly gain, as a security change with its own `mx-adversary` review, after the re-fork.
- **Write blobs once to their final path; no temp-then-rename on FUSE.**
  - Docs: GC/storage/docs/cloud-storage-fuse/overview, GC/storage/docs/consistency.
  - The docs don't state whether a single-file rename on FUSE is atomic, and a directory rename isn't atomic on a flat bucket. An object becomes visible only when its upload completes, and every read re-hashes, so a write straight to the final path fails closed.
  - If tests show a problem, switch to the Python client with `if_generation_match=0` (create-only, strongly consistent); that needs an FD record.
  - Turn on soft delete; the default is 7 days.
  - Object Versioning and retention locks don't suit a FUSE-mounted bucket: the mount can't write to a bucket with a retention policy, and versioning isn't supported.
- **JSON logs** with `severity` and `logging.googleapis.com/trace` from `X-Cloud-Trace-Context` (GC/run/docs/logging). No log configuration was found in `caos/`.
- **TOTP second factor (Identity Platform)** (FB/auth/web/totp-mfa, GC/identity-platform/quotas).
  - Free up to 50,000 monthly active users on Blaze.
  - Needs a resolver step in `AuthGate`.
  - Enabling it is documented only through the Node Admin SDK or REST.
  - Optional.
- **IAM database authentication** (GC/sql/docs/postgres/iam-authentication). It drops the database password. It needs a dedicated-core tier and a connector or the Auth Proxy, but the Python connector supports only pg8000 and asyncpg, not psycopg. So later, if ever.

## 4. Skip, with the reason

| Feature | Reason |
|---|---|
| Firebase Hosting in front ✓ | 60 s request timeout, including rewrites to Cloud Run (FB/hosting/cloud-run), which would cut the run stream. A second origin breaks the cookie. |
| App Hosting ✓ | Node.js only: Next.js, Angular and output-bundle adapters on `nodejs22`/`nodejs24` (FB/app-hosting/frameworks-tooling). |
| App Check (reCAPTCHA Enterprise) | Needs a third-party script, an iframe and a `_GRECAPTCHA` cookie, which breaks the strict CSP. `EventSource` can't send its header. |
| Firebase SQL Connect (Data Connect) | Duplicates the psycopg layer. Brownfield use needs a hand-written GraphQL schema. Gemini helps generate queries only in the console, CLI and VS Code; there's no runtime NL→query API. |
| Firebase AI Logic | Client-side Gemini calls bypass the server-side reservation (invariant 8). No server SDK. |
| Genkit (Python) | Preview, 0.x, with breaking minor releases (genkit.dev/docs/python/api-stability). It would be a second orchestration layer beside LangGraph. Revisit at 1.0. |
| Remote Config (server) | Preview in Python, and nothing needs runtime flags today. |
| Cloud Tasks | 30-minute HTTP deadline, shorter than a run. |
| Cloud Run jobs / worker pools | Splitting the worker is not needed at one instance. Revisit if runs need their own scaling. |
| Managed connection pooling | Enterprise Plus only, and transaction mode loses session features. |
| Cloud Armor, Binary Authorization | Armor needs a load balancer and disabling `run.app`. Binary Authorization adds nothing for a single owner deploying from their terminal. |
| Firebase Performance Monitoring (web) | Beta, no SLA, adds `connect-src` hosts. |
| Automation that disables billing | Can delete resources, with no guarantee of staying under budget. |

## 5. The query feature

### Prerequisite: an issuer-facts read model

No store table has a rating, sector or leverage column. Those values live inside accepted
module outputs: the registers each output profile declares (D124). Every option below
first needs a typed table or view, `issuer_facts`, with these columns:

- issuer
- sector
- rating with its agency and scale
- metric name and definition
- value as `numeric`
- as-of date
- run id
- artifact digest
- citation coordinates

It is written when a module output is accepted, so every answer row can cite its source
(invariant 11) and values stay Decimal (invariant 7). **Owner question:** where should a
rating like "B3" come from? It could be the agency rating stated in a filing, or the
methodology's own output. That choice decides which register feeds the table.

### Recommended shape (built only on what the docs support)

1. **Database lane.**
   - The model is called through the existing `caos.models` factory, with a reservation.
   - It picks from a small allow-list of typed query tools: filter, rank, aggregate and compare over `issuer_facts`.
   - The tools run through psycopg on a read-only role with a statement timeout.
   - The model returns a Pydantic chart spec naming one of the existing chart kinds (`BarChart`, `ScatterChart`, `DumbbellChart`, …), which the SPA renders.
   - The model never writes SQL; ranking and arithmetic happen in SQL.
   - The example becomes `rank(sector="Technology", rating="B3", metric="leverage", order="desc")` → rows with citations → a bar spec.
   - This is the pattern MCP Toolbox for Databases recommends (fixed, parameterized tools with authenticated parameters, GC/sql/docs/postgres/pre-built-tools-with-mcp-toolbox), without adding the Toolbox itself.
2. **Web lane, optional and separate: Gemini with Grounding with Google Search.**
   - It returns `groundingMetadata`: the search queries, source chunks, inline supports and the Search Suggestions markup.
   - Pricing on Gemini 3 and later is $14 per 1,000 search queries, after 5,000 free a month (ai.google.dev/gemini-api/docs/generate-content/google-search).
   - The Search Suggestions must be shown unmodified next to the answer, with sources shown inline and in aggregate.
   - Only Gemini can ground, so this lane needs its own Gemini call path and its own reservation wrapper; OpenRouter models can't do it.
   - Combining search with function calling is Preview on one docs page and unsupported on another. So the lane runs as its own call, and database facts are passed in as text.
   - **It conflicts with invariant 1 ("no web discovery")** unless the owner rules that the invariant covers governed runs only. Under that ruling, web answers are labelled ungoverned and never feed a governed run.
3. **Revisit later:**
   - The Conversational Analytics API `QueryData`: NL→SQL on Cloud SQL for PostgreSQL, currently Preview, with no pricing found. Google makes its model call, so the reservation couldn't meter it.
   - Genkit for Python, at 1.0.

## Not verified

- Grounding with Google Search and SQL Connect carry no GA label in the pages found.
- `QueryData` pricing.
- The minimal IAM roles for session cookies and Remote Config.
- Whether a single-file rename on FUSE is atomic.
- Whether reCAPTCHA is compatible with Trusted Types.
- Whether Firebase Auth can switch off open sign-up without Identity Platform. Today anyone holding the public API key can create an account; `CAOS_ROLE_MAP` still refuses that account with 403.
- Nothing in this review was run or tested.
