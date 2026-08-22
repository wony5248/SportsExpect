#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
COMPOSE="docker compose -f compose.yaml -f compose.oracle.yaml"

cd "$PROJECT_DIR"

$COMPOSE exec -T scheduler python -m backend.app.cli backup

LATEST=$(find data/backups -maxdepth 1 -type f -name 'baseball-*.db' -print | sort | tail -n 1)
if [ -z "$LATEST" ]; then
  echo "ERROR: no backup file was created." >&2
  exit 1
fi

echo "Latest backup: $LATEST"
ls -lh "$LATEST"
sha256sum "$LATEST"
