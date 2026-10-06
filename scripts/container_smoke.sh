#!/usr/bin/env bash
# Build the image, boot it against a throwaway database on the local TEST
# Postgres, wait for every health code OK, and assert a token-less read is 401.
# Needs no real credential; cleans up the container and the database on exit.
set -u
cd "$(dirname "$0")/.."

ADMIN_URL="postgresql://postgres:local-test-admin-only@127.0.0.1:55437/postgres"
DB=caos_container_smoke
NAME=caos-container-smoke
PORT=8001
IMAGE=caos-firebase:smoke
BASE="http://127.0.0.1:${PORT}"

db() { # $1 = SQL statement, run autocommit on the throwaway server
  ADMIN_URL="$ADMIN_URL" SQL="$1" uv run python -c '
import os, psycopg
with psycopg.connect(os.environ["ADMIN_URL"], autocommit=True) as c:
    c.execute(os.environ["SQL"])'
}

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1
  db "DROP DATABASE IF EXISTS $DB WITH (FORCE)" >/dev/null 2>&1
}
fail() { echo "FAIL: $1"; docker logs --tail 30 "$NAME" 2>&1 | sed 's/^/  /' >&2; exit 1; }
trap cleanup EXIT

cleanup
db "CREATE DATABASE $DB" || { echo "FAIL: cannot create the database"; exit 1; }
docker build -t "$IMAGE" . >/dev/null || { echo "FAIL: docker build"; exit 1; }

docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" \
  --add-host host.docker.internal:host-gateway \
  -e "CAOS_DATABASE_URL=postgresql://postgres:local-test-admin-only@host.docker.internal:55437/$DB" \
  -e CAOS_BLOB_ROOT=/tmp/blobs \
  -e OPENROUTER_API_KEY=sk-or-test \
  -e CAOS_MODEL_ENDPOINT=openai/gpt-6-luna \
  -e CAOS_MODEL_PRICE=openai/gpt-6-luna,0.0000002,0.000001,2026-10-04 \
  -e CAOS_FIREBASE_PROJECT=caos-smoke \
  -e CAOS_PUBLIC_ORIGIN=https://caos-smoke.example \
  -e CAOS_ROLE_MAP=smoke@example.com=ANALYST \
  "$IMAGE" >/dev/null || fail "docker run"

health=""
for _ in $(seq 1 60); do
  health=$(curl -s "$BASE/api/health" || true)
  if [ -n "$health" ] && HEALTH="$health" python3 -c '
import json, os, sys
d = json.loads(os.environ["HEALTH"])
fields = ("store", "bundle", "blobs", "identity", "workers")
ok = d.get("status") == "ready" and all(d.get(f) == "OK" for f in fields)
sys.exit(0 if ok else 1)' 2>/dev/null; then
    break
  fi
  health=""
  sleep 2
done
[ -n "$health" ] || fail "health never answered ready with every code OK"

body=$(curl -s -w '\n%{http_code}' "$BASE/api/v1/book")
status=${body##*$'\n'}
[ "$status" = "401" ] || fail "token-less read answered $status, not 401"
case "$body" in
  *NOT_AUTHENTICATED*) ;;
  *) fail "token-less read did not answer NOT_AUTHENTICATED" ;;
esac

echo "PASS: container boots, health ready and all OK, token-less read is 401 NOT_AUTHENTICATED"
