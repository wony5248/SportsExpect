#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
COMPOSE="docker compose -f compose.yaml -f compose.oracle.yaml"

cd "$PROJECT_DIR"

echo "== Containers =="
$COMPOSE ps

echo "== API health =="
curl --fail --silent --show-error http://127.0.0.1:8000/health
echo

echo "== Data readiness =="
if curl --fail --silent --show-error http://127.0.0.1:8000/ready; then
  echo
else
  echo
  echo "WARNING: /health passed but /ready is degraded. Inspect scheduler logs below." >&2
fi

echo "== Scheduler recent logs =="
$COMPOSE logs --tail=40 scheduler

echo "== Host storage =="
df -h "$PROJECT_DIR"
du -sh data 2>/dev/null || true

echo "== Latest local backup =="
find data/backups -maxdepth 1 -type f -name 'baseball-*.db' -print 2>/dev/null | sort | tail -n 1
