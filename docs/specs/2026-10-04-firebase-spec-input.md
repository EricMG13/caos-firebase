\# Deploying and Optimizing CAOS v2 on Firebase & Google Cloud

\-\--

\## Part 1: Feasibility & Architecture of CAOS on Firebase

Yes, you \*\*can\*\* build and host this application on Firebase, but
\*\*not as a pure, traditional Firebase BaaS app\*\* (like an SPA
talking directly to Firestore/NoSQL).

Your codebase (\`caos-v2\`) has several architectural invariants:

1\. \*\*A relational database schema\*\* (\`server/store/schema.sql\`
uses SQL transactions, unique key constraints, run IDs, and gate
states)\[span_0\](start_span)\[span_0\](end_span).

2\. \*\*Deterministic Python runtime & heavy computational libraries\*\*
(\`openpyxl\`, \`pypdf\`, complex financial calculators, schema
validators)\[span_1\](start_span)\[span_1\](end_span).

3\. \*\*Server-Sent Events (SSE) streaming\*\*
(\`server/api/stream.py\`) for live agent run step
outputs\[span_2\](start_span)\[span_2\](end_span).

4\. \*\*Bespoke React SPA\*\* (\`frontend/\`) with rich UI
views\[span_3\](start_span)\[span_3\](end_span).

Because Firebase is part of Google Cloud, you can map the CAOS stack to
Firebase services cleanly using a \*\*hybrid Firebase + Google Cloud
Run\*\* pattern.

\### Component Mapping

\| CAOS Component \| Native Project Code \| Firebase / GCP Equivalent \|
Feasibility & Fit \|

\| :\-\-- \| :\-\-- \| :\-\-- \| :\-\-- \|

\| \*\*Frontend\*\* \| React / Vite SPA
(\`frontend/\`)\[span_4\](start_span)\[span_4\](end_span) \|
\*\*Firebase Hosting\*\* \| \*\*Native.\*\* Build with \`npm run
build\`\[span_5\](start_span)\[span_5\](end_span) and deploy static
assets to Firebase Hosting\'s global CDN. \|

\| \*\*Backend & Skills\*\* \| Python server (\`server/\`),
\`vendor/deploy-v/\` skills\[span_6\](start_span)\[span_6\](end_span) \|
\*\*Google Cloud Run\*\* via Firebase Rewrites \| \*\*Native.\*\*
Dockerize the FastAPI/Python server (\`Dockerfile\` already exists in
your repo)\[span_7\](start_span)\[span_7\](end_span) and route API
requests via \`firebase.json\` rewrites. \|

\| \*\*Agent Streaming\*\* \| SSE streaming
(\`server/api/stream.py\`)\[span_8\](start_span)\[span_8\](end_span) \|
\*\*Cloud Run (HTTP/2 / SSE enabled)\*\* \| \*\*Excellent.\*\* Cloud Run
natively supports request streaming and long-lived HTTP connections. \|

\| \*\*Database\*\* \| SQL schema
(\`server/store/schema.sql\`)\[span_9\](start_span)\[span_9\](end_span)
\| \*\*Cloud SQL (PostgreSQL)\*\* or \*\*Firebase Data Connect\*\* \|
\*\*Crucial.\*\* Do \*\*not\*\* rewrite this into Firestore NoSQL. Use
Cloud SQL (Postgres) or Firebase Data Connect (PostgreSQL). \|

\| \*\*Evidence & Blobs\*\* \| PDF parsing & document stores
(\`server/blobs.py\`)\[span_10\](start_span)\[span_10\](end_span) \|
\*\*Cloud Storage for Firebase\*\* \| \*\*Native.\*\* Perfect for
storing source debt agreements, 10-Ks, and exported Excel models/credit
memos\[span_11\](start_span)\[span_11\](end_span). \|

\| \*\*Authentication\*\* \| Custom role tokens
(\`server/api/identity.py\`)\[span_12\](start_span)\[span_12\](end_span)
\| \*\*Firebase Auth\*\* + Custom Claims \| \*\*Upgrade.\*\* Maps
cleanly to CAOS roles (\`analyst\`, \`approver\`, \`committee_chair\`)
via Firebase JWT claims. \|

\### Recommended Architecture

Instead of trying to convert the Python code into lightweight Node Cloud
Functions or forcing relational credit records into NoSQL Firestore, use
\*\*Firebase as the unified delivery and authentication edge\*\* backed
by Cloud Run:

\`\`\`text

\[ Underwriter Browser / React Client \]

│

▼

\[ Firebase Hosting \] (Global CDN)

├── /\* ──\> Serves compiled React SPA assets

└── /api/\*\* ──\> Rewrite rule forwards to Cloud Run

│

▼

\[ Google Cloud Run (Docker) \]

├── Python FastAPI App (caos-v2 server)

├── Vendor Agent Skills (CP-0 to CP-6)

└── SSE Streaming Endpoint

│

┌───────────────┴──────────────┐

▼ ▼

\[ Cloud Storage for Firebase \] \[ Cloud SQL (PostgreSQL) \]

(PDFs, Excel models, artifacts) (Audit logs, Runs, Ledger gates)
