# AI PR Code Reviewer

[![CI](https://github.com/priyambansal13/PR-reviewer/actions/workflows/ci.yml/badge.svg)](https://github.com/priyambansal13/PR-reviewer/actions/workflows/ci.yml)

Event-driven GitHub Pull Request reviewer. A GitHub App webhook triggers four parallel LangGraph agents running on
GPT-4o-mini: Static Analysis, Security (OWASP Top 10), Architecture and Code Style. Their merged findings are posted
back to the PR as inline comments. A Learner service picks up each repo's conventions from merged PRs.

FastAPI microservices (gateway, webhook, orchestrator, reviewer, learner) · Celery + Redis · PostgreSQL ·
Prometheus + Grafana · Langfuse.

- **Plan & architecture:** [docs/PLAN.md](docs/PLAN.md)
- **How each phase works:** [docs/LEARNING-NOTES.md](docs/LEARNING-NOTES.md)
- **GitHub App setup:** [docs/SETUP-GITHUB-APP.md](docs/SETUP-GITHUB-APP.md)
- **Deploying to AWS (EKS):** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- **Status:** all 7 phases done: runs locally end to end on real PRs; CI on every push; EKS manifests and deploy
  workflow ready (not deployed).

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
./setup.sh knowledge owner/repo     # what the Learner knows about a repo
./setup.sh learn owner/repo#12      # run the Learner on a PR now
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
| Grafana | `http://localhost:3030` — dashboard **PR Reviewer** (anonymous view; admin/admin to edit) |
| Prometheus | `http://localhost:9090` |
| Langfuse | your Langfuse Cloud project (set `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`) |
| Postgres | `localhost:5435` (user/pass/db `reviewer`) |
| Redis | `localhost:6380` |

## Layout

```
common/          shared: config, models + async session, Celery app, GitHub App client, diff parser
services/        gateway, webhook, orchestrator (worker), reviewer (worker), learner (worker)
scripts/         send_event.py (pretend to be GitHub), github_app.py (check App, set webhook URL)
migrations/      Alembic (async)
monitoring/      prometheus.yml, Grafana provisioning, dashboard generator (build_dashboard.py)
deploy/k8s/      Kubernetes manifests (kustomize): Deployments, HPA, PDB, Ingress (ALB), NetworkPolicies, migrate Job
.github/         ci.yml (lint, tests, manifests, image) and deploy.yml (manual: ECR → EKS)
tests/           pytest — DB tests run in a rolled-back transaction
docs/            plan and setup guides
```
