import json

import httpx
import respx

from release_watch.config import Settings
from release_watch.watch.login import AVEPOINT, LoginResult, SessionFetcher, browser_login

BL = "http://browserless.browserless.svc.cluster.local:3000/function"
PASSWORD = 'p"\\${x}'  # quotes, a backslash and a template marker: must reach the form unchanged
PAGE = "<html><body><main><h2>October 2026</h2><p>New things.</p></main></body></html>"
AVE = "https://learn.avepoint.com/m365/whats-new.html"
COOKIES = [{"name": "s", "value": "v", "domain": "learn.avepoint.com"}]


def settings(**kw):
    values = dict(database_url="postgresql+psycopg://x@y/z", docextractor_api_key="k", browserless_token="tok",
                  avepoint_user="u@x", avepoint_password=PASSWORD)
    return Settings(**(values | kw))


def reply(**data):
    return httpx.Response(200, json={"data": data})


async def login(s=None):
    async with httpx.AsyncClient() as client:
        return await browser_login(client, s or settings(), AVEPOINT)


@respx.mock
async def test_login_without_credentials_makes_no_request():
    route = respx.post(BL).mock(return_value=reply(ok=True, step="done", cookies=COOKIES))
    assert await login(settings(avepoint_user="")) == LoginResult(False, "no_credentials", [])
    assert not route.called


@respx.mock
async def test_login_sends_credentials_only_in_context():
    route = respx.post(BL).mock(return_value=reply(ok=True, step="done", cookies=COOKIES))
    await login()
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["context"]["user"] == "u@x" and body["context"]["pw"] == PASSWORD
    assert PASSWORD not in body["code"] and "u@x" not in body["code"]
    assert req.headers["Authorization"] == "Bearer tok" and "tok" not in str(req.url)


@respx.mock
async def test_login_success_returns_cookies():
    respx.post(BL).mock(return_value=reply(ok=True, step="done", cookies=COOKIES))
    result = await login()
    assert result.ok and result.cookies == COOKIES and result.reason == ""
    assert "v" not in repr(result).replace("cookies", "")


@respx.mock
async def test_login_stopped_step_is_the_reason():
    respx.post(BL).mock(return_value=reply(ok=False, step="password field"))
    assert await login() == LoginResult(False, "stopped at password field", [])


@respx.mock
async def test_login_error_reply_is_never_echoed():
    route = respx.post(BL).mock(return_value=httpx.Response(500, text=f"boom {PASSWORD}"))
    result = await login()
    assert result == LoginResult(False, "browserless_http_500", []) and PASSWORD not in repr(result)
    route.mock(return_value=httpx.Response(429, text="busy"))
    assert (await login()).reason == "browserless_http_429"
    assert route.call_count == 2  # one request per login, no retry


@respx.mock
async def test_login_transport_error_is_a_reason():
    respx.post(BL).mock(side_effect=httpx.ConnectError("refused"))
    assert (await login()).reason == "browserless_ConnectError"


@respx.mock
async def test_session_fetcher_reads_page_with_cookies():
    route = respx.post(BL).mock(return_value=reply(html=PAGE, finalUrl=AVE, status=200, contentType="text/html"))
    async with httpx.AsyncClient() as client:
        result = await SessionFetcher(client, settings(), COOKIES, pause=0).fetch(AVE)
    assert json.loads(route.calls[0].request.content)["context"]["cookies"] == COOKIES
    assert (result.outcome, result.text, result.status) == ("ok", PAGE, 200)


@respx.mock
async def test_session_fetcher_signed_out_page_is_blocked():
    respx.post(BL).mock(return_value=reply(html="<html>Sign in</html>" + "x" * 2000, status=200,
                                           finalUrl="https://learn.avepoint.com/login?returnUrl=%2Fm365"))
    async with httpx.AsyncClient() as client:
        result = await SessionFetcher(client, settings(), COOKIES, pause=0).fetch(AVE)
    assert result.outcome == "blocked"


@respx.mock
async def test_session_fetcher_browserless_error_is_an_error_result():
    respx.post(BL).mock(return_value=httpx.Response(503, text="queue full"))
    async with httpx.AsyncClient() as client:
        result = await SessionFetcher(client, settings(), COOKIES, pause=0).fetch(AVE)
    assert (result.outcome, result.reason) == ("error", "browserless_http_503")
