#!/usr/bin/env bash
# The fork's gate runner (fork-owned; FD26). Runs the CLAUDE.md "Gates" commands with
# independent ones in parallel, one log file per gate, and a summary table.
#
#   scripts/fork_gates.sh quick [--base REF]
#   scripts/fork_gates.sh full  [--base REF] [--all] [--log-dir DIR] [--keep-db] [--dry-run]
#
# full  : every documented gate, unchanged. Frontend gates run only if the diff against
#         the base touches frontend/ (or --all). pytest and postgres-races use this
#         checkout's own Postgres (scripts/worktree_postgres.sh), never the shared one.
# quick : ruff on the changed files, mypy, the tests that reference a changed module,
#         and the frontend lint/typecheck/vitest when frontend/src changed.
# Shared resources: the container smoke (compose test-postgres :55437, port 8001) and the
# vite-preview gates (:4173) are serialized across checkouts by a mkdir lock under
# $TMPDIR (FORK_LOCK_TIMEOUT seconds, default 3600). Ctrl-C kills the running gates.
# --dry-run prints the plan (phases, parallel chains, commands) and runs nothing.
# Gate output goes to log files (a shell wrapper can rewrite pytest's stdout); read the
# exit codes from the table. macOS bash 3.2 compatible.

MODE=""
BASE="origin/main"
ALL=0
DRY=0
KEEPDB=0
LOG=""
# Upstream gate-config baseline commit (CLAUDE.md "$B").
GATE_B=b13110e545ebbc9b74bc7efd3dd3388afb6c0fc5

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    full | quick) MODE=$1 ;;
    --base) BASE=${2:?--base needs a ref}; shift ;;
    --all) ALL=1 ;;
    --dry-run) DRY=1 ;;
    --keep-db) KEEPDB=1 ;;
    --log-dir) LOG=${2:?--log-dir needs a path}; shift ;;
    -h | --help) usage ;;
    *) echo "fork_gates: unknown argument $1" >&2; usage ;;
  esac
  shift
done
[ -n "$MODE" ] || usage

ROOT=$(cd "$(dirname "$0")/.." && pwd -P) || exit 2
cd "$ROOT" || exit 2

PLAN=()  # entries: phase|chain|name|command (phases run in order, chains in parallel)
add() { PLAN+=("$1|$2|$3|$4"); }

have_base() { git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null 2>&1; }

# Changed set: committed vs base, uncommitted, untracked. A failing git diff fails the
# function instead of yielding an empty set.
changed_files_strict() {
  local a b c
  a=$(git diff --name-only "$BASE...HEAD") || return 1
  b=$(git diff --name-only HEAD) || return 1
  c=$(git ls-files --others --exclude-standard) || return 1
  printf '%s\n%s\n%s\n' "$a" "$b" "$c" | sort -u
}

# The same set minus files deleted from the tree.
changed_files() {
  changed_files_strict | while read -r f; do [ -e "$f" ] && echo "$f"; done
}

quoted() { local out="" f; for f in "$@"; do out="$out $(printf '%q' "$f")"; done; echo "$out"; }

# --- plans -------------------------------------------------------------------------

plan_full() {
  local fe=0 files
  if [ "$ALL" -eq 1 ]; then
    fe=1
  elif ! files=$(changed_files_strict); then
    echo "fork_gates: cannot diff against $BASE; running the frontend gates (fail closed)" >&2
    fe=1
  elif echo "$files" | grep -q '^frontend/'; then
    fe=1
  else
    echo "frontend gates skipped (no change under frontend/ vs $BASE); CI runs them"
  fi
  FE=$fe
  # shellcheck disable=SC2016  # expanded later by bash -c
  local pg='CAOS_TEST_POSTGRES_URL="$FORK_PGURL" CAOS_REQUIRE_POSTGRES=1'
  add 0 sync sync "uv sync --locked --all-groups"
  [ "$fe" -eq 1 ] && add 0 fe-install npm-ci "npm --prefix frontend ci --ignore-scripts"
  # pytest, then postgres-races: never two suites on one Postgres at once.
  add 1 py pytest "$pg uv run pytest -n auto -m 'not live_provider'"
  add 1 py scan-floors-cov "uv run python scripts/scan_floors.py coverage.xml --cobertura"
  add 1 py postgres-races "$pg uv run pytest --no-cov tests/test_postgres_races.py"
  add 1 lock uv-lock-check "uv lock --check"
  add 1 ruff ruff-check "uv run ruff check ."
  add 1 ruff ruff-format "uv run ruff format --check ."
  add 1 mypy mypy "uv run mypy caos scripts tests"
  add 1 vocab check-vocabulary "uv run python scripts/check_vocabulary.py"
  add 1 tested check-tested "uv run python scripts/check_tested.py"
  add 1 io io-budget "uv run python scripts/io_budget.py --assert"
  add 1 bandit bandit "uv run bandit -r caos scripts icm -f json -o bandit.json; uv run python scripts/scan_floors.py bandit.json --no-parse-errors --cover caos scripts icm --unscanned tests"
  add 1 audit pip-audit "uv run pip-audit --strict"
  add 1 leaks gitleaks "gitleaks git --no-banner"
  add 1 cx complexipy "uv run complexipy caos scripts icm --max-complexity-allowed 15"
  add 1 dup jscpd "npx --prefix frontend --no-install jscpd --threshold 3 --min-lines 10 --ignore '**/node_modules/**,**/dist/**,**/dist-demo/**' caos scripts icm frontend/src"
  add 1 gc check-gate-config "uv run python scripts/check_gate_config.py"
  add 1 gc check-gate-config-against-B "uv run python scripts/check_gate_config.py --against $GATE_B"
  add 1 icm check-icm "uv run python scripts/check_icm.py"
  add 1 docs document-register "uv run python scripts/document_register.py"
  add 1 pkg verify-package "uv run python -B vendor/deploy-v/verify_package.py"
  if [ "$fe" -eq 1 ]; then
    add 1 fe-a npm-audit "npm --prefix frontend audit --omit=dev"
    add 1 fe-a fe-lint "npm --prefix frontend run lint"
    add 1 fe-a fe-typecheck "npm --prefix frontend run typecheck"
    add 1 fe-a fe-test "npm --prefix frontend run test"
    # build empties frontend/dist, which the py chain's platform-boot tests read, and
    # a11y and the workbench share vite preview's fixed port 4173 across checkouts:
    # a phase of its own, after the py chain, under the shared-resource lock.
    add 2 fe-b fe-build "npm --prefix frontend run build"
    add 2 fe-b fe-build-demo "npm --prefix frontend run build:demo"
    add 2 fe-b fe-a11y "npm --prefix frontend run a11y"
    add 2 fe-b fe-workbench "npm --prefix frontend run test:workbench"
  fi
  # pre-commit may rewrite files and re-run the linters: after the parallel batch.
  add 3 precommit pre-commit "uv run pre-commit run --all-files"
  # The smoke hardcodes the compose test-postgres (127.0.0.1:55437), a container name and
  # port 8001, so it needs compose up and the shared-resource lock. It measures Docker's
  # disk itself (needs >= 5 GB free) and builds an image.
  add 4 smoke container-smoke "docker compose up -d --wait test-postgres && scripts/container_smoke.sh; rc=\$?; docker image rm caos-firebase:smoke >/dev/null 2>&1; exit \$rc"
}

plan_quick() {
  if ! have_base; then
    echo "fork_gates: base $BASE not found (use --base REF)" >&2
    exit 2
  fi
  local files py="" tests="" f mod pkg leaf hits q n xd
  files=$(changed_files)
  for f in $files; do
    case "$f" in *.py) py="$py $f" ;; esac
  done
  if [ -n "$py" ]; then
    # shellcheck disable=SC2086
    q=$(quoted $py)
    add 1 ruff ruff-check-changed "uv run ruff check$q"
    add 1 ruff ruff-format-changed "uv run ruff format --check$q"
    add 1 mypy mypy "uv run mypy caos scripts tests"
  else
    echo "quick: no changed Python files; ruff and mypy skipped"
  fi
  for f in $py; do
    case "$f" in
      tests/test_*.py) tests="$tests $f" ;;
      caos/* | scripts/*)
        mod=${f%.py}; mod=${mod%/__init__}; pkg=${mod%/*}; leaf=${mod##*/}
        mod=$(echo "$mod" | tr / .); pkg=$(echo "$pkg" | tr / .)
        hits=$(grep -rlE --include='*.py' \
          -e "${mod//./\\.}([^A-Za-z0-9_]|\$)" \
          -e "from ${pkg//./\\.} import .*\\b$leaf\\b" \
          -e "scripts/$leaf\\.py" tests 2>/dev/null | grep '^tests/test_')
        tests="$tests $hits" ;;
    esac
  done
  tests=$(echo "$tests" | tr ' ' '\n' | grep . | sort -u | tr '\n' ' ')
  if [ -n "$tests" ]; then
    n=$(echo "$tests" | wc -w | tr -d ' ')
    xd=""; [ "$n" -gt 4 ] && xd=" -n auto"
    # shellcheck disable=SC2086
    add 1 pytest pytest-mapped "env \${FORK_PGURL:+CAOS_TEST_POSTGRES_URL=\"\$FORK_PGURL\"} uv run pytest -q --no-cov$xd -m 'not live_provider'$(quoted $tests)"
    NEED_PG=1
  else
    echo "quick: no test references a changed module; running no tests (not the whole suite)"
  fi
  if echo "$files" | grep -q '^frontend/src/'; then
    add 1 fe lint "npm --prefix frontend run lint"
    add 1 fe2 typecheck "npm --prefix frontend run typecheck"
    add 1 fe3 vitest-changed "cd frontend && npx --no-install vitest run --mode demo --changed $BASE --passWithNoTests"
  fi
}

# --- engine ------------------------------------------------------------------------

print_plan() {
  local e phase chain rest name cmd last=-1
  for e in "${PLAN[@]}"; do
    phase=${e%%|*}; rest=${e#*|}; chain=${rest%%|*}; rest=${rest#*|}
    name=${rest%%|*}; cmd=${rest#*|}
    if [ "$phase" != "$last" ]; then echo "phase $phase (chains in a phase run in parallel):"; last=$phase; fi
    printf '  [%s] %-28s %s\n' "$chain" "$name" "$cmd"
  done
}

run_chain() {  # phase chain
  local e phase chain rest name cmd t0 rc
  for e in "${PLAN[@]}"; do
    phase=${e%%|*}; rest=${e#*|}; chain=${rest%%|*}; rest=${rest#*|}
    name=${rest%%|*}; cmd=${rest#*|}
    [ "$phase" = "$1" ] && [ "$chain" = "$2" ] || continue
    t0=$SECONDS
    bash -c "$cmd" > "$LOG/$name.log" 2>&1
    rc=$?
    echo "$rc $((SECONDS - t0))" > "$LOG/$name.rc"
  done
}

LOCK_DIR="${TMPDIR:-/tmp}/caos-fork-gates-shared.lock"
LOCK_HELD=0
LOCK_TIMEOUT="${FORK_LOCK_TIMEOUT:-3600}"
LOCKED_PHASES=" 2 4 "  # fe-b (vite preview :4173) and the container smoke (:8001, compose :55437)
CHAIN_PIDS=""

acquire_lock() {
  local waited=0 owner
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    owner=$(cat "$LOCK_DIR/pid" 2>/dev/null)
    if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
      echo "fork_gates: removing stale lock of dead pid $owner" >&2
      rm -rf "${LOCK_DIR:?}"
      continue
    fi
    if [ "$waited" -ge "$LOCK_TIMEOUT" ]; then
      echo "fork_gates: waited ${LOCK_TIMEOUT}s for $LOCK_DIR (held by pid ${owner:-?}); giving up" >&2
      return 1
    fi
    [ $((waited % 60)) -eq 0 ] && echo "fork_gates: waiting for the shared-resource lock held by pid ${owner:-?} (another checkout's smoke or preview run)" >&2
    sleep 5
    waited=$((waited + 5))
  done
  echo $$ > "$LOCK_DIR/pid"
  LOCK_HELD=1
}

release_lock() {
  if [ "$LOCK_HELD" -eq 1 ]; then rm -rf "${LOCK_DIR:?}"; LOCK_HELD=0; fi
}

kill_tree() {
  local child
  for child in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$child"; done
  kill "$1" 2>/dev/null
}

cleanup() {
  local p
  for p in $CHAIN_PIDS; do kill_tree "$p"; done
  CHAIN_PIDS=""
  release_lock
  if [ "$STOP_DB" -eq 1 ]; then scripts/worktree_postgres.sh stop >/dev/null 2>&1; fi
}

run_plan() {
  local e phase chain rest ph chains pids c
  for ph in 0 1 2 3 4; do
    chains=" "
    for e in "${PLAN[@]}"; do
      phase=${e%%|*}; rest=${e#*|}; chain=${rest%%|*}
      [ "$phase" = "$ph" ] || continue
      case "$chains" in *" $chain "*) ;; *) chains="$chains$chain " ;; esac
    done
    [ "$chains" = " " ] && continue
    case "$LOCKED_PHASES" in
      *" $ph "*) acquire_lock || { echo "lock timeout" > "$LOG/lock.err"; return 1; } ;;
    esac
    pids=""
    for c in $chains; do
      run_chain "$ph" "$c" &
      pids="$pids $!"
    done
    CHAIN_PIDS="$pids"
    # shellcheck disable=SC2086
    wait $pids
    CHAIN_PIDS=""
    release_lock
  done
}

summary() {
  local e rest name rc secs bad
  {
    printf '%-30s %-6s %s\n' gate exit seconds
    for e in "${PLAN[@]}"; do
      rest=${e#*|}; rest=${rest#*|}; name=${rest%%|*}
      if [ -f "$LOG/$name.rc" ]; then
        read -r rc secs < "$LOG/$name.rc"
      else
        rc="?"; secs=0
      fi
      printf '%-30s %-6s %s\n' "$name" "$rc" "$secs"
    done
  } | tee "$LOG/summary.txt"
  bad=$(grep -vcE ' 0 +[0-9]+$|^gate ' "$LOG/summary.txt")
  echo "logs: $LOG"
  [ "$bad" -eq 0 ]
}

# --- main --------------------------------------------------------------------------

FE=0
NEED_PG=0
STOP_DB=0
if [ "$MODE" = "full" ]; then plan_full; NEED_PG=1; else plan_quick; fi

if [ "$DRY" -eq 1 ]; then
  echo "mode=$MODE base=$BASE frontend=$FE"
  [ "$NEED_PG" -eq 1 ] && echo 'postgres: scripts/worktree_postgres.sh (exported as FORK_PGURL for the commands below)'
  [ "$MODE" = "full" ] && echo "lock: phases$LOCKED_PHASES hold $LOCK_DIR (mkdir lock, waits up to ${LOCK_TIMEOUT}s, released on exit/INT/TERM)"
  print_plan
  exit 0
fi

if [ ${#PLAN[@]} -eq 0 ]; then echo "nothing to run"; exit 0; fi

[ -n "$LOG" ] || LOG=$(mktemp -d "${TMPDIR:-/tmp}/caos-gates-XXXXXX")
mkdir -p "$LOG" || exit 2
rm -f "${LOG:?}"/*.rc

if [ "$NEED_PG" -eq 1 ]; then
  line=$(scripts/worktree_postgres.sh) || {
    if [ "$MODE" = "full" ]; then echo "fork_gates: no Postgres, cannot run the gates" >&2; exit 1; fi
    echo "fork_gates: no Postgres; mapped tests run without one" >&2
    line=""
  }
  FORK_PGURL=${line#CAOS_TEST_POSTGRES_URL=}
  export FORK_PGURL
  if [ "$MODE" = "full" ] && [ "$KEEPDB" -eq 0 ]; then STOP_DB=1; fi
fi
trap cleanup EXIT
trap 'echo "fork_gates: interrupted; stopping gates" >&2; exit 130' INT TERM

echo "HEAD $(git rev-parse --short HEAD), logs in $LOG"
run_plan
summary
