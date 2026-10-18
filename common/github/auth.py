"""GitHub App authentication.

Two kinds of credentials:
1. **App JWT:** signed with the App's private key (RS256), valid for at most 10 minutes. It proves "I am the App",
   but can only call /app/* endpoints.
2. **Installation token:** swapped for the JWT at POST /app/installations/{id}/access_tokens. It acts on the repos
   that installation can see, with the permissions the App was granted, and is valid for 1 hour. It is cached in
   Redis so all workers share it instead of each minting their own.
"""

import time
from datetime import datetime
from pathlib import Path

import httpx
import jwt
from redis.asyncio import Redis

from common.config import Settings, get_settings

API_VERSION = "2022-11-28"
TOKEN_CACHE_KEY = "gh:installation-token:{}"
REFRESH_MARGIN_S = 300  # throw a token away 5 min before it expires, so a long task never holds an expired one


class GitHubConfigError(RuntimeError):
    pass


class GitHubAppAuth:
    def __init__(
        self,
        app_id: str,
        private_key: str,
        http: httpx.AsyncClient,
        redis: Redis | None = None,
    ):
        if not app_id:
            raise GitHubConfigError("GITHUB_APP_ID is not set — see docs/SETUP-GITHUB-APP.md")
        self.app_id = app_id
        self._private_key = private_key
        self._http = http
        self._redis = redis

    @classmethod
    def from_settings(
        cls, http: httpx.AsyncClient, redis: Redis | None = None, settings: Settings | None = None
    ) -> "GitHubAppAuth":
        settings = settings or get_settings()
        return cls(settings.github_app_id, load_private_key(settings.github_private_key_path), http, redis)

    def app_jwt(self, now: float | None = None) -> str:
        now = int(now if now is not None else time.time())
        payload = {
            "iat": now - 60,  # GitHub recommends back-dating a minute to absorb clock drift
            "exp": now + 9 * 60,  # the maximum is 10 minutes
            "iss": self.app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        key = TOKEN_CACHE_KEY.format(installation_id)
        if self._redis is not None and (cached := await self._redis.get(key)):
            return cached.decode() if isinstance(cached, bytes) else cached

        resp = await self._http.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if resp.status_code != 201:
            raise GitHubConfigError(
                f"could not get installation token for {installation_id}: {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        token = data["token"]
        if self._redis is not None:
            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            ttl = int(expires_at - time.time() - REFRESH_MARGIN_S)
            if ttl > 0:
                await self._redis.set(key, token, ex=ttl)
        return token

    async def forget_installation_token(self, installation_id: int) -> None:
        if self._redis is not None:
            await self._redis.delete(TOKEN_CACHE_KEY.format(installation_id))


def load_private_key(path: Path) -> str:
    try:
        return Path(path).read_text()
    except FileNotFoundError as exc:
        raise GitHubConfigError(
            f"GitHub App private key not found at {path} — download it from the App settings (see "
            "docs/SETUP-GITHUB-APP.md)"
        ) from exc
