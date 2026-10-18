# AI PR Code Reviewer

Event-driven GitHub Pull Request reviewer. A GitHub App webhook triggers four parallel LangGraph agents running on
GPT-4o-mini: Static Analysis, Security (OWASP Top 10), Architecture and Code Style. Their merged findings are posted
back to the PR as inline comments. A Learner service picks up each repo's conventions from merged PRs.

FastAPI microservices (gateway, webhook, orchestrator, reviewer, learner) · Celery + Redis · PostgreSQL ·
Prometheus + Grafana · Langfuse.

- **Plan & architecture:** [docs/PLAN.md](docs/PLAN.md)
- **How each phase works:** [docs/LEARNING-NOTES.md](docs/LEARNING-NOTES.md)
- **GitHub App setup:** [docs/SETUP-GITHUB-APP.md](docs/SETUP-GITHUB-APP.md)
- **Status:** Phase 4 done: every PR gets an "AI Review" check and a GitHub review with inline comments and
  one-click suggestions (about $0.0025 per review). Next: Phase 5 (Learner).

## Quick start

Needs Docker Desktop and [uv](https://docs.astral.sh/uv/).

```bash
./setup.sh start     # creates .env (+ secrets), installs deps, migrates, builds and starts everything
./setup.sh status    # containers and health
./setup.sh tunnel    # public HTTPS URL for GitHub (updates the App's webhook URL automatically)
./setup.sh github    # check the GitHub App and its installations
./setup.sh event opened   # send a signed fake GitHub PR event to the gateway
./setup.sh runs      # recent reviews: status, findings, tokens, cost
./setup.sh show owner/repo#12       # findings of a PR's latest review
./setup.sh rereview owner/repo#12   # review it again
./setup.sh repost owner/repo#12     # post the saved review again (no LLM cost; never double-posts)
./setup.sh logs orchestrator-worker
./setup.sh test      # run tests
./setup.sh psql      # poke at the database
./setup.sh stop
```

| Thing | Where |
|---|---|
| Gateway (public entry) | `http://localhost:8000` — `POST /webhook/github` |
| Webhook service | internal only (`webhook:8001` on the compose network) |
| Workers | `orchestrator-worker` (queue `review`), `reviewer-worker` (queue `post`), `learner-worker` (queue `learn`) |
| Postgres | `localhost:5435` (user/pass/db `reviewer`) |
| Redis | `localhost:6380` |

## Layout

```
common/          shared: config, models + async session, Celery app, GitHub App client, diff parser
services/        gateway, webhook, orchestrator (worker), reviewer (worker), learner (worker)
scripts/         send_event.py (pretend to be GitHub), github_app.py (check App, set webhook URL)
migrations/      Alembic (async)
tests/           pytest — DB tests run in a rolled-back transaction
docs/            plan and setup guides
```
