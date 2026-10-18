import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from redis.asyncio import Redis

from common.github import DiffUnavailable, GitHubAppAuth, GitHubClient, GitHubError, GitHubRetryable
from common.github import client as client_mod
from services.orchestrator.fetch import Superseded, fetch_diff

API = "https://api.github.com"
REPO = "octo/demo"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, key.public_key()


@pytest.fixture
async def redis():
    r = Redis.from_url("redis://localhost:6380/15")  # db 15 = tests only
    try:
        await r.ping()
    except OSError:
        pytest.skip("Redis not reachable — run ./setup.sh start")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", instant)


@pytest.fixture
async def http():
    async with httpx.AsyncClient(base_url=API) as c:
        yield c


def token_route(mock, token="ghs_abc"):
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    return mock.post(f"{API}/app/installations/555/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": token, "expires_at": expires})
    )


def test_app_jwt_claims(keypair, http):
    pem, public = keypair
    auth = GitHubAppAuth("12345", pem, http)
    now = time.time()
    claims = jwt.decode(auth.app_jwt(now), public, algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(now) - 60
    assert claims["exp"] - claims["iat"] <= 600  # GitHub rejects JWTs that live longer than 10 minutes


@respx.mock
async def test_installation_token_is_cached_in_redis(keypair, http, redis):
    route = token_route(respx)
    auth = GitHubAppAuth("12345", keypair[0], http, redis)
    assert await auth.installation_token(555) == "ghs_abc"
    assert await auth.installation_token(555) == "ghs_abc"
    assert route.call_count == 1
    ttl = await redis.ttl("gh:installation-token:555")
    assert 3000 < ttl <= 3300  # 1h minus the 5 min safety margin
    sent_jwt = route.calls[0].request.headers["authorization"].removeprefix("Bearer ")
    assert jwt.decode(sent_jwt, keypair[1], algorithms=["RS256"])["iss"] == "12345"


@respx.mock
async def test_401_refreshes_token_once(keypair, http, redis):
    token_route(respx)
    pulls = respx.get(f"{API}/repos/{REPO}/pulls/7").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"number": 7})]
    )
    await redis.set("gh:installation-token:555", "stale")
    gh = GitHubClient(http, GitHubAppAuth("1", keypair[0], http, redis), 555)
    assert (await gh.get_pull(REPO, 7))["number"] == 7
    assert pulls.calls[0].request.headers["authorization"] == "Bearer stale"
    assert pulls.calls[1].request.headers["authorization"] == "Bearer ghs_abc"


@respx.mock
async def test_retries_5xx_then_gives_up(keypair, http):
    token_route(respx)
    respx.get(f"{API}/repos/{REPO}/pulls/7").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json={"number": 7})]
    )
    gh = GitHubClient(http, GitHubAppAuth("1", keypair[0], http), 555)
    assert (await gh.get_pull(REPO, 7))["number"] == 7

    respx.get(f"{API}/repos/{REPO}/pulls/8").mock(return_value=httpx.Response(503))
    with pytest.raises(GitHubRetryable):
        await gh.get_pull(REPO, 8)


@respx.mock
async def test_rate_limit_is_retryable_and_404_is_not(keypair, http):
    token_route(respx)
    gh = GitHubClient(http, GitHubAppAuth("1", keypair[0], http), 555)
    respx.get(f"{API}/repos/{REPO}/pulls/9").mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "1"})
    )
    with pytest.raises(GitHubRetryable):
        await gh.get_pull(REPO, 9)
    respx.get(f"{API}/repos/{REPO}/pulls/10").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubError) as err:
        await gh.get_pull(REPO, 10)
    assert err.value.status == 404 and not isinstance(err.value, GitHubRetryable)


DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n a\n+b\n"


@respx.mock
async def test_fetch_diff_uses_compare_for_exact_commits(keypair, http):
    token_route(respx)
    respx.get(f"{API}/repos/{REPO}/pulls/7").mock(return_value=httpx.Response(200, json={"head": {"sha": "h" * 40}}))
    compare = respx.get(f"{API}/repos/{REPO}/compare/{'b' * 40}...{'h' * 40}").mock(
        return_value=httpx.Response(200, text=DIFF)
    )
    gh = GitHubClient(http, GitHubAppAuth("1", keypair[0], http), 555)
    out = await fetch_diff(gh, REPO, 7, "b" * 40, "h" * 40, max_lines=100)
    assert out.source == "compare"
    assert compare.calls[0].request.headers["accept"] == "application/vnd.github.diff"
    assert [f.path for f in out.selection.files] == ["x.py"]


@respx.mock
async def test_fetch_diff_falls_back_to_files_api(keypair, http):
    token_route(respx)
    respx.get(f"{API}/repos/{REPO}/pulls/7").mock(return_value=httpx.Response(200, json={"head": {"sha": "h" * 40}}))
    respx.get(f"{API}/repos/{REPO}/compare/{'b' * 40}...{'h' * 40}").mock(return_value=httpx.Response(406))
    respx.get(f"{API}/repos/{REPO}/pulls/7/files").mock(
        return_value=httpx.Response(
            200, json=[{"filename": "y.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+print(1)"}]
        )
    )
    gh = GitHubClient(http, GitHubAppAuth("1", keypair[0], http), 555)
    out = await fetch_diff(gh, REPO, 7, "b" * 40, "h" * 40, max_lines=100)
    assert out.source == "files-api"
    assert out.files[0].added_lines() == {1}


@respx.mock
async def test_fetch_diff_skips_superseded_commit(keypair, http):
    token_route(respx)
    respx.get(f"{API}/repos/{REPO}/pulls/7").mock(return_value=httpx.Response(200, json={"head": {"sha": "n" * 40}}))
    gh = GitHubClient(http, GitHubAppAuth("1", keypair[0], http), 555)
    with pytest.raises(Superseded):
        await fetch_diff(gh, REPO, 7, "b" * 40, "h" * 40, max_lines=100)


def test_compare_diff_unavailable_is_its_own_error():
    assert issubclass(DiffUnavailable, GitHubError)
