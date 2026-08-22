#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
COMPOSE="docker compose -f compose.yaml -f compose.oracle.yaml"

cd "$PROJECT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env is missing. Copy .env.oracle.example to .env and replace every example value." >&2
  exit 1
fi

mkdir -p data/backups data/locks
chmod 700 data data/backups data/locks

$COMPOSE config --quiet
$COMPOSE build --pull
$COMPOSE up -d --remove-orphans

attempt=1
while [ "$attempt" -le 12 ]; do
  if curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null; then
    echo "API health check passed."
    $COMPOSE ps
    echo "Deployment completed. Check public HTTPS and run scripts/oracle-check.sh next."
    exit 0
  fi
  sleep 5
  attempt=$((attempt + 1))
done

echo "ERROR: API did not become healthy within 60 seconds." >&2
$COMPOSE ps >&2
$COMPOSE logs --tail=100 api scheduler caddy >&2
exit 1
