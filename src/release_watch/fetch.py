"""Polite async fetching of vendor pages, with soft-404 and login-wall detection (briefing: vendor quirks)."""

import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

import httpx

from release_watch.config import Settings
from release_watch.urls import host_of

ATTEMPTS = 3
RETRY_STATUSES = {429, 503}
SHORT_PAGE_BYTES = 1000
MAX_RETRY_AFTER = 120
MAX_BODY_BYTES = 15_000_000
LOGIN_PATTERN = re.compile(r"(^|[./_-])(login|signin|sign-in|sso|auth|oauth)([./_?=-]|$)|/account/", re.I)

BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

Outcome = Literal["ok", "blocked", "gone", "error"]


@dataclass(frozen=True)
class HostPolicy:
    user_agent: str | None = None
    max_concurrency: int = 2
    min_delay_ms: int = 1000
    soft_404_markers: tuple[str, ...] = ()


# Matched by host suffix.
DEFAULT_HOST_POLICIES: dict[str, HostPolicy] = {
    "ibm.com": HostPolicy(user_agent="curl/8.5.0"),
    "cohesity.com": HostPolicy(soft_404_markers=("ERROR_FALLBACK;404",)),
}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    content_type: str
    text: str
    bytes: int
    outcome: Outcome
    reason: str


def policy_for(host: str, policies: dict[str, HostPolicy], default: HostPolicy | None = None) -> HostPolicy:
    for suffix, policy in policies.items():
        if host == suffix or host.endswith("." + suffix):
            return policy
    return default or HostPolicy()


def _looks_like_login(url: str) -> bool:
    return bool(LOGIN_PATTERN.search(host_of(url)) or LOGIN_PATTERN.search(httpx.URL(url).path))


def classify_response(status: int | None, url: str, final_url: str, body: str,
                      policy: HostPolicy) -> tuple[Outcome, str]:
    if status in (404, 410):
        return "gone", f"http_{status}"
    if status in (401, 403):
        return "blocked", "forbidden"
    if status is None or not 200 <= status < 300:
        return "error", f"http_{status}"
    if any(marker in body for marker in policy.soft_404_markers):
        return "gone", "soft_404"
    if _looks_like_login(final_url) and not _looks_like_login(url):
        return "blocked", "login_redirect"
    if len(body.encode()) < SHORT_PAGE_BYTES and "redirecting" in body.lower():
        return "blocked", "session_redirect"
    return "ok", ""


class Fetcher:
    def __init__(self, settings: Settings, policies: dict[str, HostPolicy] = DEFAULT_HOST_POLICIES,
                 transport: httpx.AsyncBaseTransport | None = None, default_policy: HostPolicy | None = None):
        self._settings = settings
        self._policies = policies
        self._default_policy = default_policy
        # One client for the run: its cookie jar keeps cookies set mid-redirect (ConnectWise guest auth).
        self._client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0),
                                         transport=transport)
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_start: dict[str, float] = {}

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _pace(self, host: str, policy: HostPolicy) -> None:
        async with self._host_locks[host]:
            wait = self._last_start.get(host, 0.0) + policy.min_delay_ms / 1000 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_start[host] = time.monotonic()

    async def fetch(self, url: str) -> FetchResult:
        host = host_of(url)
        policy = policy_for(host, self._policies, self._default_policy)
        sem = self._semaphores.setdefault(host, asyncio.Semaphore(policy.max_concurrency))
        headers = {"User-Agent": policy.user_agent or self._settings.user_agent,
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        if policy.user_agent is None:
            # Some WAFs (Red Hat docs, Cohesity and Commvault IR) 403 a browser UA without the rest of the headers.
            headers.update(BROWSER_HEADERS)
        async with sem:
            for attempt in range(ATTEMPTS):
                await self._pace(host, policy)
                try:
                    resp = await self._client.get(url, headers=headers)
                except httpx.TransportError as exc:
                    if attempt == ATTEMPTS - 1:
                        return FetchResult(url, url, None, "", "", 0, "error", type(exc).__name__)
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status_code in RETRY_STATUSES and attempt < ATTEMPTS - 1:
                    await asyncio.sleep(_retry_after(resp, attempt))
                    continue
                if len(resp.content) > MAX_BODY_BYTES:
                    return FetchResult(url, str(resp.url), resp.status_code, resp.headers.get("content-type", ""),
                                       "", len(resp.content), "error", "too_large")
                body = resp.text
                outcome, reason = classify_response(resp.status_code, url, str(resp.url), body, policy)
                return FetchResult(url, str(resp.url), resp.status_code, resp.headers.get("content-type", ""),
                                   body, len(resp.content), outcome, reason)
        raise AssertionError("unreachable")


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After", "")), 1.0), MAX_RETRY_AFTER)
    except ValueError:
        return float(2 ** (attempt + 1))
