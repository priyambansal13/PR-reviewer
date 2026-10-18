"""Pretend to be GitHub: send a signed webhook to the running gateway.

uv run python scripts/send_event.py opened           # new PR -> review queued
uv run python scripts/send_event.py synchronize      # new commit (random SHA) -> new review
uv run python scripts/send_event.py merged           # PR merged -> learner queued
uv run python scripts/send_event.py opened --bad-signature
"""

import argparse
import json
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings  # noqa: E402
from services.gateway.signature import sign  # noqa: E402
from tests.fixtures.events import delivery_id, pr_event  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["opened", "synchronize", "merged", "draft"])
parser.add_argument("--url", default="http://localhost:8000/webhook/github")
parser.add_argument("--bad-signature", action="store_true")
args = parser.parse_args()

match args.action:
    case "opened":
        payload = pr_event("opened")
    case "draft":
        payload = pr_event("opened", draft=True)
    case "synchronize":
        payload = pr_event("synchronize", head={"ref": "feature/login", "sha": secrets.token_hex(20)})
    case "merged":
        payload = pr_event("closed", state="closed", merged=True, merged_at="2026-10-18T10:00:00Z")

body = json.dumps(payload).encode()
secret = "wrong" if args.bad_signature else get_settings().github_webhook_secret
resp = httpx.post(
    args.url,
    content=body,
    headers={
        "x-github-event": "pull_request",
        "x-github-delivery": delivery_id(),
        "x-hub-signature-256": sign(secret, body),
        "content-type": "application/json",
    },
)
print(resp.status_code, resp.text)
