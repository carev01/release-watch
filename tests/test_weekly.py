from release_watch.models import WatchedSource
from release_watch.watch import weekly
from release_watch.watch.login import LoginResult, LoginSite
from tests.fakes import ScriptedFetcher, SummaryLLM, html_page

A = LoginSite("A", ("a.com",), "", "u", "p")
B = LoginSite("B", ("b.com",), "", "u", "p")
A_URL, B_URL = "https://a.com/rn", "https://b.com/rn"
PAGE = "<h2>October 14, 2026</h2><ul><li>SAML sign-on.</li></ul>"


def add(sf, url):
    with sf() as s:
        s.add(WatchedSource(vendor_name="V", url=url, url_key=url, title="RN", kind="entry_page", state="active",
                            origin="seed", selector="main", login_walled=True))
        s.commit()


async def visit(sf, results: dict, pages: dict, alert_fails=False):
    logins, alerts = [], []
    fetcher = ScriptedFetcher(pages)

    async def login(site):
        logins.append(site.name)
        return results[site.name]

    async def alert(text):
        alerts.append(text)
        if alert_fails:
            raise ConnectionError("teams down")

    out = await weekly.run_login_visits(sf, SummaryLLM(), login=login, fetcher_for=lambda cookies, hosts: fetcher,
                                        alert=alert, confirm_delay=0, sites=(A, B))
    return out, logins, alerts, fetcher


OK = LoginResult(True, "", [{"name": "s", "value": "v"}])


async def test_failed_login_alerts_once_and_skips_only_that_vendor(session_factory):
    add(session_factory, A_URL)
    add(session_factory, B_URL)
    out, logins, alerts, fetcher = await visit(
        session_factory, {"A": LoginResult(False, "stopped at password field", []), "B": OK},
        {B_URL: html_page(B_URL, PAGE)})
    assert logins == ["A", "B"]
    assert alerts == ["Release Watch: the weekly login to A failed (stopped at password field). "
                      "Its 1 pages were not read this week."]
    assert fetcher.calls == [B_URL]
    assert out["vendors"]["A"] == {"login": "failed", "reason": "stopped at password field", "sources": 1, "read": 0}
    assert out["vendors"]["B"]["login"] == "ok" and out["vendors"]["B"]["read"] == 1


async def test_vendor_with_every_page_blocked_reports_zero_read(session_factory):
    add(session_factory, B_URL)
    blocked = html_page(B_URL, "Sign in", outcome="blocked", reason="login_redirect")
    out, _, alerts, _ = await visit(session_factory, {"A": OK, "B": OK}, {B_URL: blocked})
    assert out["vendors"]["B"]["login"] == "ok" and out["vendors"]["B"]["read"] == 0 and alerts == []


async def test_crash_in_one_vendor_does_not_stop_the_other(session_factory, monkeypatch):
    add(session_factory, A_URL)
    add(session_factory, B_URL)
    real = weekly.run_watch

    async def flaky(*args, **kw):
        if kw["login_hosts"] == ("a.com",):
            raise RuntimeError("boom")
        return await real(*args, **kw)

    monkeypatch.setattr(weekly, "run_watch", flaky)
    out, _, alerts, fetcher = await visit(session_factory, {"A": OK, "B": OK}, {B_URL: html_page(B_URL, PAGE)})
    assert out["vendors"]["A"] == {"login": "ok", "reason": "crash: RuntimeError", "sources": 1, "read": 0}
    assert out["vendors"]["B"]["read"] == 1 and fetcher.calls == [B_URL]
    assert alerts == ["Release Watch: the weekly visit to A stopped (crash: RuntimeError)."]


async def test_failed_alert_does_not_stop_the_other_vendor(session_factory):
    add(session_factory, A_URL)
    add(session_factory, B_URL)
    out, _, alerts, fetcher = await visit(
        session_factory, {"A": LoginResult(False, "browserless_http_429", []), "B": OK},
        {B_URL: html_page(B_URL, PAGE)}, alert_fails=True)
    assert len(alerts) == 1 and fetcher.calls == [B_URL]
    assert out["vendors"]["A"]["login"] == "failed" and out["vendors"]["B"]["read"] == 1
