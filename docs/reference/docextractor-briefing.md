# DocExtractor — Briefing for the Release Watch developer

Nov 3, 2026 · @Everton Cardoso

## Summary

The DocExtractor API can serve as the **catalog** for Release Watch today with a read-only API key. It cannot serve as the **daily change signal**: it re-crawls each vendor only every 1 to 3 months.

Release Watch is a separate service with two daily jobs:

1. **Release-notes watch:** notice when a vendor's live release-notes page changes, and notify.
2. **Version watch:** notice when a vendor ships a product version newer than the one DocExtractor holds, meaning a "version bump" is due.

What DocExtractor gives you:

- Every vendor, product and source.
- The product version and URL template.
- The URL, title and last-known content of every page.
- Version history and diffs.
- A gap-free change feed.

What Release Watch must do itself:

- Fetch the live pages daily.
- Decide which pages count as release notes.
- Probe vendors for newer versions.

Release Watch is **notify-only and independent**. It reads DocExtractor's catalog to know what to watch, then fetches and compares live pages on its own. It never asks DocExtractor to fetch, probe or change anything, so it needs no DocExtractor changes.

One consequence is worth deciding early: **login-walled release notes** (AvePoint, Cohesity, Rubrik) can be watched only if Release Watch gets its own vendor accounts. DocExtractor's sessions are not shared.

## DocExtractor in one page

DocExtractor crawls vendor documentation sites (backup and data-protection vendors) and stores every page as Markdown in the vendor's own table-of-contents (TOC) order. It keeps a version history of each page and exposes all of it through a REST API.

It is a FastAPI and PostgreSQL app on the homelab k3s cluster, namespace `docextractor`. It fetches pages with plain HTTP where it can, and through the shared Firecrawl and Browserless services where pages need a browser.

**Data model** (counts as of 2026-11-03):

| Object | Count | What it is | Key fields for Release Watch |
| --- | --- | --- | --- |
| Vendor | 43 | A company (Commvault, Cohesity, Veeam…) | `id`, `name`, `website`, `tier_id` |
| Product | 109 | A product line under a vendor | `version`, `previous_version` (only 22 products set one) |
| Source | 396 | One documentation site or PDF for a product. 310 are web, 86 are PDF | `base_url`, `url_template`, `platform`, `auth_realm_id`, `last_extracted_at` |
| Article | 120,847 live | One page, or one section of a PDF | `source_url`, `title`, `content_markdown`, `content_changed_at`, `source_changed_at`, `last_updated_at` |
| TOC entry | — | The source's navigation tree; an entry may be a heading with no page | `title`, `url`, `level`, `children`, `article_id` |
| Article version | — | A superseded snapshot of a page, with a diff | `extracted_at`, `version`, `has_diff` |
| Extraction run | — | One crawl of one source | `status`, `kind`, counts |

**How crawls are scheduled.** Three enabled jobs crawl whole tiers of vendors, each starting at 02:00. A Tier 1 run takes about 9 hours:

| Job | When it runs | Effective interval |
| --- | --- | --- |
| Tier 1 | First Sunday of every month | Monthly |
| Tier 2 | First Saturday of odd months | Every 2 months |
| Tier 3 | Last Saturday of Feb, May, Aug and Nov | Quarterly |

A crawl is incremental. Unchanged pages are detected by hash, and only real changes create a version and a change-feed row. An operator can also trigger a crawl by hand.

**Consequence for Release Watch:** a page in DocExtractor can be up to three months behind the live site. Treat DocExtractor as the catalog and history, not as the daily alarm.

## Access

Use a dedicated **read-only API key** issued to Release Watch, sent as `X-API-Key: dxk_…`. Do not reuse an operator's key.

| Item | Value |
| --- | --- |
| In-cluster base URL (recommended) | `http://docextractor-backend.docextractor.svc:8000/api` |
| LAN base URL | `http://docextractor.k3s.home.lan/api` |
| Interactive API docs | `http://docextractor-backend.docextractor.svc:8000/docs`, and the schema at `/openapi.json`. Both are in-cluster only: on the LAN host, `/docs` is the web UI |
| Auth header | `X-API-Key: <key>`, or `Authorization: Bearer <JWT>` |
| Key creation | `POST /api/auth/keys` with `{"name": "release-watch", "role": "read_only"}`. The raw key is returned once; store it as a Kubernetes Secret |

**Authorization rules** (enforced by `AuthMiddleware` on every `/api/` call):

- Access is decided by HTTP method. GET needs `read_only`; POST, PUT, PATCH and DELETE need `read_write`. Release Watch needs only GET.
- Vendor visibility is per user. A key sees only the vendors its owning user can see, so issue it from a user with see-all access, or Release Watch will silently miss vendors.
- `/api/jobs` and `/api/auth-realms` are admin-only. Release Watch does not need them.

There is no rate limiter, but the API shares a database with the crawler. Keep to a few requests per second and page with `limit=200`.

## Endpoints Release Watch needs

All of these are GET calls, so a read-only key can use them. Paths are relative to `/api`. List endpoints take `skip` and `limit` (maximum 200) and return `{<items>: [...], total}`.

| Purpose | Call | Returns (fields that matter) |
| --- | --- | --- |
| Vendors | `GET /vendors` | `vendors[]`: `id`, `name`, `website`, `tier_id` |
| Products | `GET /products?vendor_id=` | `products[]`: `id`, `vendor_id`, `name`, `version`, `previous_version` |
| Sources | `GET /sources?product_id=` or `?vendor_id=` | `sources[]`: `id`, `product_id`, `name`, `source_type` (`web` or `pdf`), `base_url`, `url_template`, `platform`, `auth_realm_id`, `status`, `last_extracted_at` |
| Page tree of a source | `GET /articles/toc/{source_id}` | Nested `entries[]`: `title`, `url`, `level`, `is_article`, `article_id`, `children[]` |
| Find pages | `GET /articles?q=release+notes&source_id=` | Full-text search over title and body. Paginate with `next_cursor`; resend the same `q` |
| One page with content | `GET /articles/{id}` | `source_url`, `title`, `content_markdown`, `last_updated_at`, `content_changed_at`, `source_changed_at`, `vendor`, `product`, `parent_chapter` |
| Page history | `GET /articles/{id}/versions` | Snapshots, newest first: `extracted_at`, `version`, `has_diff` |
| Diff | `GET /articles/{id}/versions/{vid}/diff?against=next` or `current` | Unified `diff_text` |
| Source changelog | `GET /sources/{id}/changelog` | Every add, change and removal for a source, newest first |
| Change feed | `GET /articles/delta?since=<cursor>&vendor_id=` | JSONL stream (see below) |

**The change feed** (`/articles/delta`) is the efficient way to keep a local mirror in sync. A graph-RAG service in the `graph-rag` namespace already consumes it.

- Call it without `since` once to get a full snapshot. Then call it with the `next_since` cursor from the last line of the previous response.
- Each line is one JSON object:
  - `added` and `updated` lines carry the full page: `id`, `topic_key`, `source_id`, `vendor`, `product`, `title`, `source_url`, `content_markdown`, `content_hash`, `content_changed_at` and `source_changed_at`.
  - `removed` lines are tombstones: `id`, `topic_key`, `source_id` and `removed_at`, with no content.
- The last line is `{"control":"cursor","next_since":"…","count":N}`. If it is missing, the stream was cut short: do not advance your cursor.
- The feed is gap-free while runs are active: it never serves past the oldest running crawl.

**Webhooks** (`new_page`, `updated_page`, `removed_page`, `extraction_complete`) also exist, optionally HMAC-signed, but creating one needs a read-write key. A page event carries `page_url`, `page_title`, `article_id`, `vendor_name` and `product_name`. For Release Watch, polling the delta feed once a day is simpler and needs no inbound endpoint.

## Finding release-notes pages

DocExtractor has no "release notes" flag. Release-notes pages live in three shapes, and Release Watch has to classify them itself:

| Shape | Example | How to find it |
| --- | --- | --- |
| A whole source that is release notes | Arcserve ShadowProtect's 8 "… Release Notes" sources; Zerto "Release Notes"; Backup Exec "Release Notes" | Source `name` matches release notes, what's new or readme. 23 sources match today |
| Pages inside a user guide | A "What's New" chapter in a Veeam guide | Title or URL match on the pages, or a TOC branch named "Release Notes" |
| A release-notes PDF | Dell "…-19-12-release-notes.pdf", Cohesity `75_ReleaseNotes.pdf` | A `pdf` source with release notes in its name. The version is usually inside the URL, and a new release is a **new** PDF URL |

A rough title and URL match (`release notes`, `what's new`, `new features`, `changelog`, `revision history`, `readme`) over live pages finds 723 pages in 118 sources, across 31 of the 43 vendors. Expect noise: some are articles that only mention release notes.

**12 vendors have no release-notes page in DocExtractor at all:** AWS, CloudAlly, Eon, Flosum, Gearset, Google, Nakivo, Red Hat, Salesforce, Slide, Synology and Velero. For those, Release Watch needs URLs entered by hand (a GitHub releases feed, a vendor blog or a status page).

**Recommendation:** run the heuristic once to propose a list, have a person confirm it, and keep the confirmed list in Release Watch's own database, keyed by DocExtractor `article_id` and `source_id`. Re-run the discovery weekly so new release-notes pages are proposed rather than silently missed.

## Versioning model

A product's `version` is the release DocExtractor currently tracks. A **version bump** means an operator moves the product to a newer release: DocExtractor rewrites every templated source URL and re-crawls those sources. Release Watch's version job exists to say when a bump is due.

**Coverage is thin:** only 22 of 109 products have a `version`, and 21 of those have templated sources (156 sources in total). The other 87 products are either unversioned (SaaS) or carry a version only as literal text inside a URL.

**URL templates.** A source's `url_template` is its `base_url` with the release spelled as placeholders:

| Placeholder | Meaning | Example product and template |
| --- | --- | --- |
| `{version}` | The product version exactly as stored | Commvault Cloud 11.46: `https://documentation.commvault.com/{version}/software/index.html` |
| `{version_compact}` | The version with separators removed (`25-1` becomes `251`) | Backup Exec (Arctera) |
| `{version_major}` | The first component (`24-1` becomes `24`) | System Recovery: `…/arctera-system-recovery-{version_major}-readme/{version}/arcteratm-system-recovery-{version}-readme.html` |
| `{rev}` | An opaque build id the **vendor** mints per release. It can't be guessed | Cohesity NetBackup 11.2.0.1: `https://docs.cohesity.com/docs/netbackup/{version}/160052279-{rev}-0` |

**Version spelling varies per vendor.** A product stores the version the way its URLs write it: `11.46` (Commvault), `25-1` (Arctera), `20-2` (Dell), `5.2.1-1025` (HYCU), `2026.2.1` (CloudAlly), `4.22` (Red Hat OADP, which follows the OpenShift version). Release Watch must compare versions per product, not with one global rule.

**Bump endpoints, for context only.** The operator uses `POST /products/{id}/versions/preview` and `POST /products/{id}/versions/bump` in DocExtractor once Release Watch has flagged a new release. Release Watch calls neither: it decides "is there a newer version?" from its own fetches, using the templates above to know where a new release's pages would live.

**Traps when probing a candidate URL:**

- **Fake success:** Cohesity answers a missing release with HTTP 200 and an app shell containing `ERROR_FALLBACK;404`. Check the body, not just the status code.
- **Templates that can't predict the next release:** in Dell PDF URLs (`manual16714979-dell-…-{version}-…pdf`) the manual id also changes per release, so a guessed URL 404s even when the release exists.
- **Login walls:** these send a short page that redirects to a login URL, often with status 200.

## Freshness and change semantics

Each page carries several timestamps, and only two of them mean "the content changed":

| Field | Moves when | Use it for |
| --- | --- | --- |
| `source_changed_at` | The vendor's page itself changed (the raw-scrape hash moved and a version was archived) | **This** is "the vendor edited the release notes" |
| `content_changed_at` | The served Markdown changed: a vendor edit, **or** DocExtractor enrichment such as image captions or URL rewriting | Re-syncing a mirror; too noisy for alerts |
| `content_changed_basis` | — | `exact`, `lower_bound` (the change happened at or before this time) or `first_seen` |
| `last_updated_at` + `last_updated_source` | The vendor's own revision date from page metadata (`vendor_meta` or `page_markup`). Null for most vendors | A useful hint when present. Never invented |
| `extracted_at` | Every crawl, changed or not | Never use as a change signal |

**The delayed signal.** Changes reach DocExtractor only when a crawl runs (monthly to quarterly, see above). The change feed then reports them within minutes. So the feed tells Release Watch *what changed since the last crawl*, not *what changed today*.

**The daily signal comes from Release Watch's own fetch.** Fetch the page's `source_url`, extract the main text, normalise it, hash it, and compare with yesterday's hash. Release Watch's own first fetch is the baseline; DocExtractor's stored copy plays no part in change detection.

Two things inflate false "changed" results. Strip both before hashing:

- **Signed or re-minted URLs:** Document360 and Azure image URLs carry `st`, `se` and `sig` query tokens that change on every fetch. This made about 60% of Securiti pages look changed on every crawl until DocExtractor learned to strip them.
- **Page chrome:** headers, footers, cookie banners, "last visited" widgets and per-request ids. Compare only the article body.

## API readiness check

Verdict: **ready as is.** Release Watch only needs DocExtractor to say *what* to watch, and every read it needs is a GET that a read-only key can call. No DocExtractor change is required.

| What Release Watch needs from DocExtractor | Supported? | How |
| --- | --- | --- |
| All vendors, products and sources | Yes | `/vendors`, `/products`, `/sources` |
| Each product's tracked version and URL templates, to know what "newer" means and where a new release would live | Yes | `products[].version`, `sources[].url_template` |
| The URLs of release-notes pages | Yes, after Release Watch classifies them | Search, TOC, source names |
| Knowing when DocExtractor itself moved (a bump done, a page removed, a source added), so the watch list and the "bump due" alerts stay current | Yes | Daily catalog sync; `removed` tombstones in the delta feed |

### Consequences of watching independently

- **Login-walled pages:** 38 sources sit behind 5 sign-ins: AvePoint Learn (27), Cohesity (8), and Securiti, Rubrik and Eon (1 each). Release notes exist behind AvePoint (24 sources), Cohesity (13) and Rubrik (1). Release Watch can watch these only with its own vendor accounts and its own login handling. Otherwise leave them out, or watch a public substitute such as a vendor blog or public PDF.
- **Version checks are Release Watch's own.** It builds candidate URLs from `url_template` and fetches them itself, so it must reimplement the checks listed under *Traps* in the versioning section: soft-404 bodies and login redirects.
- **Cohesity's `{rev}` templates** can't be probed by guessing a URL, and those sites are login-walled anyway. Detect new Cohesity releases from release notes or public pages instead.

### Minor (no change needed)

- Product responses carry `vendor_id` but no vendor name. Join on the client.
- The delta feed filters by `source_id` or `vendor_id`, not by product.
- No timestamp records when a product was last bumped (`updated_at` also moves on a rename). Release Watch should record bumps it observes in its own database.

## Rules of engagement and gotchas

**Hard rules:**

1. **Never use the shared Firecrawl or Browserless.** DocExtractor's crawls depend on them, and extra load has twice tipped them into 502 and 503 errors mid-crawl. If Release Watch needs a headless browser, run its own.
2. **Never call write endpoints**: bump, trigger, PATCH and DELETE. Release Watch recommends; an operator acts.
3. **Never store or log secrets.** That covers the API key and any vendor cookie. A Slack webhook URL is not treated as a secret here.
4. **Be polite to vendors:** one request per page per day, a real browser User-Agent, backoff on 429. Arctera (`docs.backupexec.com`) returns 429 under fast sweeps.

**Vendor quirks that will bite a plain HTTP fetcher** (all learned the hard way in DocExtractor):

| Vendor or site | What happens | What works |
| --- | --- | --- |
| IBM docs | 403 for a browser User-Agent | A `curl/<version>` User-Agent |
| Dell support (Akamai) | Requests from our IP are blocked | A real browser with a warm-up visit first; prefer the PDF URLs |
| Red Hat docs (Akamai) | Blocked without a warm-up visit | A browser warm-up, or rely on DocExtractor's crawl |
| `www.rubrik.com` PDFs | 403 on every path (WAF) | Nothing automatic; manual upload |
| Cohesity docs | 403 for a `HeadlessChrome` User-Agent; missing pages return 200 with `ERROR_FALLBACK;404` | A real Chrome User-Agent; check the body |
| Most Flare, Zoomin and Veeam sites | Server-rendered, fine over plain HTTP | Plain GET |
| Keepit product updates | One page holding several feeds (`#updates`, `#pmc`) | Watch each section separately |

**Other gotchas:**

- **Expired sessions look healthy.** AvePoint answers an expired cookie with a 200 "Redirecting…" page of about 400 bytes. Treat a sudden, very short page as "unknown", never as "changed".
- **A page can move.** DocExtractor identifies a page by `topic_key`, and its `article_id` survives a version bump even though the URL changes, because the version is stripped out of topic\_key. Key Release Watch's records on `article_id` and re-read `source_url` from the API each day rather than caching it forever.
- **PDF articles share a URL.** A PDF split into sections gives many articles one `source_url` (with `#page=` anchors). Watch the PDF once, not per section.
- **Removed is soft.** A removed page gets a `removed` tombstone in the feed; it may come back later under the same id. Mark it inactive rather than deleting your history.

## Sources

Everything in this briefing comes from the DocExtractor repository (`backend/app/routes`, `backend/app/schemas`, `CLAUDE.md`) and read-only queries on the production database on 2026-11-03.
