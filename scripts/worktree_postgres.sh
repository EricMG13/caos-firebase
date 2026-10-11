#!/usr/bin/env bash
# A throwaway Postgres 17 for THIS checkout, so parallel worktrees never share the
# compose database (they raced on tests/test_pg_reaper.py). Fork-owned.
#   scripts/worktree_postgres.sh [start]   idempotent; prints CAOS_TEST_POSTGRES_URL=...
#   scripts/worktree_postgres.sh stop      removes the container
# Use: eval "$(scripts/worktree_postgres.sh)"  (the line is exported by fork_gates.sh).
set -u

# Same image (tag-less digest pin) as compose.yaml's test-postgres service.
IMAGE="postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
# The documented local test password (compose.yaml, CLAUDE.md); not a secret.
PASSWORD="local-test-admin-only"

ROOT=$(cd "$(dirname "$0")/.." && pwd -P) || exit 2
if command -v shasum >/dev/null 2>&1; then
  HASH=$(printf '%s' "$ROOT" | shasum -a 256 | cut -c1-8)
else
  HASH=$(printf '%s' "$ROOT" | sha256sum | cut -c1-8)
fi
NAME="caos-pg-$HASH"

if ! docker info >/dev/null 2>&1; then
  echo "worktree_postgres: Docker is unavailable (is the daemon running?)" >&2
  exit 1
fi

case "${1:-start}" in
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1
    echo "removed $NAME" >&2
    exit 0
    ;;
  start) ;;
  *) echo "usage: $0 [start|stop]" >&2; exit 2 ;;
esac

state=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)
if [ "$state" != "true" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1
  docker run -d --name "$NAME" \
    -e POSTGRES_DB=postgres -e POSTGRES_USER=postgres -e "POSTGRES_PASSWORD=$PASSWORD" \
    --tmpfs /var/lib/postgresql/data \
    -p 127.0.0.1::5432 "$IMAGE" >/dev/null || {
    echo "worktree_postgres: docker run failed" >&2
    exit 1
  }
fi

ready=0
i=0
while [ "$i" -lt 60 ]; do
  if docker exec "$NAME" pg_isready -h 127.0.0.1 -U postgres -d postgres >/dev/null 2>&1; then
    ready=1
    break
  fi
  i=$((i + 1))
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "worktree_postgres: $NAME not ready after 60 s" >&2
  docker logs --tail 20 "$NAME" >&2
  exit 1
fi

PORT=$(docker port "$NAME" 5432/tcp | head -n1 | sed 's/.*://')
case "$PORT" in '' | *[!0-9]*)
  echo "worktree_postgres: cannot read the published port" >&2
  exit 1
  ;;
esac
echo "CAOS_TEST_POSTGRES_URL=postgresql://postgres:$PASSWORD@127.0.0.1:$PORT/postgres"
