# CAOS on Firebase — re-fork onto current caos-databricks (2026-10-11)

Status: approved in conversation 2026-10-11, pending the owner's review of this file.
Amends `2026-10-04-firebase-design.md`; where the two differ, this file wins.

## 1. Why

- The fork point is `caos-databricks` 861b218 (2026-10-04). By a2d1efa9 (2026-10-11)
  upstream `rebuild/databricks` had moved 577 commits (431 files, +93,215 / −23,326),
  309 of them in the last seven days, and it is still under active development.
- Phase 1 (13 commits on `phase1`, 950c877) exists only on the owner's laptop; origin
  holds the skeleton commit 242c6ef.
- The fork's fresh history (D1) makes every catch-up a manual port.

## 2. Owner decisions (2026-10-11)

1. Re-fork from the upstream tip and replay Phase 1 on it.
2. Keep upstream's history; `upstream` is a second remote.
3. Fork plus regular merges: the Databricks code stays in the tree, inert; Phase 2
   (strip) is dropped while the fork tracks upstream.
4. Firebase-side work uses the Firebase agent skills and the Firebase MCP server (§5).

## 3. Building `refork`

1. **Backup.** Push `phase1` to origin unchanged.
2. `git remote add upstream https://github.com/EricMG13/caos-databricks.git`, fetch.
3. Branch `refork` at the tip of `upstream/rebuild/databricks` at the time this step
   runs, called **B** below and recorded in FD15. Upstream keeps moving; commits after
   B arrive with the first sync merge (§6).
4. `git merge -s ours --allow-unrelated-histories main`: the old history (242c6ef,
   88ba489) becomes an ancestor and the tree stays B's, so origin `main` later
   fast-forwards to `refork` with no force-push.
5. **Carry the fork's docs** from 88ba489: `CLAUDE.md`, `README.md`,
   `docs/decisions.md`, `docs/specs/2026-10-04-*.md`,
   `docs/superpowers/plans/2026-10-04-firebase-phase1.md`. Refresh
   `docs/upstream/CLAUDE.caos-databricks.md` from B's `CLAUDE.md`. `.gitignore` is B's
   plus the fork's extra lines. `.gitignore.merged` (a stray file) is not carried.
6. **Replay** `88ba489..phase1` except two commits that keeping the history makes
   unnecessary: 9929b80 (restored the vendored Databricks skills, which B has) and
   bc57dbf (D9: re-anchored tests on synthetic commits; the real commits are now in
   history). Eleven commits remain, plus this spec's commit.
7. **Conflict rule: upstream wins, the Firebase change is re-applied on top with the
   smallest footprint.** New behaviour goes in new files where possible.
   - `caos/models.py` (17 upstream commits in the last week): OpenRouter's `ChatOpenAI`
     replaces only the `ChatDatabricks` construction in `chat_model`; upstream's
     Copilot dispatch (`copilot.parsed`) stays as is and inert, because no Copilot model
     name is configured. Upstream's later behaviour (D117 deadline and lease, D118
     re-run after a late provider error, the Copilot charge path) is kept.
   - `caos/pricing.py`, `caos/graph/worker.py`, tests: upstream's version, Firebase
     change re-applied.
   - `pyproject.toml`: merged by hand; `uv.lock` regenerated with `uv lock`, never
     hand-merged.

## 4. Decision log

- The fork's decisions are renamed D1–D14 → FD1–FD14 in `docs/decisions.md` and every
  fork-owned reference (`CLAUDE.md`, the 2026-10-04 specs and plan, comments and tests
  in files the fork added or changed), so they no longer collide with upstream's D-numbers
  in `docs/rebuild/decisions.md`. Commit messages are not rewritten.
- **FD15** — Re-fork at B with upstream history kept; supersedes FD1's fresh history and
  retires FD9.
- **FD16** — OpenRouter is the non-Copilot branch of `chat_model`; Copilot dispatch is
  kept inert.
- **FD17** — The fork tracks upstream by merges (§6); the Databricks code stays inert;
  Phase 2 is dropped.
- **FD18** — Firebase-side provisioning is declared in `firebase.json` and applied with
  the Firebase CLI or MCP server (§5).

## 5. Firebase side (skills and MCP)

- **Project.** The owner names an existing Firebase project or has one created
  (`npx -y firebase-tools@latest projects:create`), on the Blaze plan. It is the same
  Google Cloud project that runs Cloud Run.
- **Committed config.** `.firebaserc` (default alias) and `firebase.json` with
  `"auth": {"providers": {"emailPassword": true}}`, applied by
  `npx -y firebase-tools@latest deploy --only auth`. No Google sign-in (FD6).
- **Web app and its config.** The web app is registered with the MCP tool
  `firebase_create_app`; its public config comes from `firebase_get_sdk_config` into
  the `VITE_FIREBASE_*` build args. That config is public by design.
- **Users.** The owner creates the email/password accounts. No agent enters or handles
  a password.
- **Serving stays on Cloud Run through `gcloud`** (FD2). App Hosting is for Node
  full-stack frameworks (Next.js, Angular) and the backend is Python. Firebase Hosting
  stays out because of its rewrite timeout.
- **Checks against official docs once the Firebase CLI is signed in**, using the MCP
  `developerknowledge_search_documents`. Each result goes into FD18, or reopens the
  decision it contradicts:
  - V1: the Hosting-to-Cloud-Run rewrite timeout (the basis of FD2).
  - V2: whether App Hosting can run a Python container.
  - V3: the ID-token checks and the certificate URL that `caos/api/firebase_auth.py`
    implements.
  - V4: whether email/password sign-in from the Cloud Run origin needs that host in
    Authorized domains.

## 6. Tracking upstream (added to `CLAUDE.md`)

```bash
git fetch upstream
git switch -c sync/<yyyy-mm-dd> main
git merge upstream/rebuild/databricks
```

Resolve by §3.7's rule, run the full gate list and the container smoke, open a PR to
`main` from the terminal pane; the owner merges. Cadence is the owner's.

## 7. Done means

- Every gate in `CLAUDE.md` exits 0 on `refork`, with the commands and exit codes in the
  final report.
- `scripts/container_smoke.sh` passes.
- `tests/test_firebase_auth.py`, `tests/test_edge_firebase.py` and the frontend's
  `auth-gate` and `sign-out` tests pass against B's code.
- A PR `refork` → `main` is opened from the terminal pane. It exceeds 800 changed lines
  because it imports upstream. The review scope is `git diff B..refork`, which must stay
  near Phase 1's own size.

## 8. Execution

Sub-agent-driven development (global rules). The controller runs steps 3.1–3.6 (git
only, no code). Conflict resolution and fixes go to sub-agents:

| Work | Implementer | Reviewer |
|---|---|---|
| Identity replay (edge, `firebase_auth`, health) | `mx-precise` | `mx-adversary` |
| Model and pricing replay (money) | `mx-precise` | `mx-adversary` |
| Frontend sign-in, container, runbook, `firebase.json` | `mx-feature` | `mx-precise` |
| Whole branch | — | `mx-architect` |

## 9. Out of scope

- Deploying, which needs the owner's sign-ins (`gcloud auth login`, Firebase CLI),
  Blaze billing and the OpenRouter key in Secret Manager.
- Phase 2.
- Google sign-in.

## 10. Risks

- `caos/models.py` churns upstream, so most sync merges will conflict there.
- Upstream may add a boot-time requirement that only Databricks satisfies. The container
  smoke catches it at each sync.
- Merging one large sync at a time gets harder the longer the gap between syncs.
