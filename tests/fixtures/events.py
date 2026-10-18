"""Builders for realistic (trimmed) GitHub webhook payloads."""

import copy
import uuid

_PR_EVENT = {
    "action": "opened",
    "number": 7,
    "pull_request": {
        "number": 7,
        "title": "Add login endpoint",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "user": {"login": "dev", "id": 11},
        "head": {"ref": "feature/login", "sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
        "html_url": "https://github.com/octo/demo/pull/7",
    },
    "repository": {
        "id": 100,
        "full_name": "octo/demo",
        "owner": {"login": "octo", "id": 1},
        "private": False,
    },
    "installation": {"id": 555, "node_id": "x"},
    "sender": {"login": "dev"},
}


def pr_event(action: str = "opened", **pr_overrides) -> dict:
    ev = copy.deepcopy(_PR_EVENT)
    ev["action"] = action
    ev["pull_request"].update(pr_overrides)
    return ev


def installation_event(action: str = "created") -> dict:
    return {
        "action": action,
        "installation": {"id": 555, "account": {"login": "octo"}},
        "repositories": [{"id": 100, "full_name": "octo/demo"}, {"id": 101, "full_name": "octo/api"}],
    }


def delivery_id() -> str:
    return str(uuid.uuid4())
