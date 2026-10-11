# Decisions

Later entries override earlier ones. Details live in the design spec named in each entry.

- FD1 (2026-10-04) — Fork `caos-databricks` into this repository as one fresh-history
  commit at a point the owner names; public GitHub repository. Phase 1 unplugs the
  Databricks platform for the 2026-10-05 demo, Phase 2 deletes its code. Alternatives:
  a fresh lean build; forking `caos-v2` (the legacy snapshot). Design §1.
- FD2 (2026-10-04) — Cloud Run serves the SPA, the API and the run stream from one
  origin; Firebase Hosting is not used, because its rewrites cut requests at 60 s and
  the stream is long-lived. Design §2 R2.
- FD3 (2026-10-04) — One Cloud Run instance with CPU always allocated, because the
  LangGraph worker runs inside the API process. Design §2 R3.
- FD4 (2026-10-04) — Cloud SQL for PostgreSQL 17 over the Unix socket; documents on a
  Cloud Storage bucket mounted at `/data/blobs` through the existing filesystem
  backend. Design §4.2.
- FD5 (2026-10-04) — OpenRouter is the production model provider, on the owner's
  `caos-qualification` key; its limit is the hard spend stop. Design §4.4.
- FD6 (2026-10-04) — Identity: a `firebase` edge mode verifying Firebase ID tokens on the
  server, roles from a server-side email map, email/password sign-in so the content
  security policy keeps Trusted Types and same-origin COOP. Design §4.3.
- FD7 (2026-10-04) — Dependencies `@firebase/app` and `@firebase/auth` (npm; the modular packages only
  — the `firebase` umbrella pulls Firestore, whose `@grpc/grpc-js ~1.9` pin carries two
  high advisories with no fixed release as of firebase 13.0.0) for sign-in and token
  refresh. Versions: @firebase/app 0.16.3, @firebase/auth 1.13.7, chosen so
  `npm audit --omit=dev` is clean (2026-10-11). Alternative: hand-written calls to the Identity Toolkit
  and Secure Token REST APIs, which would re-implement refresh and persistence.
- FD8 (2026-10-04) — `langchain-openai` (already locked) moves from the test group to the
  runtime: `caos.models.chat_model` is `ChatOpenAI` on OpenRouter. `CAOS_REASONING_EFFORT`
  is now sent (`reasoning.effort`) and named in the identity, so the N2 refusal becomes a
  validation; `CAOS_PROVIDER_ORDER` pins the provider without fallbacks and is named in the
  identity as `model@order` (`openrouter/<model>@<order>/<effort>/<max tokens>`, the test
  adapter's form). `CAOS_MODEL_BASE_URL` overrides the base URL: https, or http to a
  loopback host only (the workspace stub answers there); anything else is refused. The
  OpenRouter-free gate (A28) exempts `caos/models.py` alone, the one production seam.
  `CAOS_REASONING_EFFORT` and `CAOS_PROVIDER_ORDER` apply to non-Copilot (OpenRouter)
  models only; a Copilot model takes its effort from its name.
- FD9 — retired by FD15: the upstream commits it re-anchored are in history again.
- FD10 (2026-10-06) — PyJWT and cryptography, already locked through other packages,
  become direct dependencies: caos/api/firebase_auth.py verifies Firebase ID tokens
  with them (FD6).
- FD11 (2026-10-06) — `CAOS_RUN_CEILING=120.00` is set explicitly on deploy, as at the
  fork point; the OpenRouter key's own limit is the hard spend stop.
- FD12 (2026-10-06) — The identity health probe warms Google's certificates in firebase
  mode (`firebase_auth.warm`), so the first signed-in page after a start is not refused;
  it reports `IDENTITY_UNAVAILABLE` while no usable keys are held.
- FD13 (2026-10-06) — The SPA force-refreshes the ID token 120 s before it expires
  (retrying every 30 s on failure), because the SDK's on-demand buffer is 30 s and a
  public listener starts no proactive refresh.
- FD14 (2026-10-06) — `CAOS_ROLE_MAP` tolerates empty entries (a trailing `;`); an empty
  map and every other malformation are still refused.
- FD15 (2026-10-11) — Re-fork at B, upstream `rebuild/databricks` commit
  d07fea550d33c42ece390aad970d7ca994b9c406, with upstream history kept; supersedes
  FD1's fresh history and retires FD9. Spec §3.
- FD16 (2026-10-11) — OpenRouter is the non-Copilot branch of `chat_model`; Copilot
  dispatch is kept inert. Spec §4.
- FD17 (2026-10-11) — The fork tracks upstream by merges (spec §6); the Databricks code
  stays inert; Phase 2 is dropped.
- FD18 (2026-10-11) — Firebase-side provisioning is declared in `firebase.json` and
  applied with the Firebase CLI or MCP server (spec §5).
- FD19 (2026-10-11) — `firebase-admin`, in a non-runtime dependency group, is used only
  by the owner-run provisioning script (H3). The alternative was the hand-written
  Identity Toolkit REST call. Resolved version: `firebase-admin` 7.7.0 (group `ops`).
- FD20 (2026-10-11) — The shutdown drain fits Cloud Run's 10 s SIGTERM grace (H2).
- FD21 (2026-10-11, owner) — CI's 800-line `size` check applies to fork-authored PRs.
  The re-fork PR and upstream-sync PRs are exempt and may merge with `size` red. They
  are reviewed on the fork's own changes: `git diff <upstream commit merged>..HEAD`.
- FD22 (2026-10-11, owner) — The repository is public, with merge commits only (squash
  and rebase merges are off), because a squash or rebase loses upstream's ancestry and
  breaks every later `git merge upstream`. A pre-publication scan (gitleaks over all
  branches, plus a personal-data grep) found nothing that caos-databricks, already
  public, does not already show.
- FD23 (2026-10-11) — Every OpenRouter request carries upstream's cache-off option
  `prompt_cache_options: {mode: explicit}` (F545): automatic prompt caching wrote the
  prompt on almost every call and was rarely read back, so the ledger understated the
  bill (N152); invariant 8 needs the request to match the price pin.
