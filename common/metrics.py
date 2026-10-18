"""All Prometheus metrics in one place, so the Grafana dashboard and the code agree on names.

API services (single process) are scraped on /metrics. Celery workers are pre-forked into several processes, so
they run prometheus_client in *multiprocess mode* (PROMETHEUS_MULTIPROC_DIR): each process writes its values to
mmap files and one HTTP server per container adds them up (common/worker_metrics.py).
"""

from prometheus_client import Counter, Histogram

# Ingress
WEBHOOKS = Counter("prr_webhooks", "Webhook deliveries at the gateway", ["event", "result"])
EVENTS = Counter(
    "prr_webhook_events", "Outcome of handled events in the webhook service", ["event", "action", "result"]
)

# Review pipeline (orchestrator)
REVIEWS = Counter("prr_reviews", "Review runs by final status", ["status"])
REVIEW_SECONDS = Histogram(
    "prr_review_duration_seconds",
    "Whole review: fetch diff + agents + save",
    buckets=(2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300),
)
AGENT_SECONDS = Histogram(
    "prr_agent_duration_seconds", "One agent's run", ["agent"], buckets=(1, 2, 3, 5, 7.5, 10, 15, 20, 30, 45, 60, 90)
)
AGENT_FAILURES = Counter("prr_agent_failures", "Agent runs that raised", ["agent"])
AGENT_FINDINGS = Counter("prr_agent_findings", "Raw findings per agent, before merging", ["agent"])
FINDINGS = Counter("prr_findings", "Findings after merging", ["severity"])
TOKENS = Counter("prr_llm_tokens", "LLM tokens", ["component", "kind"])  # component = agent name, summary, learner
COST = Counter("prr_llm_cost_usd", "LLM cost in USD", ["component"])
DIFF_LINES = Histogram(
    "prr_diff_changed_lines", "Changed lines sent for review", buckets=(10, 50, 100, 250, 500, 1000, 2000, 3000, 5000)
)

# Posting (reviewer)
POSTED = Counter("prr_reviews_posted", "Posting attempts", ["event", "result"])
INLINE_COMMENTS = Counter("prr_inline_comments", "Inline comments posted")

# Learner
LEARNER_RUNS = Counter("prr_learner_runs", "Learner runs", ["result"])
REACTIONS = Counter("prr_feedback_reactions", "Human reactions read from the bot's comments", ["kind"])
CONVENTIONS = Counter("prr_conventions", "Proposed conventions", ["result"])  # accepted | rejected

# GitHub API
GITHUB_REQUESTS = Counter("prr_github_requests", "GitHub API responses", ["status_class"])
