# CAOS on Firebase — Layer 0

Governed source documents to committee-ready credit conclusions, as a non-enterprise
deployment on Firebase and Google Cloud. A fork of `caos-databricks` (the enterprise
build) with the Databricks platform unplugged: Cloud Run serves the React workspace,
the API and the run stream from one origin; the LangGraph worker runs in the same
process; Cloud SQL Postgres holds the store and the graph checkpoints; a Cloud Storage
bucket mounted at `/data/blobs` holds every byte by digest; Firebase Auth signs people
in; every model call goes through one factory, answered by OpenRouter.

The build contract is `docs/specs/2026-10-04-firebase-design.md`; decisions are
`docs/decisions.md`.

## Status

- **Skeleton only.** The CAOS code arrives as one fresh-history commit when the owner
  names the fork point in `caos-databricks` (pending; do not copy code before that).
- **Phase 1 (unplug, for the first demo):** OpenRouter model provider, Firebase identity
  mode, Dockerfile, Cloud Run deploy. Databricks code stays in the tree, inert.
- **Phase 2 (strip, after the demo):** delete platform identity, Lakebase, the UC volume
  backend, the bundle files and the `databricks-*` dependencies, in reviewed steps.

## Repo map (after the fork)

- `caos/` — the app: `api/` (edge, identity, sections, commands, health), `store/`,
  `evidence/`, `methodology/`, `graph/`, `calculators/`, `deliverable/`, `models.py`
  (the model factory), `blobs.py`, `serve.py`.
- `frontend/` — the React workspace, built to `frontend/dist` and served by the app.
- `vendor/deploy-v/` — the methodology bundle, byte-verified at use.
- `icm/` — agent stage contracts; `scripts/` — gates; `tests/` — the suite.
- `Dockerfile`, `docs/DEPLOY.md` — the Cloud Run image and runbook (Phase 1).

## Gates (all must exit 0; none may be loosened)

Retained from `caos-databricks`: `ruff check`, `ruff format --check`, `mypy`, `pytest`
with coverage ≥ 80% against a real Postgres, `check_vocabulary`, `check_tested`,
`io_budget --assert`, bandit with its floor, `pip-audit --strict`, `gitleaks`,
`uv lock --check`, `complexipy` ≤ 15 (legacy baselined), `jscpd`, `check_gate_config`
(suppressions only fall), `check_icm`, `pre-commit`, the frontend's
`lint|typecheck|test|build|a11y`, `verify_package.py`.
Dropped with the platform: `databricks bundle *`, `workspace_stub.py`, `shipped_boot.py`,
`enterprise_deploy.sh`. Added: a container smoke (`docker build` + boot to health OK).

## Invariants (never weaken)

1. Pinned sources only; no web discovery. 2. Evidence reads fail closed with a typed
code. 3. The host owns identity: a Firebase ID token verified on the server, a role
from the server's own map; nothing the client asserts decides who it is.
4. The bundle is the methodology authority, verified on the bytes at use.
5. Human gates are digest-bound. 6. Execution is durable and exactly-once: the
accepted-attempt ledger is the truth. 7. Calculation is pure and finite; Decimal on
money. 8. Budgets fail closed; no model call without a reservation. 9. Module output
is the strict canonical envelope. 10. The route is resolved once and pinned.
11. Citations are coordinate-anchored or refused.

## Conventions

- Coding runs through sub-agent-driven development (global rules); the controller writes
  no code. PRs ≤ 800 changed lines; ship larger work as a stacked series.
- Secrets live in Secret Manager or the owner's environment, never in the repo. The
  Firebase web config is public by design and may be committed.
- Typed refusals only; never `str(exc)` on the wire or in a log; never log document
  text or a token.
- No new dependency without a `Dn` entry in `docs/decisions.md`.
- Only public or synthetic documents go through this deployment: model calls leave
  for OpenRouter.
- Merges are the owner's. Push and open PRs from the terminal pane.
