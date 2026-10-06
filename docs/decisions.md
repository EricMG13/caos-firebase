# Decisions

Later entries override earlier ones. Details live in the design spec named in each entry.

- D1 (2026-10-04) — Fork `caos-databricks` into this repository as one fresh-history
  commit at a point the owner names; public GitHub repository. Phase 1 unplugs the
  Databricks platform for the 2026-10-05 demo, Phase 2 deletes its code. Alternatives:
  a fresh lean build; forking `caos-v2` (the legacy snapshot). Design §1.
- D2 (2026-10-04) — Cloud Run serves the SPA, the API and the run stream from one
  origin; Firebase Hosting is not used, because its rewrites cut requests at 60 s and
  the stream is long-lived. Design §2 R2.
- D3 (2026-10-04) — One Cloud Run instance with CPU always allocated, because the
  LangGraph worker runs inside the API process. Design §2 R3.
- D4 (2026-10-04) — Cloud SQL for PostgreSQL 17 over the Unix socket; documents on a
  Cloud Storage bucket mounted at `/data/blobs` through the existing filesystem
  backend. Design §4.2.
- D5 (2026-10-04) — OpenRouter is the production model provider, on the owner's
  `caos-qualification` key; its limit is the hard spend stop. Design §4.4.
- D6 (2026-10-04) — Identity: a `firebase` edge mode verifying Firebase ID tokens on the
  server, roles from a server-side email map, email/password sign-in so the content
  security policy keeps Trusted Types and same-origin COOP. Design §4.3.
- D7 (2026-10-04) — Dependency `firebase` (npm, modular `app` and `auth` only) for
  sign-in and token refresh. Alternative: hand-written calls to the Identity Toolkit
  and Secure Token REST APIs, which would re-implement refresh and persistence.
- D9 (2026-10-06) — Tests that pinned upstream commits (b1c1c83, 84eb06d) are re-anchored on synthetic commits: the fork's fresh history (D1) does not carry them.
