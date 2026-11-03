"""Review queue: the owner confirms or rejects candidate sources. LAN-only, no auth (spec §12)."""

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text

from release_watch.models import NewsSource, WatchedSource

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

NEW_STATE = {"confirm": "active", "reject": "rejected", "reset": "candidate"}


def create_app(session_factory) -> FastAPI:
    app = FastAPI(title="Release Watch")

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
        # Only same-site paths, so the form can't be used as an open redirect.
        target = back if back.startswith("/") and not back.startswith("//") else "/"
        return RedirectResponse(target, status_code=303)

    return app
