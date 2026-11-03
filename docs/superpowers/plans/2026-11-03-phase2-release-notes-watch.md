# Phase 2: Release-Notes Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every day, fetch each active public release-notes source, find new and edited entries (including moving targets), confirm and gate them, and deliver them to the owner's Teams chat. New entries are sent verbatim and edited entries as diffs, each with a one-line summary, alongside a health summary that makes every failure visible.

**Architecture:**
- A `watch` CronJob (06:00) reads each source into a list of entries:
  - entry pages are split on date or version headings,
  - feeds give one entry per item,
  - JSON listings give one entry per item,
  - index pages give one entry per child link,
  - version pages give one entry per version.
- It compares the entries with the stored ones, re-fetches changed sources about 10 minutes later to confirm, applies the plausibility gates, writes `entry_event` rows with LLM summaries, and proposes successors for version-pinned URLs.
- A `digest` CronJob (08:00) turns queued events plus a health block into Adaptive Cards and posts them to the Power Automate webhook.
- The web app gains a "needs a look" queue and an event page that holds the full verbatim text.

**Tech Stack:**
- Python 3.13, httpx, SQLAlchemy 2 + Alembic, Postgres 16, FastAPI + Jinja2.
- trafilatura, selectolax (lexbor), feedparser, and **markdownify** (new: it converts selector-bound HTML and feed/JSON item HTML to Markdown).
- pytest + respx.

**Spec:** `docs/superpowers/specs/2026-11-03-release-watch-design.md`, sections 7, 9 and 12, and Phase 2 in section 13.

## Global Constraints

- **False positives are the worst failure.** When unsure, put the event in `needs_look`, never in the digest.
- **The verbatim text is the un-normalised extracted Markdown.** The LLM never writes, rewrites or trims it. A summary failure sends the entry without a summary.
- **The first successful read of a source is its baseline.** All its entries are stored, and none are reported.
- **Gates (spec 7.3):**
  - more than **3** new entries at once → `needs_look`,
  - zero entries where there were entries before → suspect,
  - size under **20%** of the previous size → suspect,
  - login redirect, block page or soft-404 → suspect,
  - a whitespace- or punctuation-only edit is dropped,
  - an edit whose diff is over **50%** of the entry → `needs_look`.
- **Three suspect days in a row** → a "source broken" health item. A 404 or 410 → `gone`, reported once.
- **Confirmation:** re-fetch about **10 minutes** later. The new or edited entry must appear identically (same key and body hash). The delay is set by `RW_CONFIRM_DELAY_SECONDS`, default 600, and is 0 in tests.
- **Teams:**
  - the envelope is `{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":<card>}]}`,
  - one card per item,
  - split an entry over **20 KB** into numbered parts, **up to 3 cards**; past that, send the summary, part 1 and a link to the web event page,
  - Adaptive Card text uses basic Markdown only: headings become bold lines and tables become plain rows, while the **words stay verbatim**.
- **Digest (08:00):**
  - it opens with a summary card holding the version lines, new and edited counts per vendor, and health,
  - then one card per new entry, then one per edited entry,
  - an empty day still sends the summary card.
- **Immediate messages:** a scheduled job that failed outright, and a last successful `watch` more than **26 h** old.
- **The webhook URL is a secret** (`RW_TEAMS_WEBHOOK_URL`). Never log it. If it is unset, `digest` fails loudly unless run with `--dry-run`.
- **Politeness:** one fetch per page per day, plus the confirmation re-fetch, under the existing per-host policy (2 concurrent requests, 1000 ms apart). Cap `Retry-After` at 120 s and response bodies at 15 MB.
- **Login-walled sources (AvePoint, Eon, Securiti.ai) are not fetched in Phase 2.** They appear in health as "waiting for login (phase 3)". Rubrik and Cohesity appear as "public sources only".
- **Never send DocExtractor anything but GET.** Never store or log secrets.

## Review Focus

1. **A page whose extractor output shifts slightly every day** (a "Last updated" line, rotating image tokens, a view counter) must not produce edited entries. Covered by `test_normalize_strips_tokens_and_noise` and `test_edit_whitespace_or_punctuation_only_is_dropped`.
2. **Two entries under the same heading text on one page** (e.g. two "Bug fixes" blocks under different dates when the splitter picks the wrong level, or the same date twice) must not collide or flip each day. Covered by `test_duplicate_keys_get_stable_suffixes`.
3. **A transient failure on the confirmation re-fetch** must defer the event to the next day, never send it and never lose it. Covered by `test_confirmation_fetch_failure_defers`.
4. **A digest that crashes half-way** must not resend already-sent cards the next day, and must not mark unsent cards as sent. Covered by `test_digest_marks_sent_per_card`.
5. **A version-pinned successor URL that answers 200 with a "page not found" shell** must not create a successor. Covered by `test_successor_rejects_thin_or_missing_version`.

---

### Task 1: Schema for the watch

**Files:**
- Create: `alembic/versions/0002_watch.py`
- Modify: `src/release_watch/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `WatchedSource` gains:
    - `follow: dict | None` (JSONB, the index/moving-target rule, Task 4),
    - `superseded_by: uuid | None`,
    - `suspect_streak: int` (default 0),
    - `last_ok_at: datetime | None`.
  - `SOURCE_STATE` gains `superseded`; `ORIGIN` gains `successor`.
  - New models:
    - `FetchAttempt(id, source_id, job_run_id, fetched_at, outcome, http_status, bytes, entries, detail)`, where outcome is the enum `fetch_outcome`: `baseline|same|changed|suspect|error|gone`.
    - `Snapshot(id, source_id, fetched_at, markdown, hash)`.
    - `Entry(id, source_id, key, heading, body, body_hash, url, published_at, first_seen, last_seen, removed_at)`, unique on `(source_id, key)`.
    - `EntryEvent(id, source_id, entry_id|None, kind, heading, body, diff, summary, url, product_de_id, version, dedupe_key unique|None, status, reason, created_at, decided_at, sent_at, cards_sent int default 0)`. Kind is the enum `event_kind`: `new|edited|version|moved`. Status is the enum `event_status`: `queued|needs_look|sent|dismissed`.
  - **Migration data step:** set `login_walled = true` on rows whose `label = 'blocked'` and `label_reason in ('login_redirect','session_redirect')`.

- [ ] **Step 1: Write the failing test.** Extend `test_schema_has_app_tables` to expect `fetch_attempt`, `snapshot`, `entry` and `entry_event`. Add `test_entry_key_unique_per_source`: inserting two `Entry` rows with the same `(source_id, key)` raises `IntegrityError`. Add `test_event_dedupe_key_unique`: the same for two events with one `dedupe_key`, while two `None` keys are fine. Add `test_migration_marks_login_walled`: a source inserted before upgrading to 0002, with `label='blocked', label_reason='login_redirect'`, is `login_walled` after the upgrade. Use the alembic downgrade/upgrade fixture pattern from `test_models.py`.
- [ ] **Step 2: Run** `uv run pytest tests/test_models.py -q`. Expected: FAIL (missing tables).
- [ ] **Step 3: Implement** the models and migration 0002. The downgrade drops the new tables, columns and enum types. Postgres can't remove enum values, so the downgrade leaves the added values in `source_state` and `source_origin`; add a comment saying so.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass, including the 0001↔0002 round-trip in `test_models.py`.
- [ ] **Step 5: Commit** `feat: watch schema (entries, events, fetch attempts, snapshots)`.

### Task 2: Extraction and normalisation

**Files:**
- Create: `src/release_watch/watch/__init__.py`, `src/release_watch/watch/extract.py`
- Modify: `pyproject.toml` (add `markdownify>=0.14`), `uv.lock`
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces:
  - `extract_markdown(html: str, url: str, selector: str | None = None) -> str`.
    - With a selector: take the first lexbor match's HTML, turned into Markdown with ATX headings by `markdownify`. An empty string if nothing matches.
    - Without one: trafilatura `output_format="markdown"`, `include_formatting=True`, `include_tables=True`, `include_links=True`, then the `<body>` text as a fallback (the existing behaviour in `classify.extract_text`, which now calls this).
  - `html_to_markdown(fragment: str) -> str`, using markdownify, for feed and JSON item bodies.
  - `normalize(text: str) -> str`, for comparison only:
    - strip query strings from URLs inside `](...)` and bare `http(s)://` URLs,
    - drop lines matching `NOISE_PATTERNS` (`last (updated|visited|modified)`, `\d+ views?`, `was this (article|page) helpful`, `print this page`), case-insensitively,
    - collapse whitespace,
    - casefold.
  - `body_hash(text: str) -> str`: the sha256 hex of `normalize(text)`.
  - `punct_insensitive(text: str) -> str`: `normalize` with all non-alphanumerics removed, used for the "punctuation-only edit" gate.

- [ ] **Step 1: Write the failing tests:**
  - `test_selector_extracts_only_that_node`: HTML with `<nav>`, `<div id="c"><h2>2026-10-01</h2><p>A</p></div>`; `extract_markdown(html, url, "#c")` gives a result starting `## 2026-10-01` that contains `A` and not the nav text.
  - `test_missing_selector_returns_empty`.
  - `test_no_selector_uses_trafilatura` (the `tests/fixtures/pages/changelog.html` fixture keeps its headings as `#` lines).
  - `test_normalize_strips_tokens_and_noise`: two texts differing only in `?sig=abc` vs `?sig=def` on an image URL and in a "Last updated: Oct 3" line have equal `body_hash`.
  - `test_punct_insensitive_ignores_punctuation`: `"Fixed bug."` vs `"Fixed bug"`.
- [ ] **Step 2: Run** `uv run pytest tests/test_extract.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `watch/extract.py`, and switch `discovery/classify.extract_text` to call `extract_markdown(html, url)`.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: body extraction and comparison normalisation`.

### Task 3: Entry splitting

**Files:**
- Create: `src/release_watch/watch/split.py`
- Test: `tests/test_split.py`, `tests/fixtures/pages/` (add `rolling.md`, `versions.md` and `bold_dates.md`)

**Interfaces:**
- Produces:
  - `@dataclass Entry(key: str, heading: str, body: str, url: str | None = None, published_at: datetime | None = None)`. `body` is verbatim Markdown, including the heading line.
  - `DATE_RE` matches ISO dates, `October 28, 2026`, `28 October 2026`, `Oct 2026` and `2026-10`.
  - `VERSION_RE` matches `v?\d+(\.\d+){1,3}([-.]\w+)?`, plus `\d{2}-\d` dashed versions.
  - `split_entries(markdown: str, rule: dict | None = None) -> list[Entry]`. Default algorithm:
    1. Heading candidates are ATX headings (`#`..`####`) and whole-line bold (`**…**`, which counts as level 5).
    2. For each level, count the headings that contain a `DATE_RE` or `VERSION_RE` match.
    3. Choose the level with the most matches (at least 1; ties go to the higher level, i.e. fewer `#`).
    4. Split there. Each chosen heading starts an entry that runs to the next heading at that level or above. Headings at the chosen level without a date or version still start an entry.
    5. Text before the first entry is ignored.
  - Rule overrides:
    - `{"heading_level": n}` forces the level.
    - `{"heading_regex": "..."}` picks entry headings by regex.
    - `{"whole": true}` makes the page a single entry keyed by the `rule["key"]` or `"page"`.
  - `entry_key(heading: str) -> str` is the normalised heading text without `#` or `*`. Duplicate keys on one page get ` (2)`, ` (3)` and so on, in document order.

- [ ] **Step 1: Write the failing tests:**
  - `test_splits_on_date_headings` (`rolling.md`: `# Release notes`, then three `## October 28, 2026`-style sections with `###` subsections). Expect 3 entries, newest first; the keys are the dates; each body includes its `###` subsections.
  - `test_prefers_level_with_most_dated_headings`: one `##` heading with a date, five `###` headings with versions → splits at `###`.
  - `test_bold_line_dates_split`: `bold_dates.md`, where dates are `**2026-10-01**` lines.
  - `test_no_dated_headings_gives_zero_entries`.
  - `test_duplicate_keys_get_stable_suffixes`: the same heading twice gives keys `x` and `x (2)`.
  - `test_whole_rule_returns_one_entry`.
  - `test_heading_level_override`.
- [ ] **Step 2: Run** `uv run pytest tests/test_split.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `watch/split.py`.
- [ ] **Step 4: Run** `uv run pytest tests/test_split.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: entry splitting on date and version headings`.

### Task 4: Source readers (entry pages, sections, feeds, JSON, index pages, version pages)

**Files:**
- Create: `src/release_watch/watch/readers.py`, `src/release_watch/watch/rules.py`
- Modify: `src/release_watch/fetch.py` (Retry-After cap, body cap, a test for cookies across redirects)
- Test: `tests/test_readers.py`, `tests/test_rules.py`, `tests/test_fetch.py`

**Interfaces:**
- Consumes: `Fetcher.fetch(url) -> FetchResult` (existing), `extract_markdown`, `html_to_markdown`, `split_entries`, `Entry`.
- Produces:
  - `@dataclass ReadResult(outcome: Literal["ok","suspect","error","gone"], reason: str, entries: list[Entry], size: int, markdown: str, final_url: str, http_status: int | None)`.
  - `async read_source(source: WatchedSource, fetcher) -> ReadResult`. Kind dispatch:
    - `entry_page` / `section`: `extract_markdown(text, url, source.selector)` → `split_entries(md, source.split_rule)`. If `split_rule` has a `json` key, use `json_entries` instead.
    - `feed`: feedparser. Each item becomes `Entry(key=item.id or item.link, heading=title, body="## "+title+"\n\n"+html_to_markdown(content or summary), url=link, published_at)`.
    - `index_page`: `children(...)` from `rules.py` using `source.follow`. Each child becomes `Entry(key=normalize_url(child.url), heading=child.title, body="", url=child.url)`. Version pages (`follow.pick.type == "versions"`) give `Entry(key=version, heading=version, body="")`.
    - Outcome mapping: fetch `blocked` → `suspect` with the reason; `gone` → `gone`; `error` → `error`; a PDF → `suspect`/`pdf_unparsed`.
  - `rules.py`:
    - `@dataclass Child(url: str, title: str, number: float | None)`.
    - `pick_children(body: str, content_type: str, base_url: str, pick: dict) -> list[Child]`. Pick types:
      - `links`: `selector` (CSS, default `a`), optional `href_regex` and `text_regex`, document order; relative hrefs are resolved.
      - `sitemap`: `loc_regex` over `<loc>` values.
      - `json`: `items` (a dotted path; a `*` segment flattens lists), `url`, `title`, optional `order_by` and `order` (`asc|desc`), optional `url_template` (`{field}` substitution, for APIs that return ids).
      - `versions`: `regex` (one capture group) over the text, or over `json_path` values; returns the de-duplicated versions, newest first by `version_tuple`.
    - `newest(children: list[Child], how: "first"|"max_number") -> Child`: `max_number` uses `Child.number`, the first integer group of `number_regex` applied to the title or URL.
    - `async children(fetcher, url: str, follow: dict) -> tuple[FetchResult, list[Child]]`. It applies `follow["pick"]`. If `follow["then"]` is set, it takes `newest(children, follow.get("newest","first"))`, fetches that child, and applies `follow["then"]` recursively (two levels for Axcient and Zerto). It applies `follow.get("url_rewrite", [])`, a list of `[from, to]` pairs, to the child URLs.
    - `json_entries(text: str, rule: dict) -> list[Entry]` for `split_rule.json = {items, key, title, body, url, date, body_format: "html"|"text"}`.
    - `version_tuple(v: str) -> tuple[int, ...]`.
  - `fetch.py`: `MAX_RETRY_AFTER = 120`; a body over `MAX_BODY_BYTES = 15_000_000` gives outcome `error`/`too_large`.

- [ ] **Step 1: Write the failing tests:**
  - **test_rules.py:**
    - `test_links_pick_with_regex` (relative hrefs resolved).
    - `test_sitemap_pick` (Afi-style `<loc>` list with `/de/` copies excluded by the regex).
    - `test_json_pick_orders` (a Zoomin-style `subLinks` with `position`, `order_by=position asc`).
    - `test_two_level_then_follows_newest` (respx: a year page list → the highest year via `max_number` → that page's links).
    - `test_versions_pick_newest_first` (`8.2.1`, `8.2.2`, `8.1.0` in JSON at `taxonomy.versions` → `["8.2.2","8.2.1","8.1.0"]`).
    - `test_url_rewrite`.
    - `test_json_entries_zendesk` (an `articles` list with an HTML `body` becomes Markdown entries keyed by id).
  - **test_readers.py** (respx):
    - `test_entry_page_reader_splits`.
    - `test_feed_reader_items`, using `tests/fixtures/pages/feed.xml`.
    - `test_index_reader_children_keys_are_normalised_urls`.
    - `test_blocked_fetch_is_suspect`.
    - `test_pdf_is_suspect`.
  - **test_fetch.py:**
    - `test_cookies_kept_across_redirects`: respx route A sets a cookie and redirects to B, and B returns 200 only when the cookie is present.
    - `test_retry_after_capped`: `Retry-After: 99999` sleeps at most 120 s; patch `asyncio.sleep` to record the value.
    - `test_body_too_large`.
- [ ] **Step 2: Run** `uv run pytest tests/test_rules.py tests/test_readers.py tests/test_fetch.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `rules.py`, `readers.py` and the fetch caps.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: source readers and newest-child rules`.

### Task 5: Compare, diff and gates

**Files:**
- Create: `src/release_watch/watch/compare.py`
- Test: `tests/test_compare.py`

**Interfaces:**
- Produces:
  - `@dataclass Known(key: str, body_hash: str, body: str)`.
  - `@dataclass Changes(new: list[Entry], edited: list[tuple[Entry, Known]], unchanged: int, removed: list[str])`.
  - `compare(known: list[Known], fresh: list[Entry]) -> Changes`. The rules:
    - a new key whose `body_hash` isn't among the known hashes → `new`;
    - a known key with a different hash → `edited`, unless `punct_insensitive` of both bodies is equal, in which case it is dropped;
    - a new key with a known hash → unchanged;
    - known keys missing from `fresh` → `removed`.
  - `edit_diff(old: str, new: str) -> tuple[str, float]`: a unified line diff (`difflib`, n=1) and its changed ratio, `1 - SequenceMatcher(None, old_lines, new_lines).ratio()`.
  - `page_suspect(prev_size: int | None, prev_entries: int, result: ReadResult) -> str | None`:
    - `"shrunk"` when `result.size < 0.2 * prev_size`,
    - `"no_entries"` when the page had entries and now has zero,
    - otherwise the `result.reason` when the outcome is suspect.
  - `gate(changes: Changes, max_new: int = 3, max_edit_ratio: float = 0.5) -> list[tuple[str, Entry, Known | None, str | None, str | None]]`. Each tuple is `(kind, entry, known, status, reason)`: status `needs_look` with reason `too_many_new` when `len(new) > max_new` (applied to all of them), and `edit_too_large` per edit over the ratio. Otherwise `queued`.

- [ ] **Step 1: Write the failing tests:**
  - `test_new_entry_detected`.
  - `test_reordered_heading_same_body_not_new`.
  - `test_edited_entry_detected_with_diff`.
  - `test_edit_whitespace_or_punctuation_only_is_dropped`.
  - `test_more_than_three_new_go_to_needs_look` (4 new → all `needs_look`/`too_many_new`; 3 new → all `queued`).
  - `test_large_edit_needs_look`.
  - `test_page_suspect_shrunk_and_no_entries`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_compare.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: entry comparison, diffs and plausibility gates`.

### Task 6: The watch run

**Files:**
- Create: `src/release_watch/watch/run.py`, `src/release_watch/watch/summary.py`
- Modify: `src/release_watch/config.py` (`confirm_delay_seconds: int = 600`, `teams_webhook_url: SecretStr = SecretStr("")`, `web_base_url: str = "http://release-watch.k3s.home.lan"`), `src/release_watch/cli.py` (`watch` command)
- Test: `tests/test_watch_run.py`, `tests/test_summary.py`

**Interfaces:**
- Consumes: `read_source`, `compare`, `gate`, `page_suspect`, `edit_diff`, `body_hash`, the models from Task 1, `LLMClient.complete_json`.
- Produces:
  - `async summarize(llm, vendor: str, heading: str, text: str) -> str | None`. The prompt asks for JSON `{"summary": "<one sentence, at most 25 words>"}`. Returns `None` on `LLMError` or a bad reply. Text is truncated to 12,000 characters for the prompt only.
  - `async run_watch(session_factory, fetcher, llm, *, confirm_delay: float, now: Callable[[], datetime] = utcnow, sleep=asyncio.sleep, only: str | None = None, dry_run: bool = False) -> dict[str, int]`. The steps:
    1. Select `state='active' AND NOT login_walled`, optionally filtered by `only` (a substring of the URL or vendor).
    2. Read every source with at most 8 in parallel (per-host limits still apply).
    3. For each source:
       - `gone` → state `gone`, plus a `fetch_attempt`.
       - `error` → a `fetch_attempt`; the suspect streak is unchanged.
       - Suspect (reader outcome or `page_suspect`) → `suspect_streak += 1`.
       - Otherwise `suspect_streak = 0` and `last_ok_at = now`. With no stored entries this is a **baseline**: insert all entries and a snapshot, with no events. Else `compare` + `gate`.
    4. **Confirmation:** sources with any `new` or `edited` result are re-read after `confirm_delay`, measured from their first read. An event is kept only if its key and body hash are identical in the re-read. If the re-read is not `ok`, the source's changes are dropped for today: no entries are updated, so tomorrow detects them again.
    5. Persist the confirmed changes:
       - upsert `Entry` rows (new ones; edited bodies and hashes; `last_seen`; `removed_at` for removed keys),
       - insert `EntryEvent` rows (`new`/`edited`, the status and reason from `gate`, the verbatim `body`, the `diff` for edits, `url` = the entry URL or the source URL),
       - insert a snapshot.
    6. **Index pages:** for each new child:
       - `follow.mode == "entry"`: fetch the child, `extract_markdown`, then a `new` event whose heading is the child title and body is the child's full Markdown. A child fetch failure gives `needs_look` with reason `child_fetch_failed`.
       - `follow.mode == "version"` (the default): a `version` event with the version taken from `VERSION_RE` on the title, then the URL. Its `dedupe_key` is `f"version:{vendor}:{product_de_id or source.id}:{version}"`; on a conflict, skip. Suppress it when the product's `de_version` compares `>=` by `version_tuple`. No version found → `needs_look`/`no_version`.
    7. Version pages (`pick.type == "versions"`) produce `version` events the same way, but only for the newest version, and only when it is newer than every stored key.
    8. Summaries: call `summarize` for each `new`/`edited` event, writing the event's `summary`. A failure leaves it `None`.
    9. Every source gets one `fetch_attempt`.
    - Returns counts `{sources, baseline, same, changed, suspect, error, gone, events_queued, events_needs_look}`.
    - `dry_run=True` reads and prints per source (outcome, entry count, first 3 keys) and writes nothing.
  - CLI: `release-watch watch [--only TEXT] [--dry-run]`, wrapped in `_run("watch", ...)`. Dry-run output goes to stdout, one line per source.

- [ ] **Step 1: Write the failing tests** (real Postgres, respx, a fake LLM returning `{"summary": "Adds X."}`):
  - `test_first_run_is_baseline_no_events`.
  - `test_new_entry_creates_queued_event_with_verbatim_body_and_summary`: on day 2 the page has one more dated section; the event body equals that section's Markdown exactly, and `summary == "Adds X."`.
  - `test_confirmation_mismatch_drops_event`: respx returns a different body on the re-read.
  - `test_confirmation_fetch_failure_defers`: the re-read returns 503; there is no event and no entry row for the new key; the next run with a good fetch creates the event.
  - `test_shrunk_page_is_suspect_and_streak_counts`.
  - `test_gone_marks_source_gone`.
  - `test_login_walled_sources_skipped`.
  - `test_index_entry_mode_sends_child_verbatim`.
  - `test_index_version_mode_dedupes_and_suppresses_known_version`: a product with `de_version` `11.46` and a new child `11.44` → no event; a new child `11.48` → one `version` event; a second source with the same product and version → no second event.
  - `test_summary_failure_keeps_event`.
  - `test_dry_run_writes_nothing`.
  - **test_summary.py:** `test_summarize_returns_sentence`, `test_summarize_none_on_error`.
- [ ] **Step 2: Run** `uv run pytest tests/test_watch_run.py tests/test_summary.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `summary.py`, `run.py`, the config fields and the CLI command (the `make_fetcher` and `make_llm` factories are reused).
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: daily watch run with confirmation, gates and summaries`.

### Task 7: Successors for version-pinned URLs

**Files:**
- Create: `src/release_watch/watch/successor.py`
- Modify: `src/release_watch/watch/run.py` (call it after step 7 of `run_watch`)
- Test: `tests/test_successor.py`

**Interfaces:**
- Consumes: `follow.pin = {"version": str, "signal": <url of a version page or index source>, "successor": "<url template with {version}, {version_major}, {version_compact}>"}`, the stored entries of the signal source, `Fetcher`, `extract_markdown`.
- Produces: `async propose_successors(session_factory, fetcher, now) -> dict[str, int]`. For each active source with `follow.pin`:
  1. The signal is the active source whose `url_key == normalize_url(pin.signal)`.
  2. Take the newest of its entry keys by `version_tuple`. If it isn't newer than `pin.version`, do nothing.
  3. Build the URL from the template and fetch it. It is **accepted** only if all of these hold:
     - the fetch outcome is `ok`,
     - it is not a PDF unless the old URL was one,
     - the extracted text is ≥ 200 characters,
     - the extracted text contains the new version string (or, for `{version_major}`, the major).
  4. **Accepted:**
     - insert a `WatchedSource` copied from the old one, with the new `url`/`url_key`, `follow.pin.version` = the new version, `origin='successor'`, `state='active'`, `confirmed_by='successor'` and `title` = the old title with the version swapped,
     - set the old source's `superseded_by` to the new id (the old source stays `active` until the new one has a baseline; `run_watch` marks it `superseded` when the successor's baseline is written),
     - add a `moved` event, `queued`, with heading `f"Release-notes URL for {product or vendor} moved to {version}"` and url the new URL, deduped by `dedupe_key = f"moved:{old.id}:{version}"`.
  5. **Rejected:** a `moved` event in `needs_look` with reason `successor_unverified` and the tried URL, deduped the same way.

- [ ] **Step 1: Write the failing tests:**
  - `test_successor_created_when_signal_newer`.
  - `test_successor_rejects_thin_or_missing_version` (a 200 with a 150-character "Page not found" → `needs_look`, no new source).
  - `test_no_successor_when_signal_not_newer`.
  - `test_old_source_superseded_after_successor_baseline` (two `run_watch` calls).
  - `test_moved_event_deduped`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: propose and verify successors for version-pinned release-notes URLs`.

### Task 8: Health

**Files:**
- Create: `src/release_watch/health.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Produces:
  - `@dataclass Health(checked: int, total: int, failed: list[str], suspect: list[str], broken: list[str], gone_new: list[str], waiting_login: dict[str,int], public_only: list[str], needs_look: int, needs_look_stale: int, sync_error: str | None, last_watch_ok: datetime | None, stale_jobs: list[str])`.
  - `compute_health(session, now: datetime) -> Health`:
    - `checked` = sources with a non-error `fetch_attempt` in the latest `watch` run window; `total` = active, not login-walled sources.
    - `failed` = error attempts. Each list holds `"<vendor>: <title or url>"` strings.
    - `broken` = `suspect_streak >= 3`.
    - `gone_new` = sources that went `gone` since the last digest (the last `digest` job_run `ok`).
    - `waiting_login` = active login-walled sources per vendor.
    - `public_only` = the constant `["Rubrik", "Cohesity"]`.
    - `needs_look_stale` = `needs_look` events older than 3 days.
    - `sync_error` = the error of the latest `sync` run if it failed.
    - `stale_jobs` = `job_run` rows `running` for more than 6 h. They are marked `failed` with error `stale: never finished`; this fixes the Phase 1 deferred minor.

- [ ] **Step 1: Write the failing tests:**
  - `test_health_counts_checked_of_total`.
  - `test_broken_after_three_suspect_days`.
  - `test_waiting_login_grouped_by_vendor`.
  - `test_needs_look_stale_after_three_days`.
  - `test_stale_running_job_marked_failed`.
  - `test_last_watch_ok`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_health.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: health summary`.

### Task 9: Teams cards and the digest

**Files:**
- Create: `src/release_watch/notify/__init__.py`, `src/release_watch/notify/cards.py`, `src/release_watch/notify/teams.py`, `src/release_watch/notify/digest.py`
- Modify: `src/release_watch/cli.py` (the `digest [--dry-run]` command, plus the immediate failure message in `_run`)
- Test: `tests/test_cards.py`, `tests/test_digest.py`

**Interfaces:**
- Produces:
  - **`cards.py`:**
    - `card_markdown(md: str) -> str`: `#` headings become `**heading**` lines; table rows become `cells joined by " | "` with separator rows dropped; images become `[image](url)`; the words are unchanged.
    - `split_parts(text: str, limit: int = 20_000) -> list[str]`: splits on paragraph boundaries, and hard-splits only a paragraph longer than the limit.
    - `entry_cards(event, vendor: str, product: str | None, web_base_url: str) -> list[dict]`:
      - up to 3 cards titled `"<vendor> · <product>: <heading> (i/n)"`, with the summary on the first card and a link to `event.url`;
      - when there are more than 3 parts: the summary, part 1, and the text `"Full entry: <web_base_url>/events/<id>"`;
      - edited events use the diff in a monospace block, not the body.
    - `summary_card(health: Health, versions: list[str], counts: dict[str, dict[str,int]], now) -> dict`: the version lines, then new and edited counts per vendor, then health. An empty day reads `"No changes; N of M sources checked"`.
    - `envelope(card) -> dict`: the Global Constraints envelope; the card is Adaptive Card schema 1.4 with `TextBlock(wrap=True)`.
  - **`teams.py`:**
    - `class TeamsError(Exception)`.
    - `class Teams` with `__init__(url: str, transport=None)` and `async post(card: dict) -> None`. It raises `TeamsError` on a non-2xx response after one retry (5 s later) on 429 or 5xx. **The message never contains the URL.**
  - **`digest.py`:**
    - `async run_digest(session_factory, teams: Teams | None, *, web_base_url: str, now: datetime, dry_run: bool = False, out=print) -> dict`.
    - It builds the cards from `queued` events (oldest first): the summary card first, then `version`/`moved` lines inside the summary, then `new` cards, then `edited` cards.
    - It posts sequentially, and after **each** event's cards it sets `status='sent'`, `sent_at` and `cards_sent`, committing per event.
    - When `last_watch_ok` is more than 26 h old, it sends an extra first card: `"Release Watch: last successful watch was <age> ago"`.
    - `dry_run` prints the card JSON and changes nothing.
    - A `TeamsError` stops the run; the remaining events stay `queued`, and the job fails.
  - `_run` failure path: when `teams_webhook_url` is set, post `"Release Watch job <job> failed: <error type>"`, swallowing any `TeamsError` raised by that post.

- [ ] **Step 1: Write the failing tests:**
  - **test_cards.py:**
    - `test_card_markdown_keeps_words` (every word of the input appears in order in the output; headings become bold).
    - `test_split_parts_three_max_then_link` (a 70 KB entry gives 2 cards: part 1 plus the link card text).
    - `test_entry_cards_numbered_parts` (a 45 KB entry → 3 cards `(1/3)`..`(3/3)`).
    - `test_summary_card_empty_day`.
    - `test_envelope_shape`.
  - **test_digest.py** (respx for the webhook):
    - `test_digest_sends_summary_then_entries`.
    - `test_digest_marks_sent_per_card`: the webhook fails on the 3rd POST; the first event is `sent`, the rest stay `queued`; the job raises.
    - `test_digest_needs_look_not_sent`.
    - `test_digest_stale_watch_alert`.
    - `test_digest_dry_run_changes_nothing`.
    - `test_webhook_url_never_logged`: caplog during a failing post doesn't contain the URL.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the modules and the CLI.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: Teams Adaptive Cards and the daily digest`.

### Task 10: Web: needs a look, event page, source health

**Files:**
- Modify: `src/release_watch/web/app.py`, `src/release_watch/web/templates/base.html`
- Create: `src/release_watch/web/templates/needs_look.html`, `event.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces:
  - `GET /needs-look` lists the `needs_look` events: vendor, kind, heading, reason, age, summary, and a link to `/events/{id}`.
  - `POST /events/decide` takes `ids`, `decision` (`send` → `queued`, picked up by the next digest; `dismiss` → `dismissed`) and `back`.
  - `GET /events/{id}` shows the full verbatim body (rendered as `<pre>` text, escaped), the diff for edits, and the summary and source link.
  - The nav gets "Review queue · Needs a look (N)".
  - It also fixes the Phase 1 web minors this touches: an invalid `state`/`label` filter gives a 400, not a 500; `back` must start with `/` and not `//` or `/\`.

- [ ] **Step 1: Write the failing tests:**
  - `test_needs_look_lists_events`.
  - `test_send_moves_to_queued`.
  - `test_dismiss`.
  - `test_event_page_shows_verbatim_escaped` (a body containing `<script>` is escaped).
  - `test_invalid_filter_400`.
  - `test_back_backslash_rejected`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: needs-a-look queue and event pages`.

### Task 11: Seed source configuration (selectors, split rules, follow rules)

**Files:**
- Modify: `src/release_watch/discovery/seeds.py`, `seeds/sources.yaml`
- Test: `tests/test_seeds.py`

**Interfaces:**
- Produces: a top-level `source_config:` list in the seeds YAML, with entries `{url, selector?, split_rule?, follow?, kind?}`.
  - `load_seeds` applies each entry to the existing `watched_source` with that `url_key` in any state except `rejected`, setting only the given fields.
  - An unknown URL raises `ValueError("source_config entry N: no source with url …")`.
  - A `follow` must have `pick.type` in `{links, sitemap, json, versions}`.
  - The counts gain `configured`.

- [ ] **Step 1: Write the failing tests:**
  - `test_source_config_applies_fields`.
  - `test_source_config_unknown_url_raises`.
  - `test_source_config_bad_pick_type_raises`.
  - `test_source_config_skips_rejected`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: per-source watch configuration in seeds`.

### Task 12: Live tuning against every active source

**Files:**
- Modify: `seeds/sources.yaml` (`source_config` entries)

This is discovery step 5 (spec section 10): every active, not login-walled source must give sensible entries before it is watched.

- [ ] **Step 1:** Refresh the dev DB from the cluster DB (`pg_dump` from the cluster pod, restored into `rw-dev-pg`), then run `uv run release-watch migrate`.
- [ ] **Step 2:** Run `uv run release-watch watch --dry-run > <workspace>/dry1.txt` against the dev DB and read the results.
- [ ] **Step 3:** For each source with 0 entries, a single entry spanning the whole page, or keys that aren't dates or versions, inspect the page and add a `source_config` entry:
  - a selector,
  - a `heading_level`/`heading_regex`,
  - a `json` split rule for the JSON endpoints (CloudAlly, NinjaOne, Synology, Gearset, VDC),
  - `follow` rules for the index and moving-target sources (Afi sitemap, Axcient two-level, Druva hub links, Zerto two-level JSON with `url_rewrite`, the Acronis documentation index with `versions`, the IBM Docs API with `versions` at `taxonomy.versions`, the Red Hat lifecycle API, the Atempo lifecycle tables, the Dell hubs, the Backup Exec `data-items`),
  - `pin` rules for the version-pinned sources (Acronis 17, the IBM `/8.2.1` etc. docs URLs, Red Hat OCP 5.0, Atempo).
- [ ] **Step 4:** Re-seed and re-run the dry run until each source either gives plausible entries, or is listed in the final report as unsupported in Phase 2 (PDF, needs a browser, JS-only), with the reason.
- [ ] **Step 5: Commit** `seeds: watch configuration for active sources`.

### Task 13: Deploy

**Files:**
- Modify: `deploy/helm/release-watch/templates/` (add `cronjob-watch.yaml` at `0 6 * * *` and `cronjob-digest.yaml` at `0 8 * * *`, both `concurrencyPolicy: Forbid`; the `RW_TEAMS_WEBHOOK_URL` secret key, optional), `values.yaml`, `README.md` (the Power Automate flow: trigger "When a Teams webhook request is received", action "Post card in a chat or channel" with the attachments' card content; and how to add the URL to the Secret)
- Test: `helm lint`, `helm template` output checked for the two CronJobs

- [ ] **Step 1:** Add the templates, values and README section. Run `helm lint deploy/helm/release-watch` and `helm template … | grep -c 'kind: CronJob'`. Expected: 4.
- [ ] **Step 2:** Commit `deploy: watch and digest CronJobs`, push, and wait for CI to go green.
- [ ] **Step 3:** Run `helm upgrade`. Apply the seeds' `source_config` to the cluster DB (`release-watch seed` through a port-forward). Run `kubectl create job --from=cronjob/release-watch-watch watch-baseline` and check its counts: mostly `baseline`, and `events_queued` 0.
- [ ] **Step 4:** Run a `digest --dry-run` job and check the summary card JSON shows health with "checked N of M".
