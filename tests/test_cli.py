import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from release_watch import cli
from release_watch.models import JobRun, WatchedSource
from tests.fakes import FakeDocExtractor


class NoFetcher:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def fetch(self, url):
        raise AssertionError("no fetch expected")


@pytest.fixture
def env(session_factory, db_url, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # keep the real .secrets out of tests
    monkeypatch.setenv("RW_DATABASE_URL", db_url)
    monkeypatch.setenv("RW_DOCEXTRACTOR_API_KEY", "dxk_secret")
    monkeypatch.setattr(cli, "make_fetcher", lambda settings: NoFetcher())
    monkeypatch.setattr(cli, "make_llm", lambda settings: None)
    return session_factory


def jobs(session_factory):
    with session_factory() as s:
        return list(s.scalars(select(JobRun)))


def test_sync_command_writes_job_run(env, monkeypatch):
    monkeypatch.setattr(cli, "make_docextractor_client", lambda settings: FakeDocExtractor())
    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    [run] = jobs(env)
    assert (run.job, run.status, run.counts["vendors"]) == ("sync", "ok", 3)


def test_discover_failure_exit_code(env, monkeypatch):
    monkeypatch.setattr(cli, "make_docextractor_client", lambda settings: FakeDocExtractor(fail_on="products"))
    result = CliRunner().invoke(cli.app, ["discover"])
    assert result.exit_code == 1
    [run] = jobs(env)
    assert (run.job, run.status) == ("discover", "failed")
    assert "dxk_secret" not in result.output


def test_discover_proposes_and_records_stage_counts(env, monkeypatch):
    monkeypatch.setattr(cli, "make_docextractor_client", lambda settings: FakeDocExtractor())

    async def no_labels(session_factory, fetcher, llm, relabel=False):
        return {}

    monkeypatch.setattr(cli, "label_pending", no_labels)
    result = CliRunner().invoke(cli.app, ["discover"])
    assert result.exit_code == 0, result.output
    [run] = jobs(env)
    assert run.counts["sync"]["sources"] == 5
    assert run.counts["proposals"]["new"] >= 1
    with env() as s:
        assert "Zerto Release Notes" in {r.title for r in s.scalars(select(WatchedSource))}


def test_seed_command(env, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "make_docextractor_client", lambda settings: FakeDocExtractor())
    CliRunner().invoke(cli.app, ["sync"])
    path = tmp_path / "s.yaml"
    path.write_text("news_sources:\n  - {vendor: Zerto, url: 'https://zerto.com/blog', kind: listing}\n")
    result = CliRunner().invoke(cli.app, ["seed", str(path)])
    assert result.exit_code == 0, result.output
    assert "news_new" in result.output


def test_watch_command_writes_job_run(env):
    result = CliRunner().invoke(cli.app, ["watch"])
    assert result.exit_code == 0, result.output
    [run] = jobs(env)
    assert (run.job, run.status, run.counts["sources"]) == ("watch", "ok", 0)


def test_partial_watch_is_recorded_apart_from_the_daily_watch(env):
    # Health reads "the last watch" as the daily run; a hand-run --only check must not take its place.
    result = CliRunner().invoke(cli.app, ["watch", "--only", "example.com"])
    assert result.exit_code == 0, result.output
    [run] = jobs(env)
    assert run.job == "watch-only"


def test_watch_login_records_its_own_job(env, monkeypatch):
    async def visits(*args, **kw):
        return {"vendors": {}}

    monkeypatch.setattr(cli, "run_login_visits", visits)
    result = CliRunner().invoke(cli.app, ["watch-login"])
    assert result.exit_code == 0, result.output
    [run] = jobs(env)
    assert (run.job, run.status) == ("watch-login", "ok")


class RecordingTeams:
    def __init__(self):
        self.cards = []

    async def post(self, card):
        self.cards.append(card)


def test_job_failure_posts_immediate_alert(env, monkeypatch):
    teams = RecordingTeams()
    monkeypatch.setattr(cli, "make_teams", lambda settings: teams)
    monkeypatch.setattr(cli, "make_docextractor_client", lambda settings: FakeDocExtractor(fail_on="products"))
    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == 1
    [card] = teams.cards
    assert card["body"][0]["text"].startswith("Release Watch job sync failed")


def test_digest_without_webhook_goes_to_the_web_ui(env):
    result = CliRunner().invoke(cli.app, ["digest"])
    assert result.exit_code == 0, result.output
    assert '"channel": "web"' in result.output
    [run] = jobs(env)
    assert (run.job, run.status) == ("digest", "ok")


def test_failure_alert_without_webhook_is_kept_for_the_web_ui(env, monkeypatch):
    from release_watch.models import Digest

    monkeypatch.setattr(cli, "make_docextractor_client", lambda settings: FakeDocExtractor(fail_on="products"))
    assert CliRunner().invoke(cli.app, ["sync"]).exit_code == 1
    with env() as s:
        [d] = s.scalars(select(Digest)).all()
    assert d.channel == "web" and d.cards[0]["body"][0]["text"].startswith("Release Watch job sync failed")
