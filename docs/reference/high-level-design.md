# Release Watch — High-level design

Nov 3, 2026 · @Everton Cardoso

## Goals, non-goals and constraints

Release Watch is a small, separate service on the homelab k3s cluster. Every morning it tells you two things: which vendor release-notes pages changed since yesterday, and which products have a newer release than the version DocExtractor tracks.

**Goals**

1. Check every confirmed release-notes page once a day and report real content changes, with a readable diff.
2. Detect daily when a product ships a version newer than its DocExtractor `version`, with the evidence, so you can decide on a bump.
3. Send one daily digest. Send an immediate alert only for a high-confidence new version.
4. Never let a silent failure look like "nothing changed": report pages that failed and runs that didn't happen.

**Non-goals**

- Bumping versions or triggering crawls. Release Watch recommends; a person acts in DocExtractor.
- Storing documentation. DocExtractor remains the archive; Release Watch keeps only the text of watched pages.
- Watching non-release-notes pages for change.

**Constraints**

- Its own namespace, database and deployment. Its only link to DocExtractor is the HTTP API, with its own key.
- No use of the shared Firecrawl or Browserless. If a browser is needed, Release Watch runs its own.
- About 700 to 800 watched pages at one fetch per page per day, polite per-host concurrency.
- DocExtractor is used for the catalog only, through a read-only key: what to watch and each product's current version. Release Watch never asks DocExtractor to fetch, probe or change anything, and never uses DocExtractor's stored pages to decide whether something changed.

## Architecture

DocExtractor is the catalog and archive; Release Watch owns the daily fetching, the change state and the notifications.

&#91;embedded content: Release Watch architecture · own namespace, DocExtractor reached only through its API\]

DocExtractor is consulted for one thing, the catalog. Every vendor page and every version probe is fetched by Release Watch itself, so Release Watch keeps working, and keeps its own judgment, whatever state DocExtractor is in.

## Monitor 1: release-notes watch

A page is reported as changed only when its normalised article text differs from the last good fetch, and a re-fetch confirms it.

**Discovery (weekly, plus on demand):**

1. Sync the catalog from DocExtractor: vendors, products, sources and versions.
2. Propose candidate pages using the briefing's heuristic: source names, page titles and URLs matching release notes, what's new, readme or changelog, plus TOC branches with those names.
3. Put the candidates in a review queue. A person confirms or rejects each one, once; rejections are remembered.
4. Add hand-entered pages for the 12 vendors DocExtractor has no release notes for. A page can be a web page, an RSS or Atom feed, or a GitHub releases feed.

**Daily check, for each confirmed page:**

1. **Choose the fetch mode** from the host policy:
   - `direct` for public sites, using plain HTTP with the host's User-Agent and concurrency settings.
   - `browser` for Akamai-gated sites, using Release Watch's own headless Chromium.
   - `session` for login-walled pages, only if Release Watch is given its own vendor accounts. Without them those pages are not watched, and the digest lists them as such.
2. **Extract the article body**, using the CSS selector DocExtractor's profile uses for that platform (for example `#articleContent` for Document360 or `article` for Flare). Otherwise use a generic readability extractor.
3. **Normalise the text.** Collapse whitespace and drop the query strings from image and link URLs, so re-minted tokens don't count as changes. Remove per-page noise patterns ("Last visited", view counters).
4. **Hash the text and compare** with the last good snapshot.
5. **If different, re-fetch once** about 10 minutes later. Record a change only if the second fetch matches the first, so A/B tests and half-deployed pages don't alert.
6. **Store the snapshot and diff**, extract any version strings that appear in the **added** lines (these feed monitor 2), and queue the change for the digest.

**Noise and failure handling:**

- **Suspect pages:** a fetch under 20% of the previous size, a redirect to a login URL, or a known block page is recorded as `suspect`, never as `changed`. Three suspect days in a row raise a "page broken" item.
- **Whitespace-only diffs:** a diff with no word-level change is dropped.
- **Removed pages:** a 404, or a redirect to an unrelated page, marks the page `gone` and reports it once.
- **Baseline:** the first fetch is the baseline and is never reported as a change.

## Monitor 2: new-version detection

A "bump due" alert fires only when there is evidence that a newer release actually exists. A guessed URL that happens to resolve is not enough by itself, and neither is a search result.

Each product gets a **version profile**:

- **The version format,** inferred from the current value and editable. `11.46` is dotted numeric, `25-1` is dashed, `5.2.1-1025` is a version plus a build number, `2026.2.1` is calendar-style.
- **A comparator** for that format, used to decide "newer".
- **An ordered list of evidence strategies,** below.

| Strategy | How it works | Applies to | Confidence |
| --- | --- | --- | --- |
| A. Release-notes evidence | A version string newer than the tracked one appears in the added lines of a watched release-notes page (monitor 1) | Every product with watched release notes | High when the heading names the product ("What's new in 11.48"); medium when it's a passing mention |
| B. Vendor version index | Parse a page or feed that lists releases: a docs version picker, a downloads page, the GitHub releases API (Velero), an RSS feed | Products where such an index exists; configured by hand | High |
| C. Template probe | Generate the next few candidate versions (next patch, next minor, next major in the product's format) and check whether the templated URL exists | The 21 products with templated sources | High when it returns real content; ignored when ambiguous |
| D. Web search | Query the in-cluster SearXNG for "\<product> \<candidate version> release" | Fallback only | Low, never alerts alone |

**Template probe details.** Candidates come from bumping each component: `11.46` gives `11.47`, `11.48` and `12.0`; `25-1` gives `25-2` and `26-1`. At most 3 candidates per product per day.

- Probe `{version}`-only templates directly with a GET. Count a URL as "exists" only when the body is real content: reject soft-404 shells (Cohesity answers 200 with `ERROR_FALLBACK;404`), login redirects and block pages.
- Skip templates whose next URL can't be predicted: Cohesity's `{rev}` builds (which are also login-walled) and Dell's PDF manual ids. Those products rely on strategies A and B.

**Combining evidence.** Evidence is grouped per (product, version) into a **candidate** with a state:

1. **Open:** some evidence, below the alert threshold.
2. **Alerted:** high confidence. One immediate alert, then a reminder in the digest each week.
3. **Resolved:** DocExtractor's `version` reached or passed it. This is detected on catalog sync and closes the candidate automatically.
4. **Dismissed:** a person said no (for example a beta, or a version you don't track). It stays dismissed.

**Products without a version.** 87 of the 109 products have no tracked version. For these, Release Watch records the newest version seen in their release notes and reports it as "new release", with no bump recommendation. A person can then decide to start tracking a version in DocExtractor.

## Data model

Release Watch keeps its own small PostgreSQL database. DocExtractor ids are stored as plain references, never as foreign keys across stacks.

| Table | One row per | Key columns |
| --- | --- | --- |
| `catalog_product` | DocExtractor product (mirror, refreshed daily) | `de_product_id`, `vendor_name`, `name`, `de_version`, `synced_at` |
| `version_profile` | Tracked product | `de_product_id`, `format` (regex), `comparator`, `strategies` (JSON), `index_url`, `index_parser` |
| `watched_page` | Confirmed release-notes page | `de_article_id` (nullable for hand-added), `de_source_id`, `de_product_id`, `url`, `fetch_mode`, `selector`, `state` (candidate / active / rejected / gone), `confirmed_by` |
| `host_policy` | Vendor host | `host`, `user_agent`, `max_concurrency`, `min_delay_ms`, `fetch_mode`, `soft_404_markers` |
| `snapshot` | Successful fetch whose text changed (plus the baseline) | `page_id`, `fetched_at`, `text_hash`, `text`, `http_status`, `bytes` |
| `fetch_attempt` | Every fetch | `page_id`, `at`, `outcome` (same / changed / suspect / error / gone), `detail` |
| `change_event` | Confirmed change | `page_id`, `detected_at`, `from_snapshot`, `to_snapshot`, `diff`, `versions_found[]`, `summary`, `digested_at` |
| `version_candidate` | (product, version) | `de_product_id`, `version`, `state`, `confidence`, `first_seen`, `alerted_at`, `resolved_at` |
| `version_evidence` | Piece of evidence | `candidate_id`, `strategy` (A–D), `url`, `excerpt`, `observed_at` |
| `job_run` | Scheduled run | `job`, `started_at`, `finished_at`, `pages_checked`, `errors`, `status` |

Storage stays small. About 800 pages at roughly 20 KB of text each, with a snapshot only on change, is well under 1 GB a year.

## Notifications

The default is one Slack digest each morning, plus an immediate message only for a high-confidence new version. Slack is the channel DocExtractor already notifies on; email can be added the same way.

**Daily digest (08:00), in this order:**

1. **Version bumps due:** product, current version, new version, the strongest evidence link, and a link to the product in DocExtractor.
2. **New releases seen** for products without a tracked version.
3. **Release notes changed:** vendor and product, page title linked to the live page, a 3-line excerpt of the added text, and a link to the full diff in Release Watch's small web view.
4. **Health:** "Checked 742 of 748 pages", with the pages that failed or look suspect, and any DocExtractor sync error.

An empty digest is still sent ("No changes; 748 pages checked"), so silence always means the job didn't run. A missing digest is itself the alarm.

**Immediate alerts:** a version candidate reaching `alerted`, and a daily job that failed outright.

**Optional summary.** A cheap LLM call (the OpenRouter account DocExtractor already uses) can turn each diff into one sentence, such as "11.48 released: adds Azure Blob immutability; fixes 14 issues". Show it beside the excerpt, never instead of it.

## Deployment and operations

Release Watch is one Python codebase with a Helm chart in its own repository, deployed to a new `release-watch` namespace with the same push, CI, then `helm upgrade` flow DocExtractor uses.

| Component | Kind | Notes |
| --- | --- | --- |
| `web` | Deployment, 1 replica | FastAPI: the review queue (confirm or reject candidate pages, dismiss versions), the diff viewer, and `/healthz`. Ingress at `release-watch.k3s.home.lan` |
| `sync` | CronJob, daily 05:00 | Catalog mirror; closes resolved version candidates |
| `discover` | CronJob, Mondays 05:30 | Proposes new release-notes pages |
| `watch` | CronJob, daily 06:00 | Monitor 1, then monitor 2. Expected to take well under an hour at about 2 pages per second across hosts |
| `digest` | CronJob, daily 08:00 | Builds and sends the digest from the database, so a slow `watch` never blocks it |
| `postgres` | StatefulSet, 1 GiB PVC | Own instance. Kasten is already in the cluster for backups |
| `chromium` | Optional Deployment | Only if `browser` fetch mode is needed (phase 4). Never the shared Browserless |

**Secrets** (Kubernetes Secrets, never in values files): the DocExtractor API key(s), the Slack webhook, and the OpenRouter key if summaries are on.

**Suggested stack:**

- Python 3.12 with httpx, selectolax or BeautifulSoup, and trafilatura for generic extraction.
- SQLAlchemy and Alembic for the database, with pytest tests.
- These mirror DocExtractor, so one person can maintain both.

**Operational checks:**

- Every CronJob writes a `job_run` row.
- The digest reports the last successful `watch`, and an immediate alert fires if it is more than 26 hours old.
- `concurrencyPolicy: Forbid` on each CronJob stops overlapping runs.

## Phased delivery

Each phase ships something usable on its own. No DocExtractor changes are needed at any phase.

1. **Catalog and discovery.** Scaffold the repository, chart and database. Build the catalog sync, candidate discovery and review queue. *Done when* you have confirmed the release-notes list.
2. **Release-notes watch (public pages).** Build the direct fetcher with host policies, normalisation, confirmation re-fetch, diffs, and the Slack digest with its health section. *Done when* a week of digests carries no false "changed" items.
3. **Version watch.** Add version profiles, strategy A (release-notes evidence) and strategy C (template probe), with candidate states and immediate alerts. *Done when* a known past bump, replayed, would have alerted. Commvault 11.44 to 11.46 is a good test.
4. **Coverage.** Add strategy B (vendor version indexes, starting with GitHub releases), an own headless browser for Akamai-gated hosts if still needed, login-walled pages if you give Release Watch its own vendor accounts, and optional LLM summaries.

## Risks and open questions

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Noisy diffs from page chrome or rotating tokens | Alert fatigue; the digest gets ignored | Body-only extraction, URL query stripping, confirmation re-fetch, word-level diff threshold |
| Vendor blocks our IP for daily fetching | Pages silently stop updating | One fetch per page per day; per-host policies; `suspect` and `broken` states in the digest |
| Version strings in release notes are ambiguous (dates, KB numbers, other products' versions) | False bump alerts | Per-product format regex; prefer versions in headings; one product's evidence never counts for another |
| Template probes cannot predict some URLs (Dell manual ids, Cohesity builds) | Missed bumps | Skip unpredictable templates; rely on strategies A and B |
| Login-walled release notes (AvePoint, Cohesity, Rubrik) are out of reach without vendor accounts | Changes there go unnoticed | Give Release Watch its own accounts, or watch public substitutes (vendor blogs, public PDFs). List them as "not watched" in the digest's health section |
| A DocExtractor schema change breaks the client | Sync fails | Pin to the documented fields; contract tests against `/openapi.json` in CI |

**Open questions for you:**

- [ ] Slack only, or also email?
- [ ] Who confirms the candidate release-notes list, and is a one-time review of about 700 candidates acceptable?
- [ ] Should Release Watch get its own logins for AvePoint, Cohesity and Rubrik, or should login-walled release notes stay out of scope?
- [ ] Do you want one-line LLM summaries in the digest (small OpenRouter cost)?
- [ ] Which of the 87 unversioned products should start tracking a version in DocExtractor?
