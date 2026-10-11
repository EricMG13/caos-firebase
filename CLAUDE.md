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

- **Re-forked** at upstream `rebuild/databricks` d07fea550d33c42ece390aad970d7ca994b9c406
  with upstream history kept (FD15), on branch `refork`.
- **Phase 1 replayed:** OpenRouter model provider (FD8, FD16), Firebase edge mode (FD6,
  FD10, FD12, FD14), frontend sign-in (FD7, FD13), Dockerfile, `cloudbuild.yaml`,
  `scripts/container_smoke.sh`, `docs/DEPLOY.md` (FD11). Databricks code stays in the
  tree, inert. Deploy follows `docs/DEPLOY.md`; the container smoke must pass first.
- **Hardening H1-H5 lands on this branch in later commits:** runbook hardening (H1),
  shutdown inside Cloud Run's grace (H2, FD20), user-provisioning script (H3, FD19),
  blob-write check on the mount (H4), Architecture Center items A1-A5 and A7 (H5).
- **Phase 2 is dropped** (FD17): the Databricks code is not deleted, so upstream merges
  stay clean.
- **Firebase project:** `caos-firebase-prod`.

## Tracking upstream

```bash
git fetch upstream
git switch -c sync/<yyyy-mm-dd> main
git merge upstream/rebuild/databricks
```

Rule: upstream's version wins; the Firebase change is re-applied on top with the
smallest footprint. Run the full gate list and the container smoke, then open a PR to
`main` from the terminal pane; the owner merges. Merge with a merge commit; never squash
or rebase (FD22). `size` follows FD21.

## Repo map (after the fork)

- `caos/` — the app: `api/` (edge, identity, sections, commands, health), `store/`,
  `evidence/`, `methodology/`, `graph/`, `calculators/`, `deliverable/`, `models.py`
  (the model factory), `blobs.py`, `serve.py`.
- `frontend/` — the React workspace, built to `frontend/dist` and served by the app.
- `vendor/deploy-v/` — the methodology bundle, byte-verified at use.
- `icm/` — agent stage contracts; `scripts/` — gates; `tests/` — the suite.
- `Dockerfile`, `docs/DEPLOY.md` — the Cloud Run image and runbook (Phase 1).

## Gates (all must exit 0; none may be loosened)

Upstream's gate list at B, minus the Databricks-only gates (`databricks bundle *`,
`tests/workspace_stub.py` runs, `tests/shipped_boot.py`, `enterprise_deploy.sh`), plus
the container smoke:

```bash
uv sync --locked --all-groups && docker compose up -d --wait
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy caos scripts tests
CAOS_TEST_POSTGRES_URL=postgresql://postgres:local-test-admin-only@127.0.0.1:55437/postgres CAOS_REQUIRE_POSTGRES=1 uv run pytest -n auto -m "not live_provider"
uv run python scripts/scan_floors.py coverage.xml --cobertura
CAOS_TEST_POSTGRES_URL=postgresql://postgres:local-test-admin-only@127.0.0.1:55437/postgres CAOS_REQUIRE_POSTGRES=1 uv run pytest --no-cov tests/test_postgres_races.py
uv run python scripts/check_vocabulary.py
uv run python scripts/check_tested.py
uv run python scripts/io_budget.py --assert
uv run bandit -r caos scripts icm -f json -o bandit.json; uv run python scripts/scan_floors.py bandit.json --no-parse-errors --cover caos scripts icm --unscanned tests
uv run pip-audit --strict
gitleaks git --no-banner
uv run complexipy caos scripts icm --max-complexity-allowed 15
npx --prefix frontend --no-install jscpd --threshold 3 --min-lines 10 --ignore "**/node_modules/**,**/dist/**,**/dist-demo/**" caos scripts icm frontend/src
uv run python scripts/check_gate_config.py
uv run python scripts/check_gate_config.py --against "$B"
uv run python scripts/check_icm.py
uv run python scripts/document_register.py
uv run python -B vendor/deploy-v/verify_package.py
uv run pre-commit run --all-files
npm --prefix frontend ci --ignore-scripts
npm --prefix frontend audit --omit=dev
npm --prefix frontend run lint && npm --prefix frontend run typecheck && npm --prefix frontend run test && npm --prefix frontend run build && npm --prefix frontend run build:demo && npm --prefix frontend run a11y && npm --prefix frontend run test:workbench
scripts/container_smoke.sh
```

CI is upstream's `ci.yml`, unchanged, so syncs never conflict on it. Its `bundle`
job runs against the workspace stand-in and is not a fork gate. The fork's own
gates (container smoke, npm audit) run in `.github/workflows/fork.yml`. The `size`
job follows FD21. `scripts/check_gate_config.py` inspects only `ci.yml`, so fork.yml's
pins and settings are checked by review, not by a gate.

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
- No new dependency without an `FDn` entry in `docs/decisions.md`.
- Only public or synthetic documents go through this deployment: model calls leave
  for OpenRouter.
- Merges are the owner's. Push and open PRs from the terminal pane.
