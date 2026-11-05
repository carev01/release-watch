# Weekly Login Visit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Once a week, sign in to AvePoint and Securiti through Browserless, read their 55 login-walled release-notes sources with the signed-in cookies, and run them through the existing watch pipeline.

**Architecture:** A login script per vendor runs in Browserless `/function` and returns the signed-in browser's cookies. A `SessionFetcher` reads each page in its own short Browserless session with those cookies, so no session hits the 5-minute limit. `run_watch` gains a "login run" mode (only the vendor's hosts, same-visit edit confirmation, moved pages to Needs a look). A weekly CLI command `watch-login` drives it per vendor and alerts once per failed login.

**Tech Stack:** Python 3.13, httpx, respx (tests), SQLAlchemy/Postgres, typer, Browserless `/function` (Puppeteer), Helm.

**Spec:** `docs/superpowers/specs/2026-11-05-weekly-login-sources-design.md`

## Global Constraints

- Credentials (`RW_AVEPOINT_USER`, `RW_AVEPOINT_PASSWORD`, `RW_SECURITI_USER`, `RW_SECURITI_PASSWORD`) never appear in any log line, exception message, `job_run.counts`, event, card or printed Browserless reply. They travel only inside the Browserless `/function` `context` JSON, never inside the `code` string.
- Browserless' raw reply body is never printed or stored; failures become short reason codes (`browserless_http_500`, `browserless_ConnectError`, `browserless_bad_payload`, `stopped at <step>`).
- Exactly one login attempt per vendor per run. No login retries anywhere.
- Browserless is shared with DocExtractor: at most `BROWSER_CONCURRENCY` (2) concurrent sessions from this job, and at least 2 s between page reads.
- All schedules and times are UTC. The weekly job runs `30 9 * * 1` (Mondays 09:30 UTC).
- Tests need no network and no credentials (respx for Browserless, `ScriptedFetcher` for pages).
- `uv run ruff check src tests` and the full `uv run pytest -q` (with `RW_TEST_DATABASE_URL=postgresql+psycopg://postgres:rw@localhost:55432/rw_test`) pass at the end of every task.
- Commits end with the session's attribution lines; pushing to `main` is allowed.

## Review Focus

1. **Cookies expire mid-visit** (short sessions): later pages arrive as a login redirect or the ~400-byte "Redirecting…" page; they must count as not read, never as entries removed. Pinned in Task 2 (`test_session_fetcher_signed_out_page_is_blocked`) and Task 1 (`test_login_run_signed_out_page_is_never_compared`).
2. **The Securiti pop-out doesn't carry over** to new sessions: every help-center page is blocked. The run must report "0 of 28 read", not crash or alert per page. Pinned in Task 3 (`test_vendor_with_every_page_blocked_reports_zero_read`); Task 6 has the stop rule.
3. **Passwords with JSON-hostile characters** (quotes, backslashes, `${`): must reach the login form unchanged and never land in the script text. Pinned in Task 2 (`test_login_sends_credentials_only_in_context`).
4. **Browserless busy or down at login time** (HTTP 429/503, connection refused): one failed login with a reason code and one alert, no retry. Pinned in Task 2 (`test_login_error_reply_is_never_echoed`) and Task 3.
5. **A crash while reading one vendor** must not stop the other vendor or lose the job record. Pinned in Task 3 (`test_crash_in_one_vendor_does_not_stop_the_other`).

---

### Task 1: Login-run mode in the watch pipeline

**Files:**
- Modify: `src/release_watch/watch/run.py` (`run_watch`, `_settle_first`, `_settle_confirmed`, `_docextractor_moved`)
- Test: `tests/test_watch_run.py`

**Interfaces:**
- Produces: `run_watch(session_factory, fetcher, llm, *, confirm_delay, now=utcnow, sleep=asyncio.sleep, only=None, dry_run=False, out=print, login_hosts: tuple[str, ...] | None = None, same_visit_edits: bool = False) -> dict`
  - `login_hosts=None` (daily): unchanged; login-walled sources stay excluded.
  - `login_hosts` given: sources are active, `login_walled=True`, and `host_of(url)` is in `login_hosts` (exact host match, `release_watch.urls.host_of`). `propose_successors` and `retire_older_versions` are skipped; the returned counts have no `successors` or `retired` keys.
  - `same_visit_edits=True`: every edit present in both the first read and the confirmation read is sent at once (no `EDIT_HOLD`, no `pending_hash`).
  - In login runs, a page with no entries or a suspect page still stores a `Snapshot` of its markdown, so split rules can be tuned offline without another login.
  - `_docextractor_moved`: an event for a `login_walled` source gets `status="needs_look"` instead of `"queued"` (same reason codes, same dedupe key).

- [ ] **Step 1: Write the failing tests** in `tests/test_watch_run.py`. Let `docextractor_source(sf, url=URL, **kw)` pass `**kw` through to `add_source`.

```python
AVE = "https://learn.avepoint.com/m365/whats-new.html"

async def test_login_run_reads_only_login_sources_on_its_hosts(session_factory):
    add_source(session_factory)                                         # public
    add_source(session_factory, AVE, login_walled=True)                 # this vendor
    add_source(session_factory, "https://other.com/rn", login_walled=True)
    fetcher = ScriptedFetcher({AVE: html_page(AVE, DAY1)})
    counts = await run(session_factory, fetcher, login_hosts=("learn.avepoint.com",))
    assert fetcher.calls == [AVE] and counts["sources"] == 1 and counts["baseline"] == 1
    assert "successors" not in counts and "retired" not in counts

async def test_same_visit_edit_is_sent_on_the_confirming_read(session_factory):
    add_source(session_factory, AVE, login_walled=True)
    edited = DAY1.replace("<li>SAML sign-on.</li>", "<li>SAML sign-on.</li><li>SCIM provisioning.</li>")
    kw = dict(login_hosts=("learn.avepoint.com",), same_visit_edits=True)
    await run(session_factory, ScriptedFetcher({AVE: html_page(AVE, DAY1)}), now=later(0), **kw)
    await run(session_factory, ScriptedFetcher({AVE: html_page(AVE, edited)}), now=later(168), **kw)
    [ev] = events(session_factory)
    assert ev.kind == "edited" and ev.diff == "**Added**\n- SCIM provisioning."

async def test_login_source_redirect_goes_to_needs_look(session_factory):
    docextractor_source(session_factory, AVE, login_walled=True)
    for _ in range(2):
        await run(session_factory, ScriptedFetcher({AVE: redirected(AVE, "https://learn.avepoint.com/m365/new.html")}),
                  login_hosts=("learn.avepoint.com",))
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.reason) == ("moved", "needs_look", "docextractor_moved")

async def test_login_run_signed_out_page_is_never_compared(session_factory):
    sid = add_source(session_factory, AVE, login_walled=True)
    kw = dict(login_hosts=("learn.avepoint.com",), same_visit_edits=True)
    await run(session_factory, ScriptedFetcher({AVE: html_page(AVE, DAY1)}), **kw)
    signed_out = html_page(AVE, "Redirecting…", outcome="blocked", reason="session_redirect")
    await run(session_factory, ScriptedFetcher({AVE: signed_out}), **kw)
    with session_factory() as s:
        assert all(e.removed_at is None for e in s.scalars(select(Entry).where(Entry.source_id == sid)))
    assert events(session_factory) == []

async def test_login_run_keeps_a_snapshot_of_a_page_without_entries(session_factory):
    sid = add_source(session_factory, AVE, login_walled=True)
    await run(session_factory, ScriptedFetcher({AVE: html_page(AVE, "<h2>Server</h2><p>Faster restores.</p>")}),
              login_hosts=("learn.avepoint.com",))
    with session_factory() as s:
        [snap] = s.scalars(select(Snapshot).where(Snapshot.source_id == sid))
    assert "Faster restores." in snap.markdown
```

- [ ] **Step 2: Run them.** `uv run pytest -q tests/test_watch_run.py -k "login or same_visit"` → FAIL (`unexpected keyword argument 'login_hosts'`).
- [ ] **Step 3: Implement** the interface above. Thread `same_visit_edits` into `_settle_confirmed`; when true, `fire` is every entry in `changes.edited` and `pend` stays empty. Pass `login_hosts is not None` into `_settle_first` to store the snapshot on the suspect / no-entries path.
- [ ] **Step 4: Run** the five tests, then the full suite and ruff → PASS (the existing `test_login_walled_sources_skipped` still passes).
- [ ] **Step 5: Commit** `Watch: login-run mode (vendor hosts only, same-visit edits, moved pages to Needs a look)`.

---

### Task 2: Browser login and the signed-in fetcher

**Files:**
- Modify: `src/release_watch/config.py` (`Settings`), `src/release_watch/fetch.py` (extract the reply parsing)
- Create: `src/release_watch/watch/login.py`
- Test: `tests/test_login.py`

**Interfaces:**
- Consumes: `classify_response`, `FetchResult`, `HostPolicy`, `BROWSER_CONCURRENCY`, `MAX_BODY_BYTES` from `fetch.py`.
- Produces:
  - `Settings` fields: `avepoint_user`, `avepoint_password`, `securiti_user`, `securiti_password` (all `SecretStr = SecretStr("")`), `login_confirm_delay_seconds: int = 300`.
  - `fetch.parse_browser_reply(url: str, body: dict) -> FetchResult`: the parsing now inside `Fetcher._fetch_in_browser` (from `data = body.get("data", body)` to the final `FetchResult`), which then uses it.
  - `login.LoginSite(name: str, hosts: tuple[str, ...], script: str, user_field: str, password_field: str)` (frozen dataclass), and `login.SITES = (AVEPOINT, SECURITI)`:
    - `AVEPOINT = LoginSite("AvePoint", ("learn.avepoint.com",), AVEPOINT_LOGIN, "avepoint_user", "avepoint_password")`
    - `SECURITI = LoginSite("Securiti", ("helpcenter.securiti.ai", "docs.securiti.ai"), SECURITI_LOGIN, "securiti_user", "securiti_password")`
  - `login.LoginResult(ok: bool, reason: str, cookies: list[dict])`, with a `__repr__` that shows the cookie count, never the values.
  - `async def browser_login(client: httpx.AsyncClient, settings: Settings, site: LoginSite) -> LoginResult`
  - `class SessionFetcher(client: httpx.AsyncClient, settings: Settings, cookies: list[dict], *, pause: float = 2.0)` with `async def fetch(self, url: str) -> FetchResult`.

The scripts (JS strings in `login.py`, ported from the 2026-11-05 trial, which worked):
- **`AVEPOINT_LOGIN`:** viewport 1440×900, `context.ua`. Open `https://learn.avepoint.com/api/auth/aos-sso/commercial?returnUrl=%2F`. Type into `#account_login_username` and `#account_login_password`, press Enter, wait for navigation. It's ok only if the page URL then starts with `https://learn.avepoint.com`.
- **`SECURITI_LOGIN`:**
  1. Open `https://app.securiti.ai/#/login`. Type into `#email` and click `button[type=submit]`.
  2. Click the leaf element whose text is `Select Domain`, then the leaf element whose text starts with `Privaci Demo New (`, then `button[type=submit]`.
  3. Type into `input[type=password]` and press Enter. Wait up to 20 s for a URL containing `#/reporting-dashboard`, or any URL off `#/login`.
  4. Click `button[aria-label="Help Center"]` and wait 5 s.
  5. In the help pane, click the open-in-new-window control: first look in every frame for an element whose `title`/`aria-label`/text matches `/open in new|new window|new tab|open_in_new/i`. Otherwise click at (1410, 155).
  6. Wait up to 20 s for a new target. It's ok only if its URL host is `helpcenter.securiti.ai`.
- **Both scripts:** each stop point returns `{ok: false, step: "<fixed text>"}`. Use exactly these steps:
  - `login form`
  - `domain drop-down`
  - `domain option`
  - `password field`
  - `signed in`
  - `help center button`
  - `help center window`

  On success they return `{ok: true, step: "done", cookies}`. The cookies come from `Network.getAllCookies` through `page.target().createCDPSession()`, taken after the last step so they include the help-center domain. Credentials arrive as `context.user` and `context.pw`.
- **`READ_PAGE`:** `page.setCookie(...context.cookies)`, then the same `goto`/status/HTML capture as `BROWSER_FUNCTION` in `fetch.py` (no warm-up).

- [ ] **Step 1: Write the failing tests** in `tests/test_login.py`. Mock with `respx.mock` on `POST http://browserless.browserless.svc.cluster.local:3000/function`; build `Settings(database_url=..., docextractor_api_key="k", browserless_token="tok", avepoint_user="u@x", avepoint_password='p"\\${x}')`.
  - `test_login_without_credentials_makes_no_request`: the site's user is empty → `LoginResult(False, "no_credentials", [])`, and the route was not called.
  - `test_login_sends_credentials_only_in_context`: the request JSON has `context.user == "u@x"` and `context.pw == 'p"\\${x}'`, the password is not in `json["code"]`, the header is `Authorization: Bearer tok`, and `"tok"` is not in the URL.
  - `test_login_success_returns_cookies`: reply `{"data": {"ok": true, "step": "done", "cookies": [{"name": "s", "value": "v", "domain": "learn.avepoint.com"}]}}` → `ok`, one cookie, and `"v" not in repr(result)`.
  - `test_login_stopped_step_is_the_reason`: reply `{"data": {"ok": false, "step": "password field"}}` → `LoginResult(False, "stopped at password field", [])`.
  - `test_login_error_reply_is_never_echoed`: status 500 with body `"boom p\"\\${x}"` → reason `"browserless_http_500"`, and the password is not in `repr(result)`. Also status 429 → `"browserless_http_429"`, with exactly one request made (no retry).
  - `test_session_fetcher_reads_page_with_cookies`: the request `context.cookies` equals the cookies given. A reply with `html`, `finalUrl` = the url and status 200 → `outcome == "ok"`, and the text is the html.
  - `test_session_fetcher_signed_out_page_is_blocked`: reply `finalUrl` = `https://learn.avepoint.com/login?returnUrl=%2Fm365` → `outcome == "blocked"`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_login.py` → FAIL (`No module named 'release_watch.watch.login'`).
- [ ] **Step 3: Implement** the interfaces. `browser_login` uses one POST with timeout 150 s and no retry. `SessionFetcher` uses a `Semaphore(BROWSER_CONCURRENCY)` and paces calls at least `pause` seconds apart. It classifies with `classify_response(..., HostPolicy())` via `parse_browser_reply`, and turns non-200 or transport errors into `FetchResult(url, url, None, "", "", 0, "error", reason)` with the same reason codes.
- [ ] **Step 4: Run** the new tests, the full suite (the existing Browserless fetch tests still pass after the extraction) and ruff → PASS.
- [ ] **Step 5: Commit** `Login: Browserless login scripts for AvePoint and Securiti, and a signed-in fetcher`.

---

### Task 3: The weekly visit and its CLI command

**Files:**
- Create: `src/release_watch/watch/weekly.py`, `src/release_watch/notify/alert.py`
- Modify: `src/release_watch/cli.py` (new `watch-login` command; `_alert_failure` uses `send_alert`)
- Test: `tests/test_weekly.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `run_watch(..., login_hosts=, same_visit_edits=)` (Task 1); `LoginSite`, `LoginResult`, `SITES`, `browser_login`, `SessionFetcher` (Task 2).
- Produces:
  - `notify.alert.send_alert(teams: Teams | None, session_factory, text: str) -> Awaitable[None]`: posts `text_card(text)` to Teams when set, and stores a `Digest(channel="teams" if teams else "web", cards=[card])`. `cli._alert_failure` calls it.
  - `weekly.run_login_visits(session_factory, llm, *, login: Callable[[LoginSite], Awaitable[LoginResult]], fetcher_for: Callable[[list[dict]], object], alert: Callable[[str], Awaitable[None]], confirm_delay: float, sites=SITES, now=utcnow, sleep=asyncio.sleep) -> dict` returning `{"vendors": {site.name: {...}}}`. Per vendor, `sources` = the number of active login-walled sources on its hosts.
    - Login failed: `{"login": "failed", "reason": r, "sources": n, "read": 0}` and one `alert(f"Release Watch: the weekly login to {name} failed ({r}). Its {n} pages were not read this week.")`.
    - Login ok: `run_watch(... fetcher_for(cookies), confirm_delay=confirm_delay, login_hosts=site.hosts, same_visit_edits=True)`, then `{"login": "ok", "sources": c["sources"], "read": c["baseline"] + c["same"] + c["changed"], **c}`.
    - Exception while reading a vendor: `{"login": "ok", "reason": f"crash: {type(exc).__name__}", "sources": n, "read": 0}` and one alert `f"Release Watch: the weekly visit to {name} stopped ({reason})."`. Then the next vendor runs.
  - CLI `release-watch watch-login`: `_run("watch-login", body)`. The body opens one `httpx.AsyncClient`, uses `browser_login(client, settings, site)` and `SessionFetcher(client, settings, cookies)`, alerts through `send_alert(make_teams(settings), session_factory, text)`, and sets `confirm_delay=settings.login_confirm_delay_seconds`.

- [ ] **Step 1: Write the failing tests** in `tests/test_weekly.py`. Use two fake sites, `LoginSite("A", ("a.com",), "", "u", "p")` and `LoginSite("B", ("b.com",), "", "u", "p")`, with one login-walled source each (`https://a.com/rn`, `https://b.com/rn`). `login` records its calls; `alerts` is a list.
  - `test_failed_login_alerts_once_and_skips_only_that_vendor`: A fails with `"stopped at password field"` and B is ok. Then:
    - `login` was called once per site;
    - `alerts == ["Release Watch: the weekly login to A failed (stopped at password field). Its 1 pages were not read this week."]`;
    - the fetcher calls are only `https://b.com/rn`;
    - `result["vendors"]["A"] == {"login": "failed", "reason": "stopped at password field", "sources": 1, "read": 0}`;
    - `result["vendors"]["B"]["read"] == 1`.
  - `test_vendor_with_every_page_blocked_reports_zero_read`: B's page returns `outcome="blocked", reason="login_redirect"` → `read == 0`, `login == "ok"`, and no alert.
  - `test_crash_in_one_vendor_does_not_stop_the_other`: monkeypatch `release_watch.watch.weekly.run_watch` with a wrapper that raises `RuntimeError` when `login_hosts == ("a.com",)` and otherwise calls the real one. Then A has `reason == "crash: RuntimeError"`, B is still read, and A gets one alert.
  - In `tests/test_cli.py`: `test_watch_login_records_its_own_job` monkeypatches `release_watch.cli.run_login_visits` to return `{"vendors": {}}`, invokes `["watch-login"]`, and checks one `job_run` with `job == "watch-login"` and `status == "ok"`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_weekly.py tests/test_cli.py -k "login or vendor"` → FAIL (`No module named 'release_watch.watch.weekly'`).
- [ ] **Step 3: Implement** the interfaces.
- [ ] **Step 4: Run** the new tests, the full suite and ruff → PASS.
- [ ] **Step 5: Commit** `Weekly login visit: one login per vendor, alert once on failure, release-watch watch-login`.

---

### Task 4: Health shows the weekly visit

**Files:**
- Modify: `src/release_watch/health.py`, `src/release_watch/notify/cards.py`
- Test: `tests/test_health.py`, `tests/test_cards.py`

**Interfaces:**
- Consumes: `job_run` rows with `job="watch-login"` and the counts shape from Task 3.
- Produces: `Health.weekly_login: str = ""` replaces `Health.waiting_login`.
  - `compute_health` takes the latest `JobRun` with `job == "watch-login"`:
    - none → `"Weekly login: no visit yet"`;
    - `now - started_at > timedelta(days=8)` → `f"Weekly login: no visit since {started_at:%a %d %b}"`;
    - `status == "failed"` → `f"Weekly login: the visit on {started_at:%a %d %b} failed"`;
    - otherwise `"Weekly login: " + ", ".join(parts)`, with each part either `f"{name} {read} of {sources} read {started_at:%a %d %b}"` (login ok) or `f"{name} login failed {started_at:%a %d %b} ({reason})"`, in the order of `counts["vendors"]`.
  - `summary_card` appends `health.weekly_login` to the health block in place of the "Waiting for login (phase 3)" line.
  - The daily `checked of total` stays as it is (login-walled sources excluded).
  - Unread pages show only in the vendor's "X of Y read" figure. Listing each one would add up to 28 lines; that's a builder's call.

- [ ] **Step 1: Write the failing tests.** In `tests/test_health.py`, replace `test_waiting_login_grouped_by_vendor` with:
  - `test_weekly_login_line_per_vendor`: a `watch-login` run 2 days before `NOW` (a Monday, 09:30 UTC) with counts `{"vendors": {"AvePoint": {"login": "ok", "sources": 26, "read": 26}, "Securiti": {"login": "failed", "reason": "stopped at password field", "sources": 29, "read": 0}}}` → `h.weekly_login == "Weekly login: AvePoint 26 of 26 read Mon 02 Nov, Securiti login failed Mon 02 Nov (stopped at password field)"`.
  - `test_weekly_login_stale_after_eight_days` → `"Weekly login: no visit since …"`.
  - `test_weekly_login_never_ran` → `"Weekly login: no visit yet"`.
  - In `tests/test_cards.py`, the summary test uses `weekly_login="Weekly login: AvePoint 26 of 26 read Mon 02 Nov"` and expects that line in the text.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_health.py tests/test_cards.py` → FAIL.
- [ ] **Step 3: Implement**, then `grep -rn waiting_login src tests` → no matches.
- [ ] **Step 4: Run** the full suite and ruff → PASS.
- [ ] **Step 5: Commit** `Health: weekly login visit line replaces "waiting for login"`.

---

### Task 5: Helm CronJob and README

**Files:**
- Create: `deploy/helm/release-watch/templates/cronjob-watch-login.yaml`
- Modify: `deploy/helm/release-watch/values.yaml`, `deploy/helm/release-watch/templates/_helpers.tpl`, `README.md`

**Interfaces:**
- Produces:
  - **`rw.loginEnv` helper:** `secretKeyRef` entries (`optional: true`) for `RW_AVEPOINT_USER`, `RW_AVEPOINT_PASSWORD`, `RW_SECURITI_USER`, `RW_SECURITI_PASSWORD`.
  - **`cron.watchLogin: "30 9 * * 1"`:** in `values.yaml`, plus the four keys in the "Created by hand" comment.
  - **CronJob `{{ .Release.Name }}-watch-login`:** a copy of `cronjob-watch.yaml` with `args: ["watch-login"]`, `component: watch-login`, `timeZone: {{ .Values.timezone }}` (UTC), and `env` = `rw.env` plus `rw.loginEnv` on the main container only (not on the `migrate` init container).
- [ ] **Step 1: Check the rendering fails first:** `helm template rw deploy/helm/release-watch | grep -c RW_AVEPOINT_PASSWORD` → `0`.
- [ ] **Step 2: Implement.** README:
  - add a `release-watch watch-login` row to the CLI table;
  - add `| watch-login | 09:30 Mondays | Signs in to AvePoint and Securiti once each and reads their login-walled pages (one login per vendor; a failed login sends one alert) |` to the jobs table;
  - add a short "Weekly login" subsection with the `kubectl patch secret` command for the four keys (placeholders only).
- [ ] **Step 3: Verify:**
  - `helm template rw deploy/helm/release-watch | grep -c RW_AVEPOINT_PASSWORD` → `1`;
  - the watch-login CronJob's schedule is `30 9 * * 1` with `timeZone: "UTC"`;
  - no other workload gets the login keys (`helm template … | grep -B40 RW_AVEPOINT_PASSWORD | grep "name: rw-watch-login"`);
  - `helm lint deploy/helm/release-watch` → no errors.
- [ ] **Step 4: Commit** `Helm: weekly watch-login CronJob (Mondays 09:30 UTC) with the login credentials`.

---

### Task 6: Deploy, first visit, tune, re-baseline (operations, live)

Each step here touches the owner's vendor accounts. Budget: the first visit, plus at most one more login per vendor. If a step's outcome isn't the one stated, stop and report to the owner instead of logging in again.

- [ ] **Step 1: Deploy.** Push, wait for CI (`gh run watch`), back up the DB to `~/release-watch-backups/<date>-before-weekly-login.sql`, run `helm -n release-watch upgrade release-watch deploy/helm/release-watch -f deploy/helm/release-watch/values-homelab.yaml`, restart the web deployment, then check `kubectl -n release-watch get cronjob release-watch-watch-login` shows `30 9 * * 1` UTC.
- [ ] **Step 2: First visit (login 1 per vendor).** Run `kubectl -n release-watch create job rw-login-first --from=cronjob/release-watch-watch-login`, wait, then read the job's last log line (the JSON counts; it holds no credentials).
  - Expected: both vendors `"login": "ok"`. AvePoint `read` is close to 26; Securiti `read` is close to 28 (the `docs.securiti.ai` page may fail).
  - If Securiti `read` is 0 with every page blocked, the cookies don't carry over: **stop and report** (spec rule).
- [ ] **Step 3: Tune offline.**
  - For each source with `suspect`/`no_entries` or fewer than 2 entries, read its latest `snapshot.markdown` with psql.
  - Add a `split_rule` to `seeds/sources.yaml` `source_config`, following the existing precedents: `whole: true` + `key` for one-page summaries, `heading_regex` for version headings. Verify each rule against the stored markdown with `release_watch.watch.split.split_entries` before committing.
  - Seed through a port-forward (as in `deployment-state` memory).
- [ ] **Step 4: Re-baseline (login 2 per vendor, only if Step 3 changed a vendor's rules).** Delete `entry` and `snapshot` rows for that vendor's login sources, re-run the job (`rw-login-second`), and check the baseline counts and that `events_queued == 0`.
- [ ] **Step 5: Wrap up.**
  - If `docs.securiti.ai/release-notes` was blocked or empty, tell the owner and propose rejecting it.
  - Check the Notifications page health block shows the "Weekly login: …" line.
  - Update the `deployment-state` memory.
