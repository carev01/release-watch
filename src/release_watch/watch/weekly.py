"""The weekly login visit (spec 2026-11-05): per vendor, one login, then its login-walled sources through the
watch pipeline with the signed-in cookies. A failed login or a crash alerts once and never stops the next vendor."""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from release_watch.models import WatchedSource
from release_watch.urls import host_of
from release_watch.watch.login import SITES, LoginResult, LoginSite
from release_watch.watch.run import run_watch, utcnow


def _source_count(session_factory, site: LoginSite) -> int:
    with session_factory() as s:
        urls = s.scalars(select(WatchedSource.url).where(WatchedSource.state == "active",
                                                         WatchedSource.login_walled.is_(True)))
        return sum(1 for url in urls if host_of(url) in site.hosts)


async def run_login_visits(session_factory, llm, *, login: Callable[[LoginSite], Awaitable[LoginResult]],
                           fetcher_for: Callable[[list[dict]], object], alert: Callable[[str], Awaitable[None]],
                           confirm_delay: float, sites=SITES, now=utcnow, sleep=asyncio.sleep) -> dict:
    vendors: dict[str, dict] = {}
    for site in sites:
        n = _source_count(session_factory, site)
        result = await login(site)
        if not result.ok:
            vendors[site.name] = {"login": "failed", "reason": result.reason, "sources": n, "read": 0}
            await alert(f"Release Watch: the weekly login to {site.name} failed ({result.reason}). "
                        f"Its {n} pages were not read this week.")
            continue
        try:
            c = await run_watch(session_factory, fetcher_for(result.cookies), llm, confirm_delay=confirm_delay,
                                now=now, sleep=sleep, login_hosts=site.hosts, same_visit_edits=True)
        except Exception as exc:  # one vendor's crash must not stop the next
            reason = f"crash: {type(exc).__name__}"
            vendors[site.name] = {"login": "ok", "reason": reason, "sources": n, "read": 0}
            await alert(f"Release Watch: the weekly visit to {site.name} stopped ({reason}).")
            continue
        vendors[site.name] = {"login": "ok", "sources": c["sources"],
                              "read": c["baseline"] + c["same"] + c["changed"], **c}
    return {"vendors": vendors}
