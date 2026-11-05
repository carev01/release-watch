"""Review queue, "needs a look" and event pages. LAN-only, no auth (spec §12)."""

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text

from release_watch.models import LABEL, SOURCE_STATE, CatalogProduct, Digest, EntryEvent, NewsSource, WatchedSource
from release_watch.notify.digest import preview_digest
from release_watch.watch.successor import watch_successor
from release_watch.web.cards import card_html, markdown_html

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["card_html"] = card_html
templates.env.filters["markdown"] = markdown_html
templates.env.filters["web_url"] = lambda u: u if isinstance(u, str) and u.startswith(("http://", "https://")) else None

# Why an item is in "needs a look", in words. {version} is the event's version.
REASON_TEXT = {
    "too_many_new": "More than 3 new entries appeared on this page in one day. That usually means the page was "
                    "reorganised rather than that several releases shipped, so nothing was sent.",
    "edit_too_large": "More than half of this entry changed at once. That is often a page redesign rather than a "
                      "real update, so it was held. The diff shows exactly what changed.",
    "child_fetch_failed": "A new release page was listed, but it could not be read, so there is no text to send.",
    "no_version": "A new item appeared on a version list, but no version number could be read from it.",
    "successor_redirected": "The expected new URL redirects to another page, so it may not be the {version} "
                            "release notes.",
    "successor_not_found": "The expected new URL shows \"not found\" wording.",
    "successor_too_short": "The expected new URL has almost no text.",
    "successor_no_version": "The expected new URL doesn't mention {version}.",
    "successor_fetch_failed": "The expected new URL could not be read.",
    "successor_url_taken": "The expected new URL is already in the source list in a state that blocks it "
                           "(for example rejected).",
    "successor_unverified": "The expected new URL failed one of the checks (it redirected, or its text looked "
                            "too different from the old page).",
}

NEW_STATE = {"confirm": "active", "reject": "rejected", "reset": "candidate"}
EVENT_DECISION = {"send": "queued", "dismiss": "dismissed"}


def _safe_back(back: str, default: str) -> str:
    """Only same-site paths, so the form can't be used as an open redirect."""
    if back.startswith("/") and not back.startswith(("//", "/\\")) and "\\" not in back:
        return back
    return default


def create_app(session_factory, web_base_url: str = "") -> FastAPI:
    app = FastAPI(title="Release Watch")

    def needs_look_count() -> int:
        with session_factory() as s:
            return s.scalar(select(func.count()).select_from(EntryEvent).where(EntryEvent.status == "needs_look"))

    templates.env.globals["needs_look_count"] = needs_look_count

    @app.get("/healthz")
    def healthz():
        try:
            with session_factory() as s:
                s.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "error", "db": "error"}, status_code=503)
        return {"status": "ok", "db": "ok"}

    @app.get("/")
    def queue(request: Request, state: str = "candidate", label: str | None = None, vendor: str | None = None):
        if state not in SOURCE_STATE.enums or (label and label not in LABEL.enums):
            raise HTTPException(400, "unknown state or label")
        with session_factory() as s:
            wq = select(WatchedSource).where(WatchedSource.state == state)
            nq = select(NewsSource).where(NewsSource.state == state)
            if vendor:
                wq, nq = wq.where(WatchedSource.vendor_name == vendor), nq.where(NewsSource.vendor_name == vendor)
            if label:
                wq = wq.where(WatchedSource.label == label)
            watched = list(s.scalars(wq.order_by(WatchedSource.vendor_name, WatchedSource.title)))
            news = [] if label else list(s.scalars(nq.order_by(NewsSource.vendor_name, NewsSource.url)))
            vendors = sorted(set(s.scalars(select(WatchedSource.vendor_name).distinct()))
                             | set(s.scalars(select(NewsSource.vendor_name).distinct())))
        groups: dict[str, dict[str, list]] = defaultdict(lambda: {"watched": [], "news": []})
        for row in watched:
            groups[row.vendor_name]["watched"].append(row)
        for row in news:
            groups[row.vendor_name]["news"].append(row)
        labels = Counter(row.label or "unlabelled" for row in watched)
        return templates.TemplateResponse(request, "queue.html", {
            "groups": dict(sorted(groups.items())), "state": state, "label": label, "vendor": vendor,
            "vendors": vendors, "watched_count": len(watched), "news_count": len(news),
            "labels": labels.most_common(), "back": str(request.url.path)
            + (f"?{request.url.query}" if request.url.query else ""),
        })

    @app.post("/decide")
    def decide(ids: Annotated[list[uuid.UUID], Form()], table: Annotated[Literal["watched", "news"], Form()],
               decision: Annotated[Literal["confirm", "reject", "reset"], Form()],
               back: Annotated[str, Form()] = "/"):
        model = WatchedSource if table == "watched" else NewsSource
        now = datetime.now(UTC)
        with session_factory() as s:
            for row in s.scalars(select(model).where(model.id.in_(ids))):
                row.state = NEW_STATE[decision]
                row.decided_at = None if decision == "reset" else now
                row.confirmed_by = None if decision == "reset" else "owner"
            s.commit()
        return RedirectResponse(_safe_back(back, "/"), status_code=303)

    @app.get("/needs-look")
    def needs_look(request: Request):
        with session_factory() as s:
            rows = s.execute(select(EntryEvent, WatchedSource).join(WatchedSource, WatchedSource.id == EntryEvent.source_id)
                             .where(EntryEvent.status == "needs_look").order_by(EntryEvent.created_at)).all()
        now = datetime.now(UTC)
        items = [{"event": ev, "source": src, "age_days": (now - ev.created_at).days,
                  "pin": ((src.follow or {}).get("pin") or {}),
                  "why": REASON_TEXT.get(ev.reason or "", ev.reason or "").format(version=ev.version or "")}
                 for ev, src in rows]
        return templates.TemplateResponse(request, "needs_look.html", {"items": items})

    @app.post("/events/{event_id}/watch-new")
    def watch_new(event_id: uuid.UUID, url: Annotated[str, Form()]):
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "the new page needs an http(s) URL")
        with session_factory() as s:
            ev = s.get(EntryEvent, event_id)
            if ev is None or ev.kind != "moved" or ev.status != "needs_look":
                raise HTTPException(404, "no such moved item waiting")
            watch_successor(s, ev, url, datetime.now(UTC))
            s.commit()
        return RedirectResponse("/needs-look", status_code=303)

    @app.post("/events/decide")
    def decide_events(ids: Annotated[list[uuid.UUID], Form()],
                      decision: Annotated[Literal["send", "dismiss"], Form()],
                      back: Annotated[str, Form()] = "/needs-look"):
        now = datetime.now(UTC)
        with session_factory() as s:
            for ev in s.scalars(select(EntryEvent).where(EntryEvent.id.in_(ids), EntryEvent.status == "needs_look")):
                ev.status, ev.decided_at = EVENT_DECISION[decision], now
            s.commit()
        return RedirectResponse(_safe_back(back, "/needs-look"), status_code=303)

    @app.get("/notifications")
    def notifications(request: Request):
        now = datetime.now(UTC)
        upcoming = preview_digest(session_factory, web_base_url=web_base_url, now=now)
        with session_factory() as s:
            past = list(s.scalars(select(Digest).order_by(Digest.created_at.desc()).limit(14)))
        return templates.TemplateResponse(request, "notifications.html", {"upcoming": upcoming, "past": past})

    @app.get("/events/{event_id}")
    def event_page(request: Request, event_id: uuid.UUID):
        with session_factory() as s:
            ev = s.get(EntryEvent, event_id)
            if ev is None:
                raise HTTPException(404, "no such event")
            src = s.get(WatchedSource, ev.source_id)
            product = s.get(CatalogProduct, src.product_de_id) if src and src.product_de_id else None
        return templates.TemplateResponse(request, "event.html", {"ev": ev, "source": src, "product": product})

    return app
