import asyncio

import httpx
import respx

from release_watch.config import CHROME_USER_AGENT, Settings
from release_watch.fetch import DEFAULT_HOST_POLICIES, Fetcher, HostPolicy, classify_response, policy_for


def settings():
    return Settings(database_url="postgresql+psycopg://x@y/z", docextractor_api_key="k")


def fast_policies():
    fast = {host: HostPolicy(**{**p.__dict__, "min_delay_ms": 0}) for host, p in DEFAULT_HOST_POLICIES.items()}
    for host in ("example.com", "a.com", "slow.com", "down.com"):
        fast[host] = HostPolicy(min_delay_ms=0)
    return fast


def test_cohesity_soft_404():
    policy = policy_for("docs.cohesity.com", DEFAULT_HOST_POLICIES)
    body = "<html><script>window.x='ERROR_FALLBACK;404'</script>" + "x" * 5000 + "</html>"
    assert classify_response(200, "https://docs.cohesity.com/x", "https://docs.cohesity.com/x", body,
                             policy) == ("gone", "soft_404")


def test_login_redirect():
    assert classify_response(200, "https://learn.avepoint.com/x", "https://login.avepoint.com/?r=x",
                             "<html>" + "x" * 5000 + "</html>", HostPolicy()) == ("blocked", "login_redirect")


def test_short_redirecting_page():
    body = "<html><body>Redirecting…</body></html>".ljust(400)
    assert classify_response(200, "https://learn.avepoint.com/x", "https://learn.avepoint.com/x", body,
                             HostPolicy()) == ("blocked", "session_redirect")


def test_status_outcomes():
    long = "x" * 5000
    assert classify_response(404, "https://a.com/x", "https://a.com/x", "", HostPolicy())[0] == "gone"
    assert classify_response(403, "https://a.com/x", "https://a.com/x", "", HostPolicy()) == ("blocked", "forbidden")
    assert classify_response(200, "https://a.com/x", "https://a.com/x", long, HostPolicy()) == ("ok", "")
    assert classify_response(500, "https://a.com/x", "https://a.com/x", long, HostPolicy())[0] == "error"


def test_login_in_original_url_not_flagged():
    assert classify_response(200, "https://a.com/auth/notes", "https://a.com/auth/notes", "x" * 5000,
                             HostPolicy()) == ("ok", "")


@respx.mock
async def test_ibm_uses_curl_ua():
    ibm = respx.get("https://www.ibm.com/docs/x").mock(return_value=httpx.Response(200, text="x" * 5000))
    other = respx.get("https://example.com/").mock(return_value=httpx.Response(200, text="x" * 5000))
    async with Fetcher(settings(), policies=fast_policies()) as f:
        await f.fetch("https://www.ibm.com/docs/x")
        await f.fetch("https://example.com/")
    assert ibm.calls[0].request.headers["User-Agent"] == "curl/8.5.0"
    assert other.calls[0].request.headers["User-Agent"] == CHROME_USER_AGENT


@respx.mock
async def test_429_retry_after_honoured(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("release_watch.fetch.asyncio.sleep", fake_sleep)
    route = respx.get("https://a.com/rn").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, text="x" * 5000)])
    async with Fetcher(settings(), policies=fast_policies()) as f:
        result = await f.fetch("https://a.com/rn")
    assert result.outcome == "ok"
    assert route.call_count == 2
    assert max(sleeps) >= 1


async def test_per_host_concurrency():
    in_flight = 0
    peak = 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, text="x" * 5000)

    transport = httpx.MockTransport(handler)
    async with Fetcher(settings(), policies=fast_policies(), transport=transport) as f:
        results = await asyncio.gather(*(f.fetch(f"https://slow.com/{i}") for i in range(6)))
    assert all(r.outcome == "ok" for r in results)
    assert peak == 2


@respx.mock
async def test_transport_error_is_error_outcome(monkeypatch):
    async def fake_sleep(s):
        return None

    monkeypatch.setattr("release_watch.fetch.asyncio.sleep", fake_sleep)
    respx.get("https://down.com/").mock(side_effect=httpx.ConnectError("nope"))
    async with Fetcher(settings(), policies=fast_policies()) as f:
        result = await f.fetch("https://down.com/")
    assert (result.outcome, result.reason) == ("error", "ConnectError")


async def test_min_delay_between_requests_to_same_host():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="x" * 5000))
    policies = {"paced.com": HostPolicy(min_delay_ms=200)}
    async with Fetcher(settings(), policies=policies, transport=transport) as f:
        start = asyncio.get_running_loop().time()
        await asyncio.gather(f.fetch("https://paced.com/1"), f.fetch("https://paced.com/2"),
                             f.fetch("https://paced.com/3"))
        elapsed = asyncio.get_running_loop().time() - start
    assert elapsed >= 0.4
