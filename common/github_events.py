"""The subset of GitHub webhook payloads we use. Unknown fields are ignored, so GitHub adding fields never breaks us.

Reference: https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class User(_Model):
    login: str


class Repo(_Model):
    id: int
    full_name: str
    owner: User


class InstallationRef(_Model):
    id: int


class Ref(_Model):
    ref: str
    sha: str


class PullRequest(_Model):
    number: int
    title: str = ""
    state: str  # open | closed
    draft: bool = False
    merged: bool | None = None
    merged_at: datetime | None = None
    user: User
    head: Ref
    base: Ref


class PullRequestEvent(_Model):
    action: str
    pull_request: PullRequest
    repository: Repo
    installation: InstallationRef | None = None


# Actions that should produce a fresh review of the PR's current head commit.
REVIEW_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
