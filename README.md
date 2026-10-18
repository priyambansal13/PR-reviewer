# AI PR Code Reviewer

Event-driven GitHub Pull Request reviewer. A GitHub App webhook triggers four parallel LangGraph agents running on
GPT-4o-mini: Static Analysis, Security (OWASP Top 10), Architecture and Code Style. Their merged findings are posted
back to the PR as inline comments. A Learner service picks up each repo's conventions from merged PRs.

FastAPI microservices (gateway, webhook, orchestrator, reviewer, learner) · Celery + Redis · PostgreSQL ·
Prometheus + Grafana · Langfuse.

- **Plan & architecture:** [docs/PLAN.md](docs/PLAN.md)
- **How each phase works:** [docs/LEARNING-NOTES.md](docs/LEARNING-NOTES.md)
- **Status:** Phase 1 done: signed webhooks → gateway → webhook service → Postgres + Redis → workers (stubs).
  Next: Phase 2 (GitHub App auth + fetching diffs).

## Quick start

Needs Docker Desktop and [uv](https://docs.astral.sh/uv/).

```bash
./setup.sh start     # creates .env (+ secrets), installs deps, migrates, builds and starts everything
./setup.sh status    # containers and health
./setup.sh event opened   # send a signed fake GitHub PR event to the gateway
./setup.sh logs orchestrator-worker
./setup.sh test      # run tests
./setup.sh psql      # poke at the database
./setup.sh stop
```

| Thing | Where |
|---|---|
| Gateway (public entry) | `http://localhost:8000` — `POST /webhook/github` |
| Webhook service | internal only (`webhook:8001` on the compose network) |
| Workers | `orchestrator-worker` (queue `review`), `learner-worker` (queue `learn`) |
| Postgres | `localhost:5435` (user/pass/db `reviewer`) |
| Redis | `localhost:6380` |

## Layout

```
common/          shared: config, SQLAlchemy models + async session, Celery app & queue names, logging
services/        gateway, webhook, orchestrator (worker), learner (worker); reviewer arrives in Phase 4
scripts/         send_event.py — pretend to be GitHub
migrations/      Alembic (async)
tests/           pytest — DB tests run in a rolled-back transaction
docs/            plan and setup guides
```
