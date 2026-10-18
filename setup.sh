#!/usr/bin/env bash
# PR Reviewer helper.  Usage: ./setup.sh <command>
#   start    install deps, start Postgres + Redis, run migrations, build + start all services
#   stop     stop the containers (data is kept)
#   status   show containers and health
#   event    send a signed fake GitHub event:  ./setup.sh event opened|synchronize|merged|draft
#   tunnel   start a public HTTPS URL for the gateway (cloudflared) and point the GitHub App at it
#   tunnel-stop
#   github   check the GitHub App: identity, permissions, installations (and sync repos to the DB)
#   runs     recent review runs
#   show     findings of a PR's latest run:    ./setup.sh show owner/repo#12
#   rereview reset a PR's latest run and review it again:  ./setup.sh rereview owner/repo#12
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
  # Generate the secrets we own (the webhook secret is pasted into the GitHub App settings in Phase 2)
  for key in INTERNAL_TOKEN GITHUB_WEBHOOK_SECRET; do
    grep -qE "^${key}=" .env || echo "${key}=" >> .env
    if grep -qE "^${key}=(change-me)?$" .env; then
      sed -i.bak "s|^${key}=.*|${key}=$(openssl rand -hex 32)|" .env && rm -f .env.bak
      echo "Generated ${key} in .env"
    fi
  done
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
  docker compose up -d postgres redis
  wait_healthy prr-postgres
  wait_healthy prr-redis
  cmd_migrate
  docker compose build gateway
  docker compose up -d
  wait_healthy prr-gateway
  echo "Ready: gateway http://localhost:8000 · Postgres localhost:5435 · Redis localhost:6380"
}

cmd_migrate() { uv run alembic upgrade head; }

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

github_ready() {
  [[ -n "$(env_value GITHUB_APP_ID)" ]] && [[ -f "$(env_value GITHUB_PRIVATE_KEY_PATH)" ]]
}

cmd_tunnel_stop() {
  # only ever stop OUR cloudflared (other projects run their own tunnels)
  if [[ -f .logs/tunnel.pid ]] && kill -0 "$(cat .logs/tunnel.pid)" 2>/dev/null; then
    kill "$(cat .logs/tunnel.pid)" && echo "Tunnel stopped"
  fi
  rm -f .logs/tunnel.pid
}

cmd_tunnel() {
  need cloudflared "brew install cloudflared"
  mkdir -p .logs
  cmd_tunnel_stop
  nohup cloudflared tunnel --no-autoupdate --url http://localhost:8000 >.logs/tunnel.log 2>&1 &
  echo $! >.logs/tunnel.pid
  local url=""
  for _ in $(seq 1 45); do
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' .logs/tunnel.log | head -1 || true)
    [[ -n "$url" ]] && break
    sleep 1
  done
  [[ -z "$url" ]] && { echo "Tunnel did not start — see .logs/tunnel.log" >&2; exit 1; }
  echo "$url" >.logs/tunnel.url
  echo "Public URL:   $url"
  echo "Webhook URL:  $url/webhook/github"
  if github_ready; then
    uv run python scripts/github_app.py set-webhook "$url/webhook/github"
  else
    echo "(GitHub App not configured yet — paste the Webhook URL into the App form; see docs/SETUP-GITHUB-APP.md)"
  fi
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_tunnel_stop; docker compose stop ;;
  migrate) cmd_migrate ;;
  status)  docker compose ps; [[ -f .logs/tunnel.url ]] && echo "Tunnel: $(cat .logs/tunnel.url)" ;;
  tunnel)  cmd_tunnel ;;
  tunnel-stop) cmd_tunnel_stop ;;
  github)  uv run python scripts/github_app.py check ;;
  runs)    uv run python scripts/runs.py list ;;
  show)    uv run python scripts/runs.py show "$2" ;;
  rereview) uv run python scripts/runs.py rereview "$2" ;;
  event)   uv run python scripts/send_event.py "${@:2}" ;;
  test)    uv run pytest "${@:2}" ;;
  lint)    uv run ruff check . && uv run ruff format --check . ;;
  logs)    docker compose logs -f "${@:2}" ;;
  psql)    docker exec -it prr-postgres psql -U reviewer -d reviewer ;;
  reset)
    read -r -p "Delete ALL pr-reviewer containers and data? [y/N] " ok
    [[ "$ok" == "y" ]] && docker compose down -v ;;
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
