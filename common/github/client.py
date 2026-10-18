"""A small async GitHub REST client that acts as one App installation."""

import asyncio
import logging
import time
from typing import Any

import httpx

from common.github.auth import API_VERSION, GitHubAppAuth

log = logging.getLogger("github")

MAX_ATTEMPTS = 4
MAX_FILES_PAGES = 30  # GitHub returns at most 3000 files for a PR (30 pages × 100)


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHubRetryable(GitHubError):
    """Rate limits, 5xx and network errors — worth retrying later (Celery retries the task)."""


class DiffUnavailable(GitHubError):
    """GitHub won't render this diff (too large / too many files). Fall back to the per-file API."""


class GitHubClient:
    def __init__(self, http: httpx.AsyncClient, auth: GitHubAppAuth, installation_id: int):
        self._http = http
        self._auth = auth
        self.installation_id = installation_id

    async def get_pull(self, repo: str, number: int) -> dict[str, Any]:
        return (await self._request("GET", f"/repos/{repo}/pulls/{number}")).json()

    async def get_compare_diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        """The PR's diff for exactly these commits (three-dot compare = changes since the merge base)."""
        resp = await self._request(
            "GET",
            f"/repos/{repo}/compare/{base_sha}...{head_sha}",
            accept="application/vnd.github.diff",
            ok_statuses=(200, 406, 422),
        )
        if resp.status_code != 200:
            raise DiffUnavailable(f"compare diff not available ({resp.status_code})", resp.status_code)
        return resp.text

    async def list_pull_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for page in range(1, MAX_FILES_PAGES + 1):
            resp = await self._request(
                "GET", f"/repos/{repo}/pulls/{number}/files", params={"per_page": 100, "page": page}
            )
            batch = resp.json()
            files.extend(batch)
            if len(batch) < 100:
                break
        return files

    async def create_check_run(self, repo: str, head_sha: str, name: str, title: str, summary: str) -> int:
        """Shows up in the PR's checks box as "in progress". Needs the App permission Checks: write."""
        resp = await self._request(
            "POST",
            f"/repos/{repo}/check-runs",
            json={
                "name": name,
                "head_sha": head_sha,
                "status": "in_progress",
                "output": {"title": title, "summary": summary},
            },
        )
        return resp.json()["id"]

    async def complete_check_run(
        self, repo: str, check_run_id: int, conclusion: str, title: str, summary: str, text: str | None = None
    ) -> None:
        output = {"title": title[:255], "summary": summary[:65_000]}
        if text:
            output["text"] = text[:65_000]
        await self._request(
            "PATCH",
            f"/repos/{repo}/check-runs/{check_run_id}",
            json={"status": "completed", "conclusion": conclusion, "output": output},
        )

    async def create_review(
        self, repo: str, number: int, commit_id: str, body: str, event: str, comments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """One review = summary body + inline comments, posted atomically (all or nothing)."""
        resp = await self._request(
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            json={"commit_id": commit_id, "body": body, "event": event, "comments": comments},
        )
        return resp.json()

    async def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._paginate(f"/repos/{repo}/pulls/{number}/reviews")

    async def list_review_comments(self, repo: str, number: int, review_id: int) -> list[dict[str, Any]]:
        # The per-review endpoint (/reviews/{id}/comments) only returns legacy diff `position`s, not `line`;
        # the PR-wide endpoint has `line`, so list that and keep this review's comments.
        comments = await self._paginate(f"/repos/{repo}/pulls/{number}/comments")
        return [c for c in comments if c.get("pull_request_review_id") == review_id]

    async def dismiss_review(self, repo: str, number: int, review_id: int, message: str) -> None:
        await self._request(
            "PUT",
            f"/repos/{repo}/pulls/{number}/reviews/{review_id}/dismissals",
            json={"message": message, "event": "DISMISS"},
        )

    async def _paginate(self, path: str, max_pages: int = 10) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = (await self._request("GET", path, params={"per_page": 100, "page": page})).json()
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        ok_statuses: tuple[int, ...] = (200, 201, 204),
        **kwargs: Any,
    ) -> httpx.Response:
        refreshed_token = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            token = await self._auth.installation_token(self.installation_id)
            headers = {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": API_VERSION}
            try:
                resp = await self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise GitHubRetryable(f"{method} {path}: {exc}") from exc
                await asyncio.sleep(_backoff(attempt))
                continue

            if resp.status_code in ok_statuses:
                return resp
            if resp.status_code == 401 and not refreshed_token:
                # cached token was revoked or the App was reinstalled — mint a fresh one once
                await self._auth.forget_installation_token(self.installation_id)
                refreshed_token = True
                continue
            if _is_retryable(resp):
                wait = _retry_after(resp) or _backoff(attempt)
                if attempt == MAX_ATTEMPTS or wait > 60:
                    raise GitHubRetryable(f"{method} {path}: {resp.status_code}", resp.status_code)
                log.warning("%s %s -> %s, retrying in %.1fs", method, path, resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            raise GitHubError(f"{method} {path}: {resp.status_code} {resp.text[:300]}", resp.status_code)
        raise GitHubRetryable(f"{method} {path}: gave up")


def _is_retryable(resp: httpx.Response) -> bool:
    if resp.status_code >= 500 or resp.status_code == 429:
        return True
    # primary/secondary rate limits come back as 403
    return resp.status_code == 403 and (
        resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in resp.text.lower()
    )


def _retry_after(resp: httpx.Response) -> float | None:
    if value := resp.headers.get("retry-after"):
        return float(value)
    if resp.headers.get("x-ratelimit-remaining") == "0" and (reset := resp.headers.get("x-ratelimit-reset")):
        return max(0.0, float(reset) - time.time())
    return None


def _backoff(attempt: int) -> float:
    return min(2.0**attempt, 20.0)
