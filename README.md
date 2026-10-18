# AI PR Code Reviewer

Event-driven GitHub Pull Request reviewer. A GitHub App webhook triggers four parallel LangGraph agents running on
GPT-4o-mini: Static Analysis, Security (OWASP Top 10), Architecture and Code Style. Their merged findings are posted
back to the PR as inline comments. A Learner service picks up each repo's conventions from merged PRs.

FastAPI microservices (gateway, webhook, orchestrator, reviewer, learner) · Celery + Redis · PostgreSQL ·
Prometheus + Grafana · Langfuse.

- **Plan & architecture:** [docs/PLAN.md](docs/PLAN.md)
- **Status:** Phase 0 (scaffold) done — infra, shared package, schema. Next: Phase 1 (gateway + webhook ingress).

## Quick start

Needs Docker Desktop and [uv](https://docs.astral.sh/uv/).

```bash
./setup.sh start     # creates .env, installs deps, starts Postgres + Redis, runs migrations
./setup.sh test      # run tests
./setup.sh psql      # poke at the database
./setup.sh stop
```

| Thing | Where |
|---|---|
| Postgres | `localhost:5435` (user/pass/db `reviewer`) |
| Redis | `localhost:6380` |

## Layout

```
common/          shared: config, SQLAlchemy models + async session, Celery app & queue names, logging
services/        the five services (added from Phase 1)
migrations/      Alembic (async)
tests/           pytest — DB tests run in a rolled-back transaction
docs/            plan and setup guides
```
