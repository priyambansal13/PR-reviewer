#!/usr/bin/env bash
# PR Reviewer helper.  Usage: ./setup.sh <command>
#   start    install deps, start Postgres + Redis, run migrations
#   stop     stop the containers (data is kept)
#   migrate  apply database migrations
#   test     run the test suite (needs `start` first)
#   lint     ruff check + format check
#   logs     follow container logs
#   psql     open a psql shell on the database
#   reset    DANGER: stop and delete all containers and data
set -euo pipefail
cd "$(dirname "$0")"

need() { command -v "$1" >/dev/null || { echo "Missing: $1 — $2" >&2; exit 1; }; }

ensure_env() {
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env from .env.example — fill in secrets as the phases need them."
  fi
}

wait_healthy() {
  local name=$1
  for _ in $(seq 1 60); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null)" == "healthy" ]] && return 0
    sleep 1
  done
  echo "$name did not become healthy" >&2; exit 1
}

cmd_start() {
  need docker "install Docker Desktop"
  need uv "brew install uv"
  ensure_env
  uv sync --quiet
  docker compose up -d
  wait_healthy prr-postgres
  wait_healthy prr-redis
  cmd_migrate
  echo "Ready: Postgres on localhost:5435, Redis on localhost:6380"
}

cmd_migrate() { uv run alembic upgrade head; }

case "${1:-}" in
  start)   cmd_start ;;
  stop)    docker compose stop ;;
  migrate) cmd_migrate ;;
  test)    uv run pytest "${@:2}" ;;
  lint)    uv run ruff check . && uv run ruff format --check . ;;
  logs)    docker compose logs -f "${@:2}" ;;
  psql)    docker exec -it prr-postgres psql -U reviewer -d reviewer ;;
  reset)
    read -r -p "Delete ALL pr-reviewer containers and data? [y/N] " ok
    [[ "$ok" == "y" ]] && docker compose down -v ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
