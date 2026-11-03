# Release Watch Phase 1 — Catalog and Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deployed Release Watch that mirrors DocExtractor's catalog daily, proposes release-notes sources and vendor news sources, classifies them by fetching them live, and lets the owner confirm or reject each one in a web review queue. Phase 1 ends with that list confirmed.

**Architecture:** One Python package (`release_watch`) with a CLI for the cron jobs (`sync`, `discover`, `seed`) and a FastAPI web app for the review queue. PostgreSQL holds the catalog mirror, candidate sources and job runs. Database code is synchronous SQLAlchemy. Network I/O to vendor sites, DocExtractor and the LLM is async httpx, so per-host concurrency is easy to bound. Deployed with a Helm chart to the `release-watch` namespace, mirroring DocExtractor's CI → ghcr → `helm upgrade` flow.

**Tech Stack:** Python 3.13 (matches DocExtractor's image), uv, httpx, SQLAlchemy 2 + psycopg 3, Alembic, pydantic-settings, FastAPI + Jinja2, trafilatura, selectolax, feedparser, PyYAML, Typer, pytest + respx, Postgres 16.

**Spec:** `docs/superpowers/specs/2026-11-03-release-watch-design.md` (sections 4–6, 10–14 apply to this phase). Background: `docs/reference/docextractor-briefing.md`.

## Global Constraints

- DocExtractor is called with **GET only**, header `X-API-Key`, `limit=200` per page, **at most 4 requests per second**. Base URL is the in-cluster URL `http://docextractor-backend.docextractor.svc:8000/api` when deployed, and `http://docextractor.k3s.home.lan/api` from the build machine.
- Never log or print a secret: DocExtractor key, LLM keys, webhook URL, vendor credentials. `.secrets` is git-ignored **before** the first commit.
- Vendor sites: **one fetch per URL per run**, at most **2 concurrent requests per host**, at least **1000 ms between requests to the same host**, a real Chrome User-Agent by default, exponential backoff on 429 (honour `Retry-After`), and no more than 3 attempts.
- Host overrides from the briefing apply: IBM docs (`ibm.com`) uses User-Agent `curl/8.5.0`; Cohesity soft-404 marker `ERROR_FALLBACK;404`; AvePoint expired-session pages (~400 bytes, "Redirecting") count as login walls.
- Excluded products (spec §4): Veeam Backup & Replication, Veeam Backup for Microsoft 365, Veeam Backup for Salesforce, Veeam ONE, Veeam Recovery Orchestrator, Veeam Service Provider Console. **Watched:** Veeam Kasten for Kubernetes, Veeam Data Cloud, Securiti.ai (all under vendor "Veeam" in DocExtractor).
- Login policy: Rubrik and Cohesity are never logged in to. AvePoint and Eon logins are phase 2, so they are not fetched behind login in phase 1.
- LLM: Azure OpenAI `gpt-5-mini` first, OpenRouter fallback. LLM output never decides alone. In phase 1 it only labels candidates for the owner's review.
- Settings are read from `RW_*` environment variables. For local runs, `.secrets` is loaded with names mapped as `DOC-EXTRACTOR` → `docextractor_api_key`, `OPENROUTER` → `openrouter_api_key`, `AZURE-OPENAI-KEY` → `azure_openai_api_key`, `AZURE-OPENAI-ENDPOINT` → `azure_openai_endpoint`.
- Shared Firecrawl and Browserless are **not** used in phase 1. A page that won't fetch directly is labelled `unsure` with reason `fetch_failed`, and phase 2 handles it.

## Review Focus

1. **A DocExtractor call fails mid-sync** (timeout or 5xx on page 3 of products). The sync must abort the whole run and mark nothing removed, rather than deactivate everything after the failure point. Pinned in Task 4.
2. **The same page under a slightly different URL** (trailing slash, `#page=12` anchor, signed query tokens, `http` vs `https`) must not become a second candidate, and must not resurrect a rejected one. Pinned in Task 5 (`normalize_url`) and Task 6.
3. **A page that moved on a version bump** (same `article_id`, new `source_url`) updates the existing candidate's URL instead of proposing a new one. Pinned in Task 6.
4. **The LLM is down or returns malformed JSON.** Azure fails → OpenRouter. Both fail → the candidate is `unsure` with reason `llm_failed`, and the run continues. Pinned in Tasks 7 and 9.
5. **A live fetch hits a soft 404 or login wall** (Cohesity's HTTP 200 shell with `ERROR_FALLBACK;404`, a redirect to a login host, a ~400-byte "Redirecting…" page). It is labelled `blocked`, never "not release notes". Pinned in Task 8.

---

## File structure

```
.gitignore  .dockerignore  pyproject.toml  uv.lock  Dockerfile  README.md
alembic.ini
alembic/env.py  alembic/versions/0001_initial.py
src/release_watch/
  __init__.py
  config.py                 # Settings + .secrets loader
  db.py                     # engine/session factory, Base
  models.py                 # all phase-1 tables
  jobs.py                   # job_run context manager
  urls.py                   # normalize_url, host_of
  docextractor.py           # read-only API client
  catalog.py                # catalog sync
  discovery/__init__.py
  discovery/heuristic.py    # propose candidates from the catalog
  discovery/classify.py     # live fetch + LLM label
  discovery/seeds.py        # load hand-researched sources from YAML
  llm.py                    # Azure → OpenRouter JSON completion
  fetch.py                  # polite async fetcher + block detection
  web/__init__.py  web/app.py
  web/templates/base.html  web/templates/queue.html
  cli.py                    # typer app: sync, discover, seed, web
seeds/sources.yaml
tests/conftest.py  tests/test_*.py  tests/fixtures/*
deploy/helm/release-watch/{Chart.yaml,values.yaml,values-homelab.yaml,templates/*}
.github/workflows/ci.yml
```

---

### Task 1: Repository scaffold and settings

**Files:**
- Create: `.gitignore`, `.dockerignore`, `pyproject.toml`, `src/release_watch/__init__.py`, `src/release_watch/config.py`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `class Settings(BaseSettings)` (env prefix `RW_`) with fields `database_url: str`, `docextractor_base_url: str = "http://docextractor.k3s.home.lan/api"`, `docextractor_api_key: SecretStr`, `docextractor_ui_url: str = "http://docextractor.k3s.home.lan"`, `azure_openai_endpoint: str = ""`, `azure_openai_api_key: SecretStr = SecretStr("")`, `azure_openai_model: str = "gpt-5-mini"`, `openrouter_api_key: SecretStr = SecretStr("")`, `openrouter_model: str = "openai/gpt-5-mini"`, `excluded_products: list[str]` (default = the six names in Global Constraints), `user_agent: str` (a current desktop Chrome UA string). Also `load_secrets_file(path: Path) -> dict[str, str]` and `get_settings() -> Settings`. `get_settings` reads `.secrets` from the CWD if present, maps names per Global Constraints, and gives env vars precedence.

- [ ] **Step 1:** `git init`. Write `.gitignore` containing `.secrets`, `.env`, `__pycache__/`, `.venv/`, `*.pyc`, `.pytest_cache/`. Run `git check-ignore .secrets`. Expected output: `.secrets`.
- [ ] **Step 2:** `pyproject.toml` with `requires-python = ">=3.13,<3.14"`, the runtime deps from Tech Stack, dev deps `pytest`, `respx`, script `release-watch = "release_watch.cli:app"`, src layout, `[tool.pytest.ini_options] testpaths=["tests"]`. Run `uv sync --python 3.13`.
- [ ] **Step 3: Write failing tests** in `tests/test_config.py`:
  - `test_secrets_file_maps_names`: a tmp file with `DOC-EXTRACTOR=dxk_x\nOPENROUTER=sk-or\n# comment\n\nAZURE-OPENAI-KEY=az\nAZURE-OPENAI-ENDPOINT=https://e.openai.azure.com/` → `load_secrets_file` returns `{"docextractor_api_key": "dxk_x", "openrouter_api_key": "sk-or", "azure_openai_api_key": "az", "azure_openai_endpoint": "https://e.openai.azure.com/"}`. Unknown names are ignored.
  - `test_env_overrides_secrets_file`: `RW_DOCEXTRACTOR_API_KEY=env` wins over the file.
  - `test_secret_not_in_repr`: `"dxk_x" not in repr(settings)`.
  - `test_default_exclusions`: `"Veeam Kasten for Kubernetes" not in settings.excluded_products` and `len(settings.excluded_products) == 6`.
- [ ] **Step 4:** Run `uv run pytest tests/test_config.py -v`. Expected: FAIL (module missing).
- [ ] **Step 5:** Implement `config.py`.
- [ ] **Step 6:** Run `uv run pytest tests/test_config.py -v`. Expected: 4 passed.
- [ ] **Step 7:** Commit `chore: scaffold release-watch package and settings`. Before committing, run `git status` and confirm `.secrets` is not listed.

### Task 2: Database schema and job runs

**Files:**
- Create: `src/release_watch/db.py`, `src/release_watch/models.py`, `src/release_watch/jobs.py`, `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`, `tests/test_models.py`, `tests/test_jobs.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces, in `db.py`: `Base` (DeclarativeBase), `make_engine(url: str) -> Engine`, `make_session_factory(engine) -> sessionmaker[Session]`.
- Produces tables in `models.py`, all with UUID `id` (except catalog tables, whose PK is the DocExtractor UUID) and `created_at`/`updated_at`:
  - `CatalogVendor(de_id PK, name, website, tier_id, active: bool, synced_at)`
  - `CatalogProduct(de_id PK, vendor_de_id, name, de_version, de_previous_version, excluded: bool, active: bool, synced_at)`
  - `CatalogSource(de_id PK, product_de_id, name, source_type, base_url, url_template, platform, auth_realm_id, status, last_extracted_at, active: bool, synced_at)`
  - `WatchedSource`: `vendor_name`, `product_de_id` (nullable), `de_source_id` (nullable), `de_article_id` (nullable), `url`, `url_key` (unique, from `normalize_url`), `title`, `kind` (enum `entry_page | index_page | feed | section | unknown`), `state` (enum `candidate | active | rejected | gone`), `origin` (enum `heuristic | seed`), `discovery_reason`, `label` (enum `release_notes | not_release_notes | unsure | blocked`, nullable), `label_reason`, `label_evidence`, `labelled_at`, `login_walled: bool`, `selector` (nullable), `split_rule` (JSONB, nullable), `confirmed_by`, `decided_at`. Partial unique index on `de_article_id` where not null.
  - `NewsSource`: `vendor_name`, `product_de_id` (nullable), `url`, `url_key` (unique), `kind` (enum `feed | listing | github_releases`), `state` (same enum as above), `origin`, `notes`, `confirmed_by`, `decided_at`
  - `JobRun(job, started_at, finished_at, status (enum running | ok | failed), counts JSONB, error)`
- Produces, in `jobs.py`: `@contextmanager job_run(session_factory, job: str) -> Iterator[dict]`. Inserts a `running` row and yields a counts dict. On normal exit it sets `ok` and the counts. On exception it sets `failed` and the error text (first 2000 chars, secrets never included) and re-raises.

- [ ] **Step 1:** `conftest.py` fixture `db_url`: uses `RW_TEST_DATABASE_URL`, else starts nothing and skips DB tests with a clear message. Locally: `docker run -d --name rw-test-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw_test -p 55432:5432 postgres:16-alpine` and `RW_TEST_DATABASE_URL=postgresql+psycopg://postgres:rw@localhost:55432/rw_test`. Fixture `session_factory` runs `alembic upgrade head` once per session and truncates all tables per test.
- [ ] **Step 2: Write failing tests:**
  - `test_upgrade_creates_tables`: after upgrade, the inspector lists all seven tables.
  - `test_url_key_unique`: inserting two `WatchedSource` rows with the same `url_key` raises `IntegrityError`.
  - `test_job_run_ok_records_counts`: the body sets `counts["vendors"]=43` → the row has `status == "ok"`, `counts == {"vendors": 43}` and `finished_at` set.
  - `test_job_run_failure_recorded_and_reraised`: the body raises `RuntimeError("boom")` → it re-raises, and the row has `status == "failed"` and `"boom" in error`.
- [ ] **Step 3:** Run `uv run pytest tests/test_models.py tests/test_jobs.py -v`. Expected: FAIL.
- [ ] **Step 4:** Implement the models and an Alembic `0001_initial` written by hand (or autogenerated, then reviewed), plus `jobs.py`.
- [ ] **Step 5:** Run the same tests. Expected: 4 passed.
- [ ] **Step 6:** Commit `feat: database schema and job_run tracking`.

### Task 3: DocExtractor read-only client

**Files:**
- Create: `src/release_watch/docextractor.py`, `tests/test_docextractor.py`

**Interfaces:**
- Produces: `class DocExtractorClient` with `__init__(base_url: str, api_key: str, *, max_rps: float = 4.0, transport: httpx.AsyncBaseTransport | None = None)`, async context manager, and async methods:
  - `list_vendors() -> list[dict]`, `list_products() -> list[dict]`, `list_sources() -> list[dict]`: these page with `skip`/`limit=200` until `total` is reached. The list key is `vendors` / `products` / `sources`.
  - `get_toc(source_id: str) -> list[dict]`: returns `entries`.
  - `search_articles(q: str, source_id: str | None = None) -> AsyncIterator[dict]`: follows `next_cursor` while `has_more`, resending `q`.
  - `get_article(article_id: str) -> dict`
- `class DocExtractorError(Exception)`: raised on any non-2xx after retries, or on transport errors.
- Retries: 3 attempts with backoff on 5xx and timeouts. No retry on 4xx.
- The client exposes no method that sends anything but GET. Its internal `_get` is the only request path.

- [ ] **Step 1: Write failing tests** with `respx`:
  - `test_list_products_paginates`: the mock returns `total=250` across two pages (200 + 50) → 250 items, and the second request has `skip=200&limit=200`.
  - `test_api_key_header_sent`: the request carries `X-API-Key: k`.
  - `test_search_follows_cursor`: page 1 has `has_more=true, next_cursor="c1"`, page 2 has `has_more=false` → it yields both pages' items, and the second request has `cursor=c1&q=release+notes`.
  - `test_5xx_retried_then_raises`: three 503s → `DocExtractorError`, after 3 calls.
  - `test_4xx_not_retried`: one 401 → `DocExtractorError` after 1 call, and the message does not contain the key.
- [ ] **Step 2:** Run `uv run pytest tests/test_docextractor.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Rate limiting is a simple async min-interval lock (`1/max_rps` seconds between request starts).
- [ ] **Step 4:** Run the tests. Expected: 5 passed.
- [ ] **Step 5:** Commit `feat: read-only DocExtractor API client`.

### Task 4: Catalog sync

**Files:**
- Create: `src/release_watch/catalog.py`, `tests/test_catalog.py`, `tests/fixtures/catalog_small.json`

**Interfaces:**
- Consumes: `DocExtractorClient` (Task 3), models and `job_run` (Task 2), `Settings.excluded_products`.
- Produces: `async def sync_catalog(client: DocExtractorClient, session: Session, excluded_products: list[str], now: datetime) -> dict[str, int]`. It returns counts `vendors`, `products`, `sources`, `deactivated`, `excluded`. Algorithm:
  1. Fetch **all three lists first**. If any call raises, propagate before touching the DB.
  2. Upsert rows with `active=True` and `synced_at=now`.
  3. Set `active=False` on rows not seen in this sync. Rows are never deleted.
  4. Set `excluded=True` on products whose `name` is in `excluded_products`.
  5. For `WatchedSource` rows with a `de_article_id`: nothing in phase 1. URL refresh by article happens in discovery (Task 6).

- [ ] **Step 1: Write failing tests** using a fake client object that returns fixture lists:
  - `test_sync_inserts_and_counts`: the fixture has 3 vendors, 4 products (one named `Veeam ONE`) and 5 sources → counts `{vendors:3, products:4, sources:5, deactivated:0, excluded:1}`.
  - `test_sync_deactivates_missing`: a second sync without one source → that source has `active=False`, and the count is `deactivated == 1`.
  - `test_sync_failure_changes_nothing`: the fake `list_sources` raises `DocExtractorError` on the second sync → it raises, and every row is still `active=True` with its old `synced_at`.
  - `test_kasten_not_excluded`: product `Veeam Kasten for Kubernetes` has `excluded=False`.
- [ ] **Step 2:** Run `uv run pytest tests/test_catalog.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement with PostgreSQL `INSERT … ON CONFLICT (de_id) DO UPDATE`, in one transaction.
- [ ] **Step 4:** Run the tests. Expected: 4 passed.
- [ ] **Step 5:** Commit `feat: daily catalog sync with soft deactivation and exclusions`.

### Task 5: URL normalisation

**Files:**
- Create: `src/release_watch/urls.py`, `tests/test_urls.py`

**Interfaces:**
- Produces: `normalize_url(url: str) -> str` (the dedupe key) and `host_of(url: str) -> str`.
- Rules for `normalize_url`:
  - lowercase the scheme and host, and map `http` to `https`,
  - drop the fragment (this covers PDF `#page=N` anchors),
  - drop `utm_*`, `st`, `se`, `sig`, `sv`, `sp`, `sr` query params, and keep and sort the rest,
  - strip one trailing `/` unless the path is `/`,
  - drop default ports.

- [ ] **Step 1: Write failing tests**, `test_normalize_url` parametrised:
  - `HTTP://Docs.X.com/a/` → `https://docs.x.com/a`
  - `https://x.com/f.pdf#page=12` → `https://x.com/f.pdf`
  - `https://x.com/p?sig=abc&b=2&a=1&utm_source=z` → `https://x.com/p?a=1&b=2`
  - `https://x.com:443/` → `https://x.com/`
  - `https://keepit.com/updates#pmc` → `https://keepit.com/updates`. Sections are told apart by `kind=section` and `selector`, not by URL key. **Exception:** for `kind == "section"`, callers build the key as `normalize_url(url) + "#" + selector`.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement with `urllib.parse`. **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: URL normalisation for candidate dedupe`.

### Task 6: Discovery heuristic

**Files:**
- Create: `src/release_watch/discovery/__init__.py`, `src/release_watch/discovery/heuristic.py`, `tests/test_heuristic.py`

**Interfaces:**
- Consumes: `DocExtractorClient.get_toc`, `search_articles`, the catalog tables, `normalize_url`.
- Produces:
  - `RELEASE_PATTERN: re.Pattern`: case-insensitive, matches `release notes?`, `what'?s new`, `new features`, `change ?log`, `revision history`, `readme`, `product updates`.
  - `@dataclass Proposal(vendor_name, product_de_id, de_source_id, de_article_id, url, title, kind_hint, reason)`
  - `async def propose_candidates(client, session) -> list[Proposal]`. It reads only active, non-excluded catalog rows. Three passes:
    1. **Source-name pass:** a source whose `name` matches → one proposal at `base_url`, with `kind_hint` = `index_page` for PDF-per-release or TOC-with-children sources, else `entry_page`, and `reason="source_name"`.
    2. **TOC pass:** for each active web source **not** already proposed in pass 1, walk `get_toc`. An entry whose `title` matches is proposed. If it has children, it becomes one `index_page` proposal at its URL, or at its first child's URL if it has no URL itself, and its children are **not** proposed separately.
    3. **Search pass:** `search_articles` for `"release notes"`, `"what's new"` and `"changelog"`. Hits whose title or URL matches `RELEASE_PATTERN` and whose source wasn't covered in passes 1–2 → `entry_page`, `reason="search"`. Hits matching only in the body are dropped, which cuts the "mentions release notes" noise.
  - `def upsert_proposals(session, proposals: list[Proposal]) -> dict[str, int]`: returns `new`, `existing`, `moved`, `skipped_rejected`.
    - Matching is first by `de_article_id`. A match with a different URL updates `url`/`url_key` and counts as `moved`.
    - Otherwise matching is by `url_key`.
    - Existing rows keep their `state`. Rejected rows stay rejected and count as `skipped_rejected`.
    - New rows get `state=candidate` and `origin=heuristic`.
    - Sources whose catalog row has an `auth_realm_id` get `login_walled=True`.

- [ ] **Step 1: Write failing tests** with a fake client and seeded catalog rows:
  - `test_source_name_match_proposes_source_once`: source "Zerto Release Notes" → exactly 1 proposal, with `reason == "source_name"`, and none from TOC or search for that source.
  - `test_toc_branch_collapses_children`: TOC entry "Release Notes" with 5 children → 1 proposal with `kind_hint == "index_page"`.
  - `test_search_body_only_hit_dropped`: a hit titled "Configuring backups" at `/config` → no proposal.
  - `test_excluded_product_skipped`: a source under `Veeam ONE` named "Release Notes" → no proposal.
  - `test_upsert_moved_article_updates_url`: an existing row with `de_article_id=A`, url `/11.44/rn`, and a proposal A at `/11.46/rn` → 1 row with url `/11.46/rn`, `moved == 1`.
  - `test_upsert_rejected_stays_rejected`: a rejected row at `https://x.com/rn`, and a proposal at `http://x.com/rn/#top` → still rejected, `skipped_rejected == 1`, and the row count is unchanged.
  - `test_login_walled_flagged`: a source with `auth_realm_id` set → `login_walled is True`.
- [ ] **Step 2:** Run `uv run pytest tests/test_heuristic.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. TOC walks run sequentially, bounded by the client's rate limit.
- [ ] **Step 4:** Run. Expected: 7 passed.
- [ ] **Step 5:** Commit `feat: release-notes candidate discovery from the catalog`.

### Task 7: LLM client (Azure OpenAI → OpenRouter)

**Files:**
- Create: `src/release_watch/llm.py`, `tests/test_llm.py`

**Interfaces:**
- Produces: `class LLMClient(settings: Settings, transport=None)` with `async def complete_json(system: str, user: str, *, max_output_tokens: int = 800) -> dict`.
  - **Azure call:** POST `{azure_openai_endpoint}/openai/v1/chat/completions` with header `api-key`, body `{"model": azure_openai_model, "messages": [...], "response_format": {"type": "json_object"}, "max_completion_tokens": N}`.
  - **OpenRouter call:** POST `https://openrouter.ai/api/v1/chat/completions` with `Authorization: Bearer`, using the same body shape with `openrouter_model` and `max_tokens`.
  - **Order:** Azure is tried first when configured. If it fails (non-2xx, timeout, or content that isn't a JSON object), OpenRouter is tried. If both fail, it raises `LLMError`.
  - Its logs record the provider and status only, never keys or prompts.
  - `last_provider: str` is set after each success.

- [ ] **Step 1: Write failing tests** with `respx`:
  - `test_azure_success`: Azure returns `{"choices":[{"message":{"content":"{\"a\":1}"}}]}` → `{"a": 1}`, with `last_provider == "azure"`, and OpenRouter is not called.
  - `test_falls_back_on_azure_500`: Azure 500, OpenRouter OK → the result comes back with `last_provider == "openrouter"`.
  - `test_falls_back_on_malformed_json`: Azure content `"not json"` → OpenRouter is used.
  - `test_both_fail_raises`: → `LLMError`.
  - `test_no_azure_config_uses_openrouter`: empty endpoint → only OpenRouter is called.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement. **Step 4:** Run. Expected: 5 passed.
- [ ] **Step 5:** Commit `feat: JSON LLM client with Azure primary and OpenRouter fallback`.

### Task 8: Polite fetcher with block detection

**Files:**
- Create: `src/release_watch/fetch.py`, `tests/test_fetch.py`

**Interfaces:**
- Produces:
  - `@dataclass HostPolicy(user_agent: str | None = None, max_concurrency: int = 2, min_delay_ms: int = 1000, soft_404_markers: tuple[str, ...] = ())`
  - `DEFAULT_HOST_POLICIES: dict[str, HostPolicy]`: a suffix match on host. `ibm.com` uses UA `curl/8.5.0`. `cohesity.com` has markers `("ERROR_FALLBACK;404",)`.
  - `@dataclass FetchResult(url, final_url, status: int | None, content_type: str, text: str, bytes: int, outcome: Literal["ok", "blocked", "gone", "error"], reason: str)`
  - `class Fetcher(settings, policies=DEFAULT_HOST_POLICIES, transport=None)`: async context manager with `async def fetch(url: str) -> FetchResult`. Per-host semaphores and min-delay; 429/503 backoff honouring `Retry-After`, max 3 attempts; follows redirects.
  - `def classify_response(status, final_url, body, policy) -> tuple[outcome, reason]`, a pure function:
    - 404 or 410 → `gone`.
    - 401 or 403 → `blocked/forbidden`.
    - A soft-404 marker in the body → `gone/soft_404`.
    - A final URL host or path that matches a login pattern (`login`, `signin`, `sign-in`, `sso`, `auth`, `oauth`, `/account/`) when the original URL didn't → `blocked/login_redirect`.
    - A body under 1000 bytes containing `redirecting` (case-insensitive) → `blocked/session_redirect`.
    - Other 2xx → `ok`. Anything else → `error`.

- [ ] **Step 1: Write failing tests:**
  - `test_cohesity_soft_404`: status 200, body contains `ERROR_FALLBACK;404`, host `docs.cohesity.com` → `("gone", "soft_404")`.
  - `test_login_redirect`: url `https://learn.avepoint.com/x`, final `https://login.avepoint.com/?r=x` → `("blocked", "login_redirect")`.
  - `test_short_redirecting_page`: 200, a 400-byte `<html>Redirecting…</html>` → `("blocked", "session_redirect")`.
  - `test_ibm_uses_curl_ua` (respx): a request to `https://www.ibm.com/docs/x` carries `User-Agent: curl/8.5.0`. A request to `https://example.com` carries the settings UA.
  - `test_429_retry_after_honoured` (respx): 429 with `Retry-After: 1`, then 200 → `ok` after 2 calls. Monkeypatch sleep and assert it was called with ≥1.
  - `test_per_host_concurrency`: 6 concurrent fetches to one host with a slow mock → at most 2 in flight at once.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement. **Step 4:** Run. Expected: 6 passed.
- [ ] **Step 5:** Commit `feat: polite async fetcher with soft-404 and login-wall detection`.

### Task 9: Live classification of candidates

**Files:**
- Create: `src/release_watch/discovery/classify.py`, `tests/test_classify.py`, `tests/fixtures/pages/{changelog.html,index.html,unrelated.html,feed.xml}`

**Interfaces:**
- Consumes: `Fetcher` (Task 8), `LLMClient` (Task 7), `WatchedSource`.
- Produces:
  - `def extract_text(html: str, url: str) -> str`: trafilatura with `output_format="markdown"`, `include_links=True`, falling back to selectolax `body` text.
  - `def looks_like_feed(result: FetchResult) -> bool`: true for a content type containing `rss`, `atom` or `xml`, or a body that starts with `<?xml` and contains `<rss` or `<feed`.
  - `async def label_candidate(source: WatchedSource, fetcher, llm) -> Label`, where `@dataclass Label(label, kind, reason, evidence)`:
    - Fetch outcome `blocked` → `Label("blocked", source.kind, result.reason, "")`. **No LLM call.**
    - `gone` → `Label("unsure", …, "gone", "")`.
    - `error` → `Label("unsure", …, "fetch_failed", "")`.
    - A feed → `Label("release_notes", "feed", "feed", "<first item title>")`, if the feed has ≥1 item.
    - Otherwise the LLM gets the vendor name, title, URL and the first 12,000 characters of extracted text, and returns `{"label": "release_notes"|"not_release_notes"|"unsure", "kind": "entry_page"|"index_page"|"unknown", "evidence": "<verbatim heading or sentence>"}`.
    - If the LLM's `evidence` string doesn't occur in the extracted text, the label is downgraded to `unsure` with reason `evidence_not_found`.
    - `LLMError` → `unsure/llm_failed`.
  - `async def label_pending(session_factory, fetcher, llm, *, relabel: bool = False) -> dict[str, int]`: labels every `candidate` row with `label IS NULL` (or all candidates when `relabel`), writes `label`, `label_reason`, `label_evidence`, `labelled_at` and `kind`, and returns counts per label. Fetches run concurrently, bounded by the fetcher's per-host limits. Each row is committed as it's labelled, so a crash keeps progress.

- [ ] **Step 1: Write failing tests**, with a fake LLM and a fake fetcher returning fixture pages:
  - `test_blocked_skips_llm`: the fetch returns blocked/login_redirect → the label is `blocked` and the fake LLM's call count is 0.
  - `test_feed_detected_without_llm`: `feed.xml` (Atom, 3 entries) → `release_notes` / `feed`.
  - `test_llm_label_accepted_when_evidence_present`: the fake LLM returns evidence `"October 28, 2026"`, which is in `changelog.html` → `release_notes` / `entry_page`.
  - `test_hallucinated_evidence_downgraded`: evidence `"Version 99.9"`, not in the text → `unsure` / `evidence_not_found`.
  - `test_llm_failure_is_unsure`: the fake raises `LLMError` → `unsure` / `llm_failed`.
  - `test_label_pending_persists_and_resumes`: 3 candidates, and the fetcher raises a non-HTTP exception on the 3rd → the first 2 rows are labelled in the DB, and the exception propagates.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement. **Step 4:** Run. Expected: 6 passed.
- [ ] **Step 5:** Commit `feat: live fetch + LLM labelling of release-notes candidates`.

### Task 10: Hand-researched seeds

**Files:**
- Create: `src/release_watch/discovery/seeds.py`, `seeds/sources.yaml` (schema plus 2 example entries only; the real content comes in Task 13), `tests/test_seeds.py`

**Interfaces:**
- Produces: `def load_seeds(session, path: Path) -> dict[str, int]`, returning `watched_new`, `news_new`, `unchanged`, `skipped_rejected`.
- YAML schema:
  ```yaml
  watched_sources:
    - vendor: Velero
      product: Velero          # optional; matched to catalog by name within the vendor
      url: https://github.com/vmware-tanzu/velero/releases.atom
      kind: feed               # entry_page | index_page | feed | section
      selector: null           # required when kind == section
      title: Velero releases
      notes: "GitHub releases Atom feed"
  news_sources:
    - vendor: Commvault
      product: null
      url: https://www.commvault.com/news
      kind: listing            # feed | listing | github_releases
      notes: "Press releases"
  ```
- Seeds are inserted with `origin=seed` and `state=candidate`, so the owner still reviews them. Re-loading is idempotent by `url_key`. A rejected row is never re-opened. An unknown `vendor` name raises `ValueError`, naming the entry index.

- [ ] **Step 1: Write failing tests:**
  - `test_seed_inserts_candidates`: the 2-entry YAML → `watched_new == 1`, `news_new == 1`, with both rows `state == "candidate"` and `origin == "seed"`.
  - `test_seed_idempotent`: a second load → `unchanged == 2` and no new rows.
  - `test_seed_unknown_vendor_errors`: vendor `Nope` → `ValueError` with `"entry 0"` in the message.
  - `test_section_key_includes_selector`: two Keepit entries at the same URL with selectors `#updates` and `#pmc` → 2 rows.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement. **Step 4:** Run. Expected: 4 passed.
- [ ] **Step 5:** Commit `feat: load hand-researched sources from YAML seeds`.

### Task 11: Review queue web app

**Files:**
- Create: `src/release_watch/web/__init__.py`, `src/release_watch/web/app.py`, `src/release_watch/web/templates/base.html`, `src/release_watch/web/templates/queue.html`, `tests/test_web.py`

**Interfaces:**
- Consumes: the models, and `Settings.docextractor_ui_url`.
- Produces: `def create_app(session_factory) -> FastAPI` with routes:
  - `GET /healthz` → `{"status": "ok", "db": "ok"}`. Returns 503 with `"db": "error"` if `SELECT 1` fails.
  - `GET /` → the review queue, HTML. Watched-source candidates and news-source candidates are grouped by vendor. Each row shows the title, the URL as an external link, kind, label with reason and evidence, origin and the login-walled flag. Filters by query string: `state` (default `candidate`), `label`, `vendor`. A count summary sits at the top ("412 candidates: 280 release_notes, 90 not, 30 unsure, 12 blocked").
  - `POST /decide` with form fields `ids` (repeated), `table` (`watched` or `news`) and `decision` (`confirm` or `reject`). Confirm sets `state=active`; reject sets `state=rejected`. Both set `decided_at` and `confirmed_by="owner"`. Responds with a 303 redirect back to the same filters.
  - `POST /decide` with `decision=reset` sets `state=candidate`, to undo a mistaken decision.
- Plain server-rendered HTML with a checkbox per row, "select all in vendor", and Confirm/Reject buttons. No JavaScript framework. LAN-only, no auth (spec §12).

- [ ] **Step 1: Write failing tests** with FastAPI `TestClient`:
  - `test_healthz_ok`: 200, `{"status": "ok", "db": "ok"}`.
  - `test_queue_lists_candidates_grouped_by_vendor`: 2 vendors, 3 candidates, 1 active → the HTML contains both vendor names and the 3 candidate titles, but not the active one.
  - `test_confirm_sets_active`: POST confirm for 2 ids → both `active`, `decided_at` set, 303.
  - `test_reject_then_reset`: reject → `rejected`; reset → `candidate`.
  - `test_label_filter`: `?label=unsure` shows only unsure rows.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement. **Step 4:** Run. Expected: 5 passed.
- [ ] **Step 5:** Commit `feat: web review queue for candidate sources`.

### Task 12: CLI jobs

**Files:**
- Create: `src/release_watch/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a Typer `app` with these commands:
  - `sync`: `job_run("sync")` around `sync_catalog`.
  - `discover [--relabel]`: `job_run("discover")` around `sync_catalog`, then `propose_candidates` + `upsert_proposals`, then `label_pending`. Counts from every stage are merged into the job's counts.
  - `seed PATH`: `load_seeds`.
  - `web [--host 0.0.0.0] [--port 8000]`: uvicorn on `create_app`.
  - `migrate`: `alembic upgrade head`.

  Each command exits non-zero on failure, after `job_run` has recorded it.

- [ ] **Step 1: Write failing tests** with Typer `CliRunner`, monkeypatching the client factory to fakes:
  - `test_sync_command_writes_job_run`: exit code 0, and one `job_run` row with `job == "sync"` and `status == "ok"`.
  - `test_discover_failure_exit_code`: the fake client raises `DocExtractorError` → exit code 1, a `job_run` row with `status == "failed"`, and no secret in the output.
- [ ] **Step 2:** Run. Expected: FAIL. **Step 3:** Implement. **Step 4:** Run. Expected: 2 passed.
- [ ] **Step 5:** Run the full suite with `uv run pytest -q`. Expected: all pass.
- [ ] **Step 6:** Commit `feat: CLI for sync, discover, seed, web and migrate`.

### Task 13: Initial discovery run and hand research (the builder does this)

This task produces data and a review-ready queue, not code. It runs against **production DocExtractor** (read-only) from the build machine, with a local Postgres.

- [ ] **Step 1:** Start a local Postgres in Docker (`rw-dev-pg`). Run `uv run release-watch migrate`, then `uv run release-watch discover`. Expected: the job ends `ok`. Counts show 43 vendors, 109 products (6 flagged excluded) and 396 sources synced, and a few hundred proposals labelled.
- [ ] **Step 2:** Review the label distribution with SQL. Spot-check 20 `release_notes` and 20 `not_release_notes` labels by opening the URLs. If precision is clearly off, adjust `RELEASE_PATTERN` or the prompt, run `discover --relabel`, and commit those code changes with tests.
- [ ] **Step 3: Hand research** (web search plus fetches) and write `seeds/sources.yaml` entries, each with a `notes` line citing why it was chosen:
  - Release-notes sources for the 12 vendors with none in DocExtractor: AWS, CloudAlly, Eon, Flosum, Gearset, Google, Nakivo, Red Hat, Salesforce, Slide, Synology, Velero. Velero is the GitHub releases Atom feed.
  - Public substitutes for Rubrik and Cohesity release notes.
  - Veeam Data Cloud: `https://www.veeam.com/veeam-data-cloud/change-log.html`. Also Kasten's and Securiti.ai's public release notes, if the heuristic missed them.
  - Keepit's `#updates` and `#pmc` sections, as two `section` entries.
  - **News sources** (blog, press releases, RSS) for every vendor that has a versioned product: run `SELECT DISTINCT vendor` over products with `de_version` set.
  - AvePoint and Eon release-notes URLs, with `kind` set. They will label as `blocked` until phase 2 adds logins, and that is expected.
- [ ] **Step 4:** Run `uv run release-watch seed seeds/sources.yaml`, then `uv run release-watch discover`, which labels the seeded rows.
- [ ] **Step 5:** Commit `data: initial hand-researched sources and news sources`.
- [ ] **Step 6:** Hand over to the owner: start `uv run release-watch web` locally (or use the deployed app after Task 14) and send the link with a summary of counts by label. **The owner reviews and confirms the list.** Phase 1 is done when no `candidate` rows remain.

### Task 14: Container, chart, CI and deployment

**Files:**
- Create: `Dockerfile`, `.dockerignore` (excludes `.secrets`, `.git`, `.venv`, `tests`), `.github/workflows/ci.yml`, `deploy/helm/release-watch/Chart.yaml`, `values.yaml`, `values-homelab.yaml`, `templates/{_helpers.tpl,secret.yaml,configmap.yaml,web-deployment.yaml,web-service.yaml,ingress.yaml,postgres-statefulset.yaml,postgres-service.yaml,cronjob-sync.yaml,cronjob-discover.yaml}`, `README.md`

**Interfaces / values pinned by the spec:**
- **Image:** `python:3.13-slim`, installed with `uv sync --frozen --no-dev`, entrypoint `release-watch`. Published to `ghcr.io/carev01/release-watch`, tagged `latest`, sha and tag. CI mirrors DocExtractor's `.github/workflows/ci.yml`: a `tests` job with a `postgres:16` service, `alembic upgrade head` and `pytest -q`, then an `image` job on pushes to main and tags.
- **Namespace** `release-watch`. **Ingress host** `release-watch.k3s.home.lan` on the Traefik default class.
- **web:** a Deployment with 1 replica, command `release-watch web`, and a `migrate` init container running `release-watch migrate`, the same pattern as DocExtractor. Liveness and readiness probes on `/healthz`.
- **CronJobs:** `sync` at `0 5 * * *` and `discover` at `30 5 * * 1`, both with `concurrencyPolicy: Forbid`, `timeZone` set from `values.timezone` (default `"America/Sao_Paulo"`; the owner can change it in values), and `backoffLimit: 1`.
- **postgres:** a StatefulSet with `postgres:16-alpine` and a 1 GiB PVC. `storageClassName: ""` means the cluster default, as in DocExtractor.
- **Secret** `release-watch-secrets` with the keys `RW_DOCEXTRACTOR_API_KEY`, `RW_AZURE_OPENAI_API_KEY`, `RW_AZURE_OPENAI_ENDPOINT`, `RW_OPENROUTER_API_KEY`, `POSTGRES_PASSWORD`. It is created by `kubectl create secret … --from-literal`, as documented in the README, and never put in values files. The chart references it by name with `existingSecret`.
- **ConfigMap:** `RW_DOCEXTRACTOR_BASE_URL=http://docextractor-backend.docextractor.svc:8000/api`, and `RW_DATABASE_URL` built from the in-chart Postgres service with the password from the secret.

- [ ] **Step 1:** Write the Dockerfile. Run `docker build -t release-watch:dev .`, then `docker run --rm release-watch:dev --help`. Expected: the five commands are listed.
- [ ] **Step 2:** Write the chart. Run `helm lint deploy/helm/release-watch -f deploy/helm/release-watch/values-homelab.yaml`, then `helm template … | kubectl apply --dry-run=server -f -`. Expected: lint passes, and the dry run reports no errors.
- [ ] **Step 3:** Write the CI workflow and the README (install steps, secret creation, the Power Automate note can wait for phase 2).
- [ ] **Step 4:** Commit `build: container image, Helm chart and CI`.
- [ ] **Step 5 (needs the owner's go-ahead; this publishes and deploys):** ask the owner to confirm before:
  - creating the GitHub repo `carev01/release-watch` (private) and pushing,
  - letting CI publish the image (and making the ghcr package readable by the cluster),
  - creating the namespace and secret,
  - running `helm upgrade --install release-watch deploy/helm/release-watch -n release-watch -f deploy/helm/release-watch/values-homelab.yaml`.
- [ ] **Step 6:** Verify the deployment:
  - `kubectl -n release-watch get pods,cronjobs,ingress` shows everything healthy,
  - `curl http://release-watch.k3s.home.lan/healthz` returns `{"status":"ok","db":"ok"}`,
  - `kubectl -n release-watch create job --from=cronjob/release-watch-sync sync-manual` completes and leaves a `job_run` row with status `ok`.

---

## Deferred to phase 2 (deliberately not in this plan)

- Testing the body selector and entry-split rule per source before it goes `active`: the splitter is built in phase 2. Confirmed sources keep `selector`/`split_rule` null until then.
- The Browserless/Firecrawl fetch modes, AvePoint/Eon logins, the digest, the Teams webhook, and the `watch` CronJob.
- Reusing DocExtractor's per-platform body selectors (`../doc-extractor/backend/app/services/profiles/*.py`), needed only for phase 2 extraction.
