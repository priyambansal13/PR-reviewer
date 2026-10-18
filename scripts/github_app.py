"""GitHub App helper.

uv run python scripts/github_app.py check                 # who am I, where am I installed, sync repos to the DB
uv run python scripts/github_app.py set-webhook URL       # point the App webhook at URL (./setup.sh tunnel)
"""

import asyncio
import sys
from pathlib import Path

import httpx
from sqlalchemy.dialects.postgresql import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings  # noqa: E402
from common.db.models import Installation, Repository  # noqa: E402
from common.db.session import dispose_engine, session_scope  # noqa: E402
from common.github.auth import API_VERSION, GitHubAppAuth  # noqa: E402


def _app_headers(auth: GitHubAppAuth) -> dict:
    return {
        "Authorization": f"Bearer {auth.app_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }


async def check() -> None:
    s = get_settings()
    async with httpx.AsyncClient(base_url=s.github_api_url, timeout=20) as http:
        auth = GitHubAppAuth.from_settings(http)
        app = (await http.get("/app", headers=_app_headers(auth))).raise_for_status().json()
        print(f"App:          {app['name']} (id {app['id']}) — {app['html_url']}")
        print(f"Permissions:  {app['permissions']}")
        print(f"Events:       {app['events']}")
        hook = await http.get("/app/hook/config", headers=_app_headers(auth))
        if hook.status_code == 200:
            print(f"Webhook URL:  {hook.json().get('url')}")

        installs = (await http.get("/app/installations", headers=_app_headers(auth))).raise_for_status().json()
        if not installs:
            print("\nNot installed anywhere yet — open the App page → Install App.")
            return
        for inst in installs:
            token = await auth.installation_token(inst["id"])
            repos = (
                (
                    await http.get(
                        "/installation/repositories",
                        params={"per_page": 100},
                        headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": API_VERSION},
                    )
                )
                .raise_for_status()
                .json()["repositories"]
            )
            print(f"\nInstallation {inst['id']} on {inst['account']['login']}: {len(repos)} repo(s)")
            async with session_scope() as session:
                await session.execute(
                    insert(Installation)
                    .values(id=inst["id"], account_login=inst["account"]["login"])
                    .on_conflict_do_nothing()
                )
                for r in repos:
                    print(f"  - {r['full_name']}")
                    await session.execute(
                        insert(Repository)
                        .values(id=r["id"], full_name=r["full_name"], installation_id=inst["id"])
                        .on_conflict_do_update(
                            index_elements=[Repository.id],
                            set_={"full_name": r["full_name"], "installation_id": inst["id"], "enabled": True},
                        )
                    )
        print("\nSynced to the database.")
    await dispose_engine()


async def set_webhook(url: str) -> None:
    s = get_settings()
    async with httpx.AsyncClient(base_url=s.github_api_url, timeout=20) as http:
        auth = GitHubAppAuth.from_settings(http)
        resp = await http.patch(
            "/app/hook/config",
            headers=_app_headers(auth),
            json={"url": url, "content_type": "json", "secret": s.github_webhook_secret, "insecure_ssl": "0"},
        )
        resp.raise_for_status()
        print(f"GitHub App webhook → {resp.json()['url']}")


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["check"]:
            asyncio.run(check())
        case ["set-webhook", url]:
            asyncio.run(set_webhook(url))
        case _:
            print(__doc__)
            sys.exit(1)
