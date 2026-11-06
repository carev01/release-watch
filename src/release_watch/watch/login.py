"""The weekly login visit's browser side: sign in to a vendor once in Browserless, then read pages with its cookies.

Credentials travel only in the /function context, never in the script text, and Browserless' reply body is never
echoed: failures are short reason codes. One login per call, never retried.
"""

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from release_watch.config import Settings
from release_watch.fetch import BROWSER_CONCURRENCY, BROWSER_TIMEOUT, FetchResult, HostPolicy, parse_browser_reply
from release_watch.urls import host_of

LOGIN_TIMEOUT = 240.0  # the slowest Securiti login, under Browserless' 300 s session limit

# Shared by the login scripts: a stop result names the step that didn't look as expected.
_HELPERS = r"""
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const stop = step => ({ data: { ok: false, step }, type: 'application/json' });
  const done = async p => {
    const cdp = await p.target().createCDPSession();
    const { cookies } = await cdp.send('Network.getAllCookies');
    return { data: { ok: true, step: 'done', cookies }, type: 'application/json' };
  };
  const leaf = async (p, test) => {
    const h = await p.evaluateHandle(t => [...document.querySelectorAll('body *')].find(e =>
      e.childElementCount === 0 && e.getClientRects().length > 0 && e.innerText &&
      (t.exact ? e.innerText.trim() === t.text : e.innerText.trim().startsWith(t.text))), test);
    return h.asElement();
  };
  const click = async (p, selector) => { const el = await p.$(selector); if (!el) return false; await el.click(); return true; };
  await page.setUserAgent(context.ua);
  await page.setViewport({ width: 1440, height: 900 });
"""

# AvePoint Learn: the AvePoint Online Services sign-in, then back on learn.avepoint.com (trial 2026-11-05).
AVEPOINT_LOGIN = r"""
export default async function ({ page, context }) {
""" + _HELPERS + r"""
  try { await page.goto('https://learn.avepoint.com/api/auth/aos-sso/commercial?returnUrl=%2F',
                        { waitUntil: 'networkidle2', timeout: 60000 }); } catch (e) {}
  await sleep(3000);
  if (!(await page.$('#account_login_username')) || !(await page.$('#account_login_password'))) return stop('login form');
  await page.type('#account_login_username', context.user, { delay: 40 });
  await page.type('#account_login_password', context.pw, { delay: 40 });
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }).catch(() => {}),
                     page.keyboard.press('Enter')]);
  await sleep(5000);
  if (!page.url().startsWith('https://learn.avepoint.com/') || /\/login/.test(page.url())) return stop('signed in');
  return await done(page);
}
"""

# Securiti: email, the "Privaci Demo New" domain, password; then the Help Center pane's open-in-new-window
# control, which signs the browser in to helpcenter.securiti.ai (trial 2026-11-05).
SECURITI_LOGIN = r"""
export default async function ({ page, context }) {
""" + _HELPERS + r"""
  try { await page.goto('https://app.securiti.ai/#/login', { waitUntil: 'networkidle2', timeout: 60000 }); } catch (e) {}
  await sleep(3000);
  if (!(await page.$('#email'))) return stop('login form');
  await page.type('#email', context.user, { delay: 40 });
  if (!(await click(page, 'button[type=submit]'))) return stop('continue button');
  await sleep(6000);
  const dropdown = await leaf(page, { text: 'Select Domain', exact: true });
  if (!dropdown) return stop('domain drop-down');
  await dropdown.click();
  await sleep(2000);
  const org = await leaf(page, { text: 'Privaci Demo New (', exact: false });
  if (!org) return stop('domain option');
  await org.click();
  await sleep(1500);
  if (!(await click(page, 'button[type=submit]'))) return stop('continue button');
  await sleep(6000);
  const pw = await page.$('input[type=password]');
  if (!pw) return stop('password field');
  await pw.type(context.pw, { delay: 40 });
  await page.keyboard.press('Enter');
  for (let i = 0; i < 20 && page.url().includes('#/login'); i++) await sleep(1000);
  if (page.url().includes('#/login')) return stop('signed in');
  await sleep(5000);
  // The top-right "?" (button.c-contextual-help-link) opens the Help Center pane; the sidebar "?" opens a menu
  // instead. The app loads slowly, so wait for the button (waits keep the worst case under LOGIN_TIMEOUT), then for the pane's Document360 frame to show
  // "Open in new tab" (#article-open-new), which opens the signed-in help center in a new window.
  let help = null;
  for (let i = 0; i < 30 && !help; i++) {
    help = await page.$('button.c-contextual-help-link');
    if (!help) await sleep(1000);
  }
  if (!help) return stop('help center button');
  await sleep(3000);
  let openNew = null;
  for (let attempt = 0; attempt < 2 && !openNew; attempt++) {  // a second click if the pane didn't open
    await help.click().catch(() => {});
    for (let i = 0; i < 30 && !openNew; i++) {
      await sleep(1000);
      for (const frame of page.frames()) {
        openNew = await frame.$('#article-open-new').catch(() => null);
        if (openNew) break;
      }
    }
  }
  if (!openNew) return stop('help pane');
  // Matched by address: the browser already holds other pages (about:blank, Browserless' own).
  const opened = page.browser().waitForTarget(t => t.type() === 'page' &&
      t.url().startsWith('https://helpcenter.securiti.ai/'), { timeout: 30000 }).then(t => t.page()).catch(() => null);
  await openNew.click();
  const win = await opened;
  if (!win) return stop('help center window');
  await sleep(5000);
  return await done(win);
}
"""

# One page in a fresh session carrying the signed-in cookies. Same capture as fetch.BROWSER_FUNCTION.
READ_PAGE = r"""
export default async function ({ page, context }) {
  await page.setUserAgent(context.ua);
  await page.setViewport({ width: 1440, height: 900 });
  const cookies = context.cookies.map(c => {
    const o = { name: c.name, value: c.value, domain: c.domain, path: c.path || '/', secure: !!c.secure,
                httpOnly: !!c.httpOnly };
    if (c.sameSite) o.sameSite = c.sameSite;
    if (c.expires && c.expires > 0) o.expires = c.expires;
    return o;
  });
  if (cookies.length) await page.setCookie(...cookies);
  let doc = null;
  page.on('response', r => { if (r.request().isNavigationRequest() && r.frame() === page.mainFrame()) doc = r; });
  try { await page.goto(context.url, { waitUntil: 'networkidle2', timeout: 60000 }); } catch (e) {}
  await new Promise(r => setTimeout(r, 2000));
  const html = await page.evaluate(() => document.documentElement.outerHTML);
  return { data: { html, finalUrl: page.url(), status: doc ? doc.status() : null,
                   contentType: doc ? (doc.headers()['content-type'] || '') : '' }, type: 'application/json' };
}
"""


@dataclass(frozen=True)
class LoginSite:
    name: str
    hosts: tuple[str, ...]
    script: str
    user_field: str  # Settings attribute names
    password_field: str


AVEPOINT = LoginSite("AvePoint", ("learn.avepoint.com",), AVEPOINT_LOGIN, "avepoint_user", "avepoint_password")
SECURITI = LoginSite("Securiti", ("helpcenter.securiti.ai", "docs.securiti.ai"), SECURITI_LOGIN, "securiti_user",
                     "securiti_password")
SITES = (AVEPOINT, SECURITI)


@dataclass
class LoginResult:
    ok: bool
    reason: str
    cookies: list[dict] = field(default_factory=list, repr=False)  # session cookies: never printed


def _endpoint(settings: Settings) -> str:
    return settings.browserless_url.rstrip("/") + "/function"


def _auth(settings: Settings) -> dict:
    # The token goes in a header, never the URL, so it can't reach a log line.
    return {"Authorization": f"Bearer {settings.browserless_token.get_secret_value()}"}


async def browser_login(client: httpx.AsyncClient, settings: Settings, site: LoginSite) -> LoginResult:
    user = getattr(settings, site.user_field).get_secret_value()
    password = getattr(settings, site.password_field).get_secret_value()
    if not (user and password):
        return LoginResult(False, "no_credentials", [])
    payload = {"code": site.script, "context": {"user": user, "pw": password, "ua": settings.browser_user_agent}}
    try:
        resp = await client.post(_endpoint(settings), json=payload, headers=_auth(settings),
                                 timeout=httpx.Timeout(LOGIN_TIMEOUT, connect=10.0))
    except Exception as exc:  # the message could carry request details; only the type is kept
        return LoginResult(False, f"browserless_{type(exc).__name__}", [])
    if resp.status_code != 200:
        return LoginResult(False, f"browserless_http_{resp.status_code}", [])
    try:
        body = resp.json()
        data = body.get("data", body)
        ok, step, cookies = bool(data["ok"]), str(data.get("step") or ""), data.get("cookies") or []
        if not isinstance(cookies, list):
            raise TypeError
    except (ValueError, KeyError, TypeError, AttributeError):
        return LoginResult(False, "browserless_bad_payload", [])
    if not ok:
        return LoginResult(False, f"stopped at {step[:40]}", [])
    if not cookies:
        return LoginResult(False, "no_cookies", [])
    return LoginResult(True, "", cookies)


class SessionFetcher:
    """Reads each page in its own short Browserless session with the signed-in cookies, so no session runs into
    Browserless' time limit. Same FetchResult as Fetcher: a login redirect comes back "blocked"."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings, cookies: list[dict], *,
                 hosts: tuple[str, ...], pause: float = 2.0):
        self._client, self._settings, self._cookies, self._pause = client, settings, cookies, pause
        self._hosts = hosts  # the vendor's sites: a page that ends anywhere else (a sign-in, the app) wasn't read
        self._sem = asyncio.Semaphore(BROWSER_CONCURRENCY)  # Browserless is shared with DocExtractor
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _paced(self) -> None:
        async with self._lock:
            wait = self._last + self._pause - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def fetch(self, url: str) -> FetchResult:
        payload = {"code": READ_PAGE, "context": {"url": url, "cookies": self._cookies,
                                                  "ua": self._settings.browser_user_agent}}
        async with self._sem:
            await self._paced()
            try:
                resp = await self._client.post(_endpoint(self._settings), json=payload, headers=_auth(self._settings),
                                               timeout=httpx.Timeout(BROWSER_TIMEOUT, connect=10.0))
            except Exception as exc:
                return FetchResult(url, url, None, "", "", 0, "error", f"browserless_{type(exc).__name__}")
        if resp.status_code != 200:
            return FetchResult(url, url, None, "", "", 0, "error", f"browserless_http_{resp.status_code}")
        result = parse_browser_reply(url, resp, HostPolicy())
        if result.outcome == "ok" and host_of(result.final_url) not in self._hosts:
            result.outcome, result.reason = "blocked", "left_site"
        return result
