"""Generates monitoring/grafana/dashboards/pr-reviewer.json.

Edit here, then run: uv run python monitoring/build_dashboard.py
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
SEVERITY_COLORS = {"critical": "dark-red", "high": "orange", "medium": "yellow", "low": "blue", "info": "gray"}
panels: list[dict] = []
_id = 0


def _next_id() -> int:
    global _id
    _id += 1
    return _id


def row(title: str, y: int) -> None:
    panels.append(
        {
            "type": "row",
            "title": title,
            "id": _next_id(),
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "collapsed": False,
            "panels": [],
        }
    )


def target(expr: str, legend: str = "", instant: bool = False) -> dict:
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": chr(65 + len(legend) % 26),
        "instant": instant,
        "range": not instant,
    }


def stat(title, expr, x, y, w=4, unit="short", decimals=None, desc=""):
    panels.append(
        {
            "type": "stat",
            "title": title,
            "description": desc,
            "id": _next_id(),
            "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": 4},
            "targets": [target(expr, instant=True)],
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "decimals": decimals,
                    "color": {"mode": "thresholds"},
                    "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                },
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"]},
                "colorMode": "value",
                "graphMode": "none",
                "textMode": "value",
            },
        }
    )


def series(title, targets, x, y, w=12, h=8, unit="short", stack=False, bars=False, desc="", overrides=None):
    panels.append(
        {
            "type": "timeseries",
            "title": title,
            "description": desc,
            "id": _next_id(),
            "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": targets,
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "custom": {
                        "drawStyle": "bars" if bars else "line",
                        "fillOpacity": 60 if bars else 15,
                        "lineWidth": 2,
                        "stacking": {"mode": "normal" if stack else "none"},
                        "spanNulls": True,
                        "showPoints": "auto",
                    },
                },
                "overrides": overrides or [],
            },
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        }
    )


def bargauge(title, expr, legend, x, y, w=8, h=8, unit="short", desc="", overrides=None):
    panels.append(
        {
            "type": "bargauge",
            "title": title,
            "description": desc,
            "id": _next_id(),
            "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [target(expr, legend, instant=True)],
            "fieldConfig": {
                "defaults": {"unit": unit, "min": 0, "color": {"mode": "palette-classic"}},
                "overrides": overrides or [],
            },
            "options": {
                "orientation": "horizontal",
                "displayMode": "gradient",
                "showUnfilled": True,
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        }
    )


def color(name, c):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}],
    }


SEV = [color(s, c) for s, c in SEVERITY_COLORS.items()]
RANGE = "$__range"
RATE = "$__rate_interval"

# ---- overview -----------------------------------------------------------------------------------------------------
row("Overview (selected time range)", 0)
stat("Reviews completed", f'sum(increase(prr_reviews_total{{status="saved"}}[{RANGE}]))', 0, 1, decimals=0)
stat("Findings", f"sum(increase(prr_findings_total[{RANGE}]))", 4, 1, decimals=0)
stat("Comments posted", f"sum(increase(prr_inline_comments_total[{RANGE}]))", 8, 1, decimals=0)
stat("LLM cost", f"sum(increase(prr_llm_cost_usd_total[{RANGE}]))", 12, 1, unit="currencyUSD", decimals=4)
stat(
    "p95 review time",
    f"histogram_quantile(0.95, sum by (le) (increase(prr_review_duration_seconds_bucket[{RANGE}])))",
    16,
    1,
    unit="s",
    decimals=1,
    desc="Fetch diff + 4 parallel agents + merge + save",
)
stat("Failed reviews", f'sum(increase(prr_reviews_total{{status="failed"}}[{RANGE}])) or vector(0)', 20, 1, decimals=0)

# ---- pipeline -----------------------------------------------------------------------------------------------------
row("Pipeline", 5)
series(
    "Webhooks at the gateway",
    [target(f"sum by (result) (increase(prr_webhooks_total[{RATE}]))", "{{result}}")],
    0,
    6,
    w=8,
    bars=True,
    stack=True,
    desc="forwarded = valid signature; bad_signature = rejected fakes",
)
series(
    "Reviews by outcome",
    [target(f"sum by (status) (increase(prr_reviews_total[{RATE}]))", "{{status}}")],
    8,
    6,
    w=8,
    bars=True,
    stack=True,
)
series(
    "Celery queue depth",
    [target('redis_key_size{key=~"review|post|learn"}', "{{key}}")],
    16,
    6,
    w=8,
    desc="Tasks waiting per queue. Growing = workers can't keep up.",
)

# ---- agents -------------------------------------------------------------------------------------------------------
row("Agents (LangGraph)", 14)
series(
    "Agent latency p50 / p95",
    [
        target(
            f"histogram_quantile(0.95, sum by (le, agent) (rate(prr_agent_duration_seconds_bucket[{RATE}])))",
            "p95 {{agent}}",
        ),
        target(
            f"histogram_quantile(0.50, sum by (le, agent) (rate(prr_agent_duration_seconds_bucket[{RATE}])))",
            "p50 {{agent}}",
        ),
    ],
    0,
    15,
    unit="s",
    desc="The four agents run in parallel, so a review takes about as long as the slowest one.",
)
series(
    "Review duration p50 / p95",
    [
        target(f"histogram_quantile(0.95, sum by (le) (rate(prr_review_duration_seconds_bucket[{RATE}])))", "p95"),
        target(f"histogram_quantile(0.50, sum by (le) (rate(prr_review_duration_seconds_bucket[{RATE}])))", "p50"),
    ],
    12,
    15,
    unit="s",
)
bargauge(
    "Findings by severity",
    f"sum by (severity) (increase(prr_findings_total[{RANGE}]))",
    "{{severity}}",
    0,
    23,
    overrides=SEV,
)
bargauge(
    "Raw findings per agent (before merge)",
    f"sum by (agent) (increase(prr_agent_findings_total[{RANGE}]))",
    "{{agent}}",
    8,
    23,
)
bargauge("Agent failures", f"sum by (agent) (increase(prr_agent_failures_total[{RANGE}]))", "{{agent}}", 16, 23)

# ---- llm ----------------------------------------------------------------------------------------------------------
row("LLM usage", 31)
bargauge(
    "Tokens by component", f"sum by (component) (increase(prr_llm_tokens_total[{RANGE}]))", "{{component}}", 0, 32, w=12
)
series(
    "Cost over time",
    [target(f"sum by (component) (increase(prr_llm_cost_usd_total[{RATE}]))", "{{component}}")],
    12,
    32,
    unit="currencyUSD",
    bars=True,
    stack=True,
)

# ---- github + learner ---------------------------------------------------------------------------------------------
row("GitHub & Learner", 40)
series(
    "GitHub API responses",
    [target(f"sum by (status_class) (increase(prr_github_requests_total[{RATE}]))", "{{status_class}}")],
    0,
    41,
    w=8,
    bars=True,
    stack=True,
    overrides=[color("2xx", "green"), color("4xx", "orange"), color("5xx", "red"), color("error", "dark-red")],
)
series(
    "Reviews posted",
    [target(f"sum by (event, result) (increase(prr_reviews_posted_total[{RATE}]))", "{{result}} {{event}}")],
    8,
    41,
    w=8,
    bars=True,
    stack=True,
)
bargauge(
    "Learner",
    f"sum by (result) (increase(prr_conventions_total[{RANGE}]))",
    "conventions {{result}}",
    16,
    41,
    desc="Conventions proposed by the Learner: accepted vs rejected (no evidence / failed fact-check)",
)

# ---- http ---------------------------------------------------------------------------------------------------------
row("HTTP services", 49)
series(
    "Request rate",
    [target(f"sum by (job, handler) (rate(http_requests_total[{RATE}]))", "{{job}} {{handler}}")],
    0,
    50,
    unit="reqps",
)
series(
    "Latency p95",
    [
        target(
            f"histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket[{RATE}])))",
            "{{job}}",
        )
    ],
    12,
    50,
    unit="s",
)

dashboard = {
    "uid": "pr-reviewer",
    "title": "PR Reviewer",
    "tags": ["pr-reviewer"],
    "timezone": "browser",
    "schemaVersion": 39,
    "refresh": "10s",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
}
out = Path(__file__).parent / "grafana" / "dashboards" / "pr-reviewer.json"
out.write_text(json.dumps(dashboard, indent=2))
print(f"wrote {out} ({len(panels)} panels)")
