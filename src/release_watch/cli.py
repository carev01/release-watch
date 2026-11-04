"""Command line for the cron jobs and the web app."""

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from release_watch.catalog import sync_catalog
from release_watch.config import Settings, get_settings
from release_watch.db import make_engine, make_session_factory
from release_watch.discovery.classify import label_pending
from release_watch.discovery.heuristic import propose_candidates, upsert_proposals
from release_watch.discovery.seeds import load_seeds
from release_watch.docextractor import DocExtractorClient
from release_watch.fetch import Fetcher
from release_watch.jobs import job_run
from release_watch.llm import LLMClient
from release_watch.notify.digest import run_digest
from release_watch.notify.teams import Teams
from release_watch.watch.run import run_watch

app = typer.Typer(no_args_is_help=True, add_completion=False)

ROOT = Path(__file__).resolve().parents[2]


def make_docextractor_client(settings: Settings) -> DocExtractorClient:
    return DocExtractorClient(settings.docextractor_base_url, settings.docextractor_api_key.get_secret_value())


def make_fetcher(settings: Settings) -> Fetcher:
    return Fetcher(settings)


def make_llm(settings: Settings) -> LLMClient:
    return LLMClient(settings)


def make_teams(settings: Settings) -> Teams | None:
    url = settings.teams_webhook_url.get_secret_value()
    return Teams(url) if url else None


def _session_factory(settings: Settings):
    return make_session_factory(make_engine(settings.database_url))


def _run(job: str, body) -> None:
    """Run an async job body inside job_run; on failure print a one-line error and exit 1."""
    settings = get_settings()
    session_factory = _session_factory(settings)
    try:
        with job_run(session_factory, job) as counts:
            counts.update(asyncio.run(body(settings, session_factory)))
    except Exception as exc:
        typer.echo(f"{job} failed: {type(exc).__name__}: {exc}", err=True)
        _alert_failure(settings, session_factory, job, exc)
        raise typer.Exit(1)
    typer.echo(json.dumps(counts, default=str))


def _alert_failure(settings: Settings, session_factory, job: str, exc: Exception) -> None:
    """Immediate message for a job that failed outright (spec 9): to Teams, or kept for the web UI while no
    webhook is set. Never masks the original failure."""
    from release_watch.models import Digest
    from release_watch.notify.cards import text_card

    card = text_card(f"Release Watch job {job} failed: {type(exc).__name__}")
    teams = make_teams(settings)
    try:
        if teams is not None:
            asyncio.run(teams.post(card))
        with session_factory() as s:
            s.add(Digest(channel="teams" if teams else "web", cards=[card]))
            s.commit()
    except Exception as alert_exc:
        typer.echo(f"failure alert not sent: {type(alert_exc).__name__}: {alert_exc}", err=True)


async def _sync(settings, session_factory) -> dict:
    async with make_docextractor_client(settings) as client:
        with session_factory() as s:
            return await sync_catalog(client, s, settings.excluded_products, datetime.now(UTC))


@app.command()
def sync() -> None:
    """Mirror DocExtractor's vendors, products and sources."""
    _run("sync", _sync)


@app.command()
def discover(relabel: bool = typer.Option(False, help="Re-label every candidate, not just new ones.")) -> None:
    """Sync the catalog, propose release-notes candidates, and label them by fetching them live."""

    async def body(settings, session_factory) -> dict:
        counts = {"sync": await _sync(settings, session_factory)}
        async with make_docextractor_client(settings) as client:
            with session_factory() as s:
                proposals, counts["toc_errors"] = await propose_candidates(client, s)
                counts["proposals"] = upsert_proposals(s, proposals)
        async with make_fetcher(settings) as fetcher:
            counts["labels"] = await label_pending(session_factory, fetcher, make_llm(settings), relabel=relabel)
        return counts

    _run("discover", body)


@app.command()
def watch(only: str = typer.Option(None, help="Only sources whose URL or vendor contains this text."),
          dry_run: bool = typer.Option(False, "--dry-run", help="Read and print each source; write nothing.")) -> None:
    """Check every active source for new and edited release-notes entries."""

    async def body(settings, session_factory) -> dict:
        async with make_fetcher(settings) as fetcher:
            return await run_watch(session_factory, fetcher, make_llm(settings), only=only, dry_run=dry_run,
                                   confirm_delay=settings.confirm_delay_seconds)

    if dry_run:
        settings = get_settings()
        counts = asyncio.run(body(settings, _session_factory(settings)))
        typer.echo(json.dumps(counts), err=True)
        return
    _run("watch", body)


@app.command()
def digest(dry_run: bool = typer.Option(False, "--dry-run", help="Print the cards as JSON; send nothing.")) -> None:
    """Send the daily digest (new versions, new and edited entries, health) to Teams, or keep it for the
    web UI's Notifications page while no webhook is set."""

    async def body(settings, session_factory) -> dict:
        return await run_digest(session_factory, None if dry_run else make_teams(settings),
                                web_base_url=settings.web_base_url, now=datetime.now(UTC), dry_run=dry_run)

    if dry_run:
        settings = get_settings()
        typer.echo(json.dumps(asyncio.run(body(settings, _session_factory(settings)))), err=True)
        return
    _run("digest", body)


@app.command()
def seed(path: Path) -> None:
    """Load hand-researched sources from a YAML file."""
    settings = get_settings()
    with _session_factory(settings)() as s:
        try:
            counts = load_seeds(s, path)
        except ValueError as exc:
            typer.echo(f"seed failed: {exc}", err=True)
            raise typer.Exit(1)
    typer.echo(json.dumps(counts))


@app.command()
def web(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Serve the review queue."""
    import uvicorn

    from release_watch.web.app import create_app

    settings = get_settings()
    uvicorn.run(create_app(_session_factory(settings), web_base_url=settings.web_base_url), host=host, port=port)


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
