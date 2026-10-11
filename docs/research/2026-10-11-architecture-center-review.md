# Google Cloud Architecture Center review for CAOS (2026-10-11)

The owner asked for two things:
- how techniques such as GraphRAG on Spanner
  (`docs.cloud.google.com/architecture/gen-ai-graphrag-spanner`) bear on the planned
  query feature;
- a review of the Architecture Center as a whole, done the way the Firebase docs were
  reviewed.

This document builds on `2026-10-11-firebase-features-review.md` and does not repeat
it. The findings come from two read-only research passes over the Architecture Center
and the product docs it links. URL prefixes are shortened: GC =
`docs.cloud.google.com`, AF = `GC/architecture/framework`.

## 1. GraphRAG and the query feature

**What the reference architecture does** (GC/architecture/gen-ai-graphrag-spanner):
- **Ingestion:** files land in Cloud Storage, and Pub/Sub triggers a Cloud Run function.
  Gemini with LangChain's `LLMGraphTransformer` extracts entities and relationships
  into Spanner Graph. Text chunks are embedded and stored with their nodes.
- **Query path:** embed the question, run a vector search to find seed nodes, traverse
  the graph, re-rank, and have Gemini summarise.
- **The page's own caveats:**
  - Plain RAG suits data without complex inter-relationships.
  - LLM-extracted entities can be generic and suit niche domains poorly.
  - Spanner "often" needs minimum capacity even for small workloads.
  - The page has no citation section.

**Verdict for CAOS: not now.** The example question ("rank all technology issuers rated
B3 by leverage, and create a chart") needs no graph. It is filter, rank and aggregate
over typed facts, which is SQL's job. A graph earns its place only for multi-hop
questions: issuer → sponsor → subsidiaries → instruments → covenants → peers. Even
there, every edge CAOS stores would need a source-block anchor or be refused
(invariant 11). LLM-extracted edges are probabilistic, so each one would have to be
verified like any other claim.

| Question type | Lane |
|---|---|
| Rank or filter issuers by rating, sector, leverage, plus a chart | Allow-listed SQL tool on `issuer_facts` |
| Averages and counts by sector or rating | SQL aggregate tool |
| Peers of issuer X | SQL on sector, rating and metrics |
| Issuers sharing a sponsor; subsidiaries, instruments or guarantors under a parent | An `edges` table (parent, child, relation, source_block_id) with a recursive CTE |
| A covenant in a known pinned filing | Direct block lookup |
| "Where do the filings discuss customer concentration?" | Vector or full-text search returning **block ids**: pgvector 0.8.5 and `pg_textsearch` are both supported on Cloud SQL for PostgreSQL 17 (GC/sql/docs/postgres/extensions) |
| Recent news about X | The separate web lane (Gemini plus Search grounding) |

**Graph store options, if one is ever needed:**

| Option | Facts | Fit |
|---|---|---|
| Spanner Graph | Enterprise edition or higher (GC/spanner/docs/editions-overview). GoogleSQL only, no PostgreSQL interface (GC/spanner/docs/graph/overview). Minimum 100 processing units, with intermittent latency spikes below 1,000 (GC/spanner/docs/compute-capacity). From $0.041 per 100 PU per hour per replica; about $90 a month for a 3-replica regional instance, inferred (cloud.google.com/spanner). 90-day free trial with no SLA and no backups (GC/spanner/docs/free-trial-instance). | A second database. Only as a rebuildable projection of Postgres, never the source of truth. |
| Cloud SQL graph extension | Apache AGE is not on the supported list, and users cannot add extensions (GC/sql/docs/postgres/extensions). | Not available. |
| BigQuery Graph | GQL needs an Enterprise slot reservation; built for analytics with seconds-to-hours latency; launch stage unclear (GC/bigquery/docs/graph-overview). | Wrong shape for interactive questions. |
| Postgres recursive CTEs | No new service. | **The lazy option. Use it first.** |

**Patterns worth copying from the gen-AI reference architectures:**
- **Allow-listed, parameterized database tools** with parameters bound out of the
  model's sight and authorised invocations. This is the MCP Toolbox pattern
  (adk.dev/integrations/mcp-toolbox-for-databases). Copy the pattern, not the server.
- **Sequential, deterministic orchestration**, with model calls only where needed and
  loops with explicit exit conditions. Skip swarm and hierarchical agents
  (GC/architecture/choose-design-pattern-agentic-ai-system).
- **A separate evaluation subsystem:**
  - a golden dataset of questions with their expected rows;
  - continuous grounding metrics;
  - human review and adversarial tests.

  Sources: GC/architecture/rag-capable-gen-ai-app-using-vertex-ai,
  AF/perspectives/ai-ml/operational-excellence.
- **The same embedding model for ingestion and query**, if vector search is ever added.
- **Citations stay CAOS's own.** Search grounding returns web URIs with byte-span
  supports. The check-grounding API maps claims to caller-supplied facts. RAG Engine
  grounds on whole chunks with only their first and last page. None of them returns
  character or bounding-box coordinates. Retrieval returns block ids, and CAOS resolves
  and verifies the coordinates server-side.
- **Chart output:** the Conversational Analytics API returns Vega-Lite. CAOS keeps its
  own chart spec over its existing chart kinds (D122–D140), which needs no new
  dependency.

**Evolution path:**
1. `issuer_facts` (Decimal values and a source-block anchor per row) plus 4–6
   allow-listed tools. A reservation precedes every model call. A golden-question
   eval set exists from day one.
2. The web lane: separate, labelled ungoverned, behind the owner's ruling on
   invariant 1.
3. pgvector or `pg_textsearch` only when "find where" questions arrive. Retrieval
   returns block ids.
4. An `edges` table with recursive CTEs at the first sponsor, subsidiary or instrument
   question.
5. A real graph only on measured Postgres slowness, or on path queries beyond a few
   hops. Pilot it on the Spanner free trial as a projection of Postgres.

## 2. Architecture Center items for the platform

Defects X1–X4 and the "adopt now" list in the Firebase review stand. This review adds
the items below.

### Adopt now

| # | Item | Today → guidance | Source |
|---|---|---|---|
| A1 | **Restore drill and retained backups** | Backups and point-in-time recovery are planned but never restored. Restore into a scratch instance, write the steps down, add `--retain-backups-on-delete`. The default backup location is the nearest multi-region, so a cross-region restore needs no replica. A restore means a new `caos-database-url` secret version plus a deploy. Blobs are content-addressed and restore independently. | AF/reliability/perform-testing-for-recovery-from-data-loss, GC/sql/docs/postgres/backup-recovery/backups |
| A2 | **`service.yaml` as the source of truth** | One deploy command with about 25 flags and the `^@^` env hack. Export with `gcloud run services describe caos --format export`, commit it, and apply with `gcloud run services replace`. No traffic-split rollouts, because two revisions would mean two in-process workers. Roll back by redeploying the previous digest. | GC/run/docs/deployment-options-for-services, AF/operational-excellence |
| A3 | **Alerts for the single-instance failure mode** | Alert on `container/startup_latencies` outside a deploy: a crash or out-of-memory kill ends live runs. Also on memory utilisation, Cloud SQL `connection_refused_count`, and a log-based metric on lease reclaims. `startup_latencies` is BETA. | GC/run/docs/monitoring |
| A4 | **Build hardening** | Three changes. SLSA provenance: `options: {requestedVerifyOption: VERIFIED}` in `cloudbuild.yaml`. A dedicated `caos-build` service account (Artifact Registry writer, log writer), because new projects build as the Compute default account, which the runbook grants secrets and the database. Digest-pinned base images in the `Dockerfile` (`node:24-slim`, `python:3.13-slim`) and the builder, deploy by `@sha256`, and an Artifact Registry cleanup policy. | GC/build/docs/securing-builds/generate-validate-build-provenance, GC/build/docs/cloud-build-service-account-updates, GC/software-supply-chain-security/docs/safeguard-builds |
| A5 | **Secret Manager data-access audit logs** | DATA_READ is off by default. Turn it on for Secret Manager only; reads happen at start-up, so the volume is tiny. | GC/secret-manager/docs/audit-logging |
| A6 | **Owner account hardening** | One Google account owns production, the secrets and the database. Use passkey or security-key 2-Step Verification. No code. | AF/security |
| A7 | **Cloud SQL connection policy** | No authorized networks, `--ssl-mode=ENCRYPTED_ONLY` and `--connector-enforcement=REQUIRED`. Verify first that Cloud Run's built-in socket counts as a connector. Changing `ssl-mode` can restart the instance, so do it at creation. | GC/sql/docs/postgres/connect-run |

### Adopt later

- **Build from a git tag** with a Cloud Build trigger, so provenance names the source.
  The repo is public, so use push or tag triggers only, never PR triggers. Deploying
  stays manual, because a deploy restarts the only instance (GC/run/docs/continuous-deployment).
- **OpenTelemetry `gen_ai` traces to Cloud Trace.** Google's LangGraph guide instruments
  Vertex/GenAI, not `ChatOpenAI` on OpenRouter. The third-party instrumentors would be
  new dependencies needing FD records (AF/perspectives/ai-ml).
- **Committed use discounts** once usage has been stable for about three months: 28% or
  46% on Cloud Run, 25% or 52% on Cloud SQL (not shared-core). They are
  non-cancellable (GC/run/cud, GC/sql/cud).
- **Document AI OCR as an independent digit check in `scripts/qualify.py`**, not as
  the extractor (below).
- **Model Armor on the query box only**, not on evidence
  (GC/model-armor/sanitize-prompts-responses).

### Skip, with the reason

| Item | Reason |
|---|---|
| Document AI as the extractor | pdfminer gives per-glyph rectangles and the paint and visibility marks behind the hidden-text defence; Document AI gives neither. OCR would admit scans, which CAOS refuses by design (`SOURCE_HAS_NO_TEXT`), as text no byte check verifies. Layout Parser does not run in `europe-west2`, and its Gemini-based versions are release candidates (GC/document-ai/docs/handle-response, /layout-parse-chunk, /regions). |
| HA or a cross-region replica for Cloud SQL | A zonal instance suits an app that tolerates downtime. The restore drill (A1) is the proportionate measure (GC/architecture/deployment-archetypes). |
| Secure serverless blueprint: load balancer, Armor, IAP, VPC-SC, CMEK | IAP needs Google identities, and Firebase users aren't. VPC-SC needs an Organization and blocks Cloud Run continuous deployment. CMEK cannot be added to an existing instance, and destroying a key version destroys its backups. The documents are public (GC/architecture/blueprints/serverless-blueprint). |
| Private IP or Direct VPC egress | A VPC to run, for no gain over the encrypted socket (GC/sql/docs/postgres/connect-run). |
| Cloud Deploy, Terraform | One environment and a single owner. |
| FSI perspective | Bank-scale: two regions by three zones and DORA exit plans. Its AI governance (explainability, audit trail, human decision) is already exceeded by digest-bound gates and the ledger (AF/perspectives/fsi). |
| Gen AI evaluation service | Judges with Gemini outside the budget ledger. CAOS's `qualify.py` and `quality_compare.py` are the evaluation gate (AF/perspectives/ai-ml). |
| "Fall back to another model" | Conflicts with the pinned route and the bound model identity (invariant 10, FD8). |
| Multi-tenant SaaS, landing zones, foundations blueprint | Out of proportion for one owner and a handful of users. |

### Ranked by value

1. A1, the restore drill and retained backups.
2. A2, `service.yaml`.
3. A4, build hardening.
4. A3, the single-instance alerts.
5. A5 and A6, the secret-access audit and the owner's 2-Step Verification.

## Not verified

- Spanner Graph, BigQuery Graph and the data-agent APIs were not exercised at CAOS
  scale.
- The Spanner monthly figure is inferred from the commitment example.
- The Cloud SQL extension list was read through a fetch summariser.
- Alert-policy, Model Armor and Cloud Trace pricing.
- What source `gcloud builds submit` provenance records.
- The logging option a user-specified build service account needs.
- Whether Cloud Run's built-in socket satisfies connector enforcement.
- Whether the project sits under an Organization.
- Layout Parser's launch stage and how good its boxes are on financial tables.
- Nothing in this review was run.
