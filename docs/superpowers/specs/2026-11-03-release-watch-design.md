# Release Watch — Design

2026-11-03 · Everton Cardoso (owner) · written with Claude

This document is the design to build from. It refines two earlier documents, kept in this repo:

- `docs/reference/docextractor-briefing.md`: what DocExtractor's API offers, its access rules, vendor quirks and hard rules of engagement. **All of it still applies** unless this document says otherwise.
- `docs/reference/high-level-design.md` (the "HLD"): the first design. This document **overrides** it where they differ; the sections marked *Changed from the HLD* say how.

Calls marked **(builder's call)** or **(Claude's call)** were not made by the owner and can be revisited.

## 1. Intent

The owner follows about 40 backup and data-protection vendors by hand and cannot keep up, so they always learn about changes late. Release Watch fixes that. It tells them **early** about new features and changes to existing features, and about new product versions that DocExtractor should be bumped to.

What matters is the **content** of a change, not the fact that a page changed. A notification that says "Vendor X's release notes changed" and forces a click-through has failed at its job.

## 2. Goals, in priority order

1. **New release-notes entries, delivered verbatim.** When a vendor adds an entry to its release notes or changelog, send the entry's **full text, word for word**, with a one-line LLM summary above it. Most vendors ship rolling releases, so an entry is usually a **date heading followed by a list of new features and changes**, not a version number.
2. **Edited entries.** When a vendor changes an existing entry, send the changed part as a diff, with a one-line summary.
3. **New product versions**, only for products with versioned documentation in DocExtractor. The message is one line: **"\<Vendor> released a new version of \<Product>: \<VERSION>"**, plus the tracked version and an evidence link.
4. **No silent failures.** A failure never looks like "nothing changed". A missing message is itself the alarm.

## 3. Anti-goals: what makes this a failure even if it runs

In the owner's order of harm:

1. **False positives, the worst.** A wrong "new entry" or "new version" erodes trust and gets the channel ignored. **When unsure, do not notify.** Put the item in a "needs a look" list in the web view instead.
2. **Missed updates or versions.** Mitigated by the health report (section 9) and weekly discovery.
3. **Late notifications.** Anything later than **7 days after the vendor published** counts as a failure. Daily checks leave plenty of slack, and the design spends some of it on confirmation (re-fetches, waiting for a second signal).

## 4. Non-goals

- Bumping versions, triggering crawls, or any DocExtractor write. Release Watch recommends; a person acts.
- Storing documentation. Only the text of watched pages and entries is kept.
- Watching pages that aren't release notes or news for change.
- **Veeam's core products are out of scope**, for both release notes and versions. The owner already knows when they change. **Exceptions, which are watched:** Veeam Kasten, Securiti.ai, and Veeam Data Cloud (VDC, whose changelog is `https://www.veeam.com/veeam-data-cloud/change-log.html`). Exclusion is **per product**, from an exclusion list in config applied at catalog-sync time, because Kasten and VDC may sit under the Veeam vendor in DocExtractor.
- *Changed from the HLD:* there is no separate "new releases seen for unversioned products" report. For rolling-release products the new-entry notifications already cover that.

## 5. Constraints (unchanged from the HLD and briefing)

- Own namespace (`release-watch`), own PostgreSQL, own deployment on the homelab k3s cluster. Its only link to DocExtractor is the HTTP API, with a **dedicated read-only key** issued from a see-all user.
- DocExtractor is the catalog only: vendors, products, sources, versions, URL templates, page URLs. **Its stored page content is never used to decide whether something changed.**
- *Changed from the briefing and HLD:* Release Watch **may use the shared Firecrawl and Browserless** when a page needs a browser. It does not run its own Chromium. Be gentle with them, because DocExtractor's crawls depend on them and extra load has caused 502/503s mid-crawl before:
  - at most 2 concurrent sessions,
  - back off and retry later on 502/503/429,
  - plain HTTP whenever it works.

  DocExtractor's tier crawls start at 02:00, and Tier 1 runs about 9 hours, so on crawl days (first Sunday monthly, first Saturday of odd months, last Saturday of Feb/May/Aug/Nov) they overlap the 06:00 `watch`. Those limits matter most then.
- Be polite: one fetch per page per day (plus the confirmation re-fetch), a real browser User-Agent unless a host policy says otherwise, backoff on 429, per-host concurrency limits.
- Never store or log secrets: the API key, vendor credentials, cookies, the webhook URL.
- The vendor quirks table in the briefing (IBM, Dell, Red Hat, Rubrik PDFs, Cohesity soft-404, Keepit multi-feed, AvePoint expired sessions, signed image URLs) is a requirement list for the fetcher.

## 6. Coverage

| Group | Treatment |
| --- | --- |
| Vendors with public release notes | Watched directly (`direct` mode, or `browser` for Akamai-gated hosts) |
| **AvePoint** (login, phase 2) | Watched daily with a login. Keep the session and re-log in only on expiry. Detect expiry by the ~400-byte "Redirecting…" page and never treat it as a change |
| **Eon** (login, phase 2) | Watched daily with a login. Needs vendor-specific login steps: Eon makes you go through several pages before it sets cookies that work on the docs site. Implement as a scripted flow through the shared Browserless (Playwright over CDP), then reuse the cookies for plain fetches where possible |
| **Rubrik, Cohesity** (login) | **Never logged in to.** The owner wants to keep logins to these accounts rare. Covered only through public sources: blog, press releases, public PDFs, public release-notes pages if any. Listed in the health section as "public sources only" |
| The 12 vendors with no release notes in DocExtractor (AWS, CloudAlly, Eon, Flosum, Gearset, Google, Nakivo, Red Hat, Salesforce, Slide, Synology, Velero) | Sources found by hand research during discovery: GitHub releases for Velero, plus vendor changelogs, "what's new" pages, blogs and RSS feeds |
| Veeam core products | Out of scope |
| Veeam Kasten, Securiti.ai, Veeam Data Cloud | Watched like any public vendor. VDC's changelog is hand-added. Securiti.ai's release notes (about 30 pages) sit behind the helpcenter.securiti.ai login. **The owner approved a daily login for Securiti.ai in Phase 2**, like AvePoint and Eon (decided 2026-11-03) |

Vendor credentials are entered by the owner as Kubernetes Secrets. Whose accounts they are is the owner's business; the code just reads a secret per vendor.

## 7. Monitor 1: new release-notes entries

*Changed from the HLD:* the unit of change is the **entry**, not the page. The HLD hashed whole pages and reported diffs. This design splits each page into entries and reports new entries verbatim.

### 7.1 What gets watched

A **watched source** is one of:

- **Entry page:** an HTML page holding many entries, newest usually first (a changelog, "What's new", a release-notes page). Most sources are this kind.
- **Feed:** RSS, Atom or GitHub releases. Each item is an entry.
- **Index page:** a page or TOC branch that links to one page (or PDF) per release, such as a release-notes PDF per version or a "Release notes" TOC branch with one child per release. Read it **live**, not from DocExtractor's TOC, which can be months stale. A **new link is a new numbered release, not a verbatim entry.** The owner's rule: vendors that publish one big document per release add features only with numbered versions, so all they need is the version line. A new link therefore produces a **version notification** ("\<Vendor> released a new version of \<Product>: \<VERSION>"), with the version taken from the link text or URL. An optional one-line LLM summary of what's new is generated from the linked document. The linked document is **not** sent verbatim. For a versioned product this is also strong evidence for monitor 2 and goes through its candidate pipeline (section 8). One (product, version) never produces two messages.
- **Section of a page:** for pages that hold several feeds (Keepit's `#updates` and `#pmc`), each section is watched separately.

Each watched source records: URL, kind, the DocExtractor `article_id`/`source_id`/`product_id` where one exists (nullable for hand-added sources), fetch mode, body selector, entry-split rule, and state (`candidate`, `active`, `rejected`, `gone`).

For DocExtractor-backed sources, **re-read the URL from the API on each catalog sync** and key on `article_id`, because URLs move on version bumps (briefing, "A page can move"). A PDF split into many articles is watched once.

### 7.2 Daily check

1. **Fetch** with the source's fetch mode and the host policy.
2. **Extract the body**, using the platform selector from DocExtractor's profiles where known (for example `#articleContent` for Document360, `article` for Flare), else a generic readability extractor (trafilatura). Convert to Markdown.
3. **Normalise for comparison only**: collapse whitespace, strip query strings from image and link URLs (signed tokens), remove known noise patterns. The **verbatim text sent to the owner is the un-normalised extracted Markdown**, not the normalised form.
4. **Split into entries** with the source's entry-split rule. The default is to split on the heading level whose headings look like dates or versions. Each entry gets:
   - a **key** (the normalised heading text, e.g. `2026-10-28` or `11.48`),
   - its **verbatim body**,
   - a **body hash** of the normalised body.
5. **Compare with the stored entries** of the source:
   - an entry whose key **and** body hash were never seen before is a **new entry**,
   - a known key with a different body hash is an **edited entry**,
   - a body hash seen before under another key (a renamed or reordered heading) is **not new**.
6. **Confirm.** Re-fetch once about 10 minutes later. The new or edited entry must appear identically in both fetches.
7. **Gate on plausibility** (section 7.3). What passes is queued for the digest; what fails goes to "needs a look".
8. **Record version evidence.** Pass the new entry's heading and body to monitor 2 (section 8.2).

The **first successful fetch of a source is its baseline**: every entry is stored and none is reported.

### 7.3 Plausibility gates (false positives are the worst failure)

An item goes to "needs a look" instead of the digest when any of these holds:

- More than **3** new entries appear at once on a source, which suggests the page structure or the splitter broke. Owner can confirm them from the web view.
- The splitter finds **zero** entries on a page that had entries before.
- The page is **suspect**: under 20% of the previous size, redirected to a login URL, a known block page or soft-404 marker, or an expired-session page.
- The edit is whitespace-only or punctuation-only. These are dropped outright, not queued.
- An edited entry's diff exceeds **50%** of the entry. That suggests a mis-split; the owner can still confirm it.

Thresholds are config values (builder's call on defaults beyond those named here).

Three suspect days in a row raise a "source broken" health item. A 404, or a redirect to an unrelated page, marks the source `gone` and reports it once in the health section.

### 7.4 The one-line summary

An LLM writes one sentence per new or edited entry, e.g. "Adds Azure Blob immutability; fixes 14 issues." It is shown **above** the verbatim text and never replaces it. **The LLM never writes, rewrites or trims the verbatim text.** If the LLM call fails, the entry is sent without a summary, not held back.

## 8. Monitor 2: new versions

*Changed from the HLD:* the main signal is **vendor news read by an LLM**, mirroring how the owner checks by hand today (vendor blogs, press releases, GitHub releases). Guessing next versions and probing URLs is demoted to a confirmation check.

### 8.1 Scope

Only products with a `version` in DocExtractor (22 today, minus excluded Veeam core products). The set is re-read on every catalog sync, so a product that starts tracking a version joins automatically.

Each such product has a **version profile**: the version format (a regex inferred from the current value, e.g. `11.46` dotted, `25-1` dashed, `5.2.1-1025` version plus build, `2026.2.1` calendar), a comparator for that format, and its news sources. Formats are inferred at sync time and editable in the web view.

### 8.2 Evidence sources

| Strategy | How | Strength |
| --- | --- | --- |
| **News** (primary) | Per vendor, one or more **news sources**: blog, press-release page, RSS/Atom feed, GitHub releases. New posts are detected daily (feed items, or new links on a listing page). Each new post is given to an LLM with the vendor's versioned products and their current versions, and one question: *does this post announce the general availability of a new version of one of these products, and which version?* The LLM must return the product, the version and a **verbatim quote** from the post. | Strong when verified (below) |
| **Release-notes evidence** | A new entry from monitor 1 on a source tied to the product whose **heading** contains a version newer than the tracked one. A version mentioned only in the body is weak. | Strong (heading) / weak (body) |
| **Template probe** (confirmation only) | For `{version}`, `{version_compact}` and `{version_major}` templates, check whether the templated URL for the **already-claimed** version resolves to real content. Reject soft-404 shells, login redirects and block pages. Never probe guessed versions. Skip `{rev}` templates and Dell's PDF manual-id templates. | Confirming only |
| **Web search** via the in-cluster SearXNG | Query "\<product> \<version> release" for an open candidate. | Weak; never alerts alone |

**Verification of LLM output (mandatory, in code):**

- the quote occurs in the fetched post text,
- the version occurs in the quote,
- the version matches the product's format regex and compares newer than the tracked version,
- the product named belongs to the vendor whose source it came from (one product's evidence never counts for another).

Failing any of these discards the claim. Betas, previews, release candidates and "coming soon" announcements are not GA. The prompt says so, and a keyword check on the quote backs it up.

### 8.3 Candidates and confidence

Evidence is grouped per (product, version) into a **candidate**:

- **High confidence → alert.** Either a verified news claim whose quote states availability explicitly ("now available", "is generally available", "released"), or any **two independent** strong or confirming signals (e.g. a news post plus a release-notes heading, or a news post plus a successful template probe).
- **Anything less → "needs a look"**, never Teams.
- States: `open` → `alerted` → `resolved` (DocExtractor's version reached or passed it on catalog sync) or `dismissed` (by the owner in the web view; stays dismissed).
- Each candidate alerts **once**, then appears in a weekly reminder line in the digest until resolved or dismissed. *Changed from the HLD:* the reminder is weekly, not daily, to avoid repetition fatigue (Claude's call).

## 9. Notifications

*Changed from the HLD:* delivery is to an **MS Teams group chat via a Power Automate webhook** the owner creates, not Slack. Email is not needed.

- Release Watch POSTs to one webhook URL, stored as a Kubernetes Secret (it contains a signature, so it is treated as a credential).
- Payload: the Teams workflow message envelope with an **Adaptive Card** attachment (`{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{…}}]}`), which Power Automate's "post to a chat when a webhook request is received" flow accepts as is (Claude's call). The repo's README documents the exact flow to build.
- Teams caps a message at about 28 KB. Send one card per item. An entry that exceeds about 20 KB is split into numbered parts ("1/3"), **up to 3 cards**. Past that, send the summary, the first part and a link to the full verbatim entry in the web view (Claude's call; rare in practice, since huge per-release documents are handled as version notifications, section 7.1). Adaptive Card text supports only basic Markdown (bold, italics, lists, links). Render tables and headings as plain text; **the words stay verbatim, only the formatting is simplified.**

**Daily digest at 08:00**, sent as a sequence of cards:

1. **Summary card:** new versions (one line each, including new releases from index-page sources for products DocExtractor doesn't version), number of new and edited entries by vendor, and the **health** block:
   - "Checked N of M sources", with the ones that failed, are suspect or are broken
   - login failures (AvePoint, Eon)
   - the "public sources only" vendors (Rubrik, Cohesity)
   - the number of items in "needs a look", flagging any older than **3 days** (so they can't quietly blow the 7-day limit)
   - DocExtractor sync errors
   - the time of the last successful `watch` run
2. **One card per new entry:** vendor and product, entry heading, the one-line summary, the **full verbatim text**, a link to the live page.
3. **One card per edited entry:** the same, with the diff of the changed part instead of the full text.

An empty day still sends the summary card ("No changes; 742 of 748 sources checked").

**Immediate messages:**

- a candidate reaching `alerted`: "\<Vendor> released a new version of \<Product>: \<VERSION> (tracked: \<current>)", with the evidence link and a link to the product in DocExtractor
- a scheduled job that failed outright
- the last successful `watch` being more than 26 hours old

## 10. Discovery

**Initial discovery is done by the builder (Claude), not left to the owner.** The owner reviews the **final list once** in the web view.

1. Run the briefing's heuristic over DocExtractor (source names, page titles, URLs, TOC branches matching release notes / what's new / new features / changelog / revision history / readme). Expect about 700 page candidates in about 118 sources.
2. Fetch each candidate live and classify it with code plus an LLM into: *release-notes source* (and its kind: entry page, index page, feed), *not release notes*, or *unsure*. Collapse duplicates: many matches will be pages inside one release-notes source, which is watched once at the right level.
3. Research by hand (web search plus fetches) the release-notes sources for the vendors with none in DocExtractor, and public substitutes for Rubrik and Cohesity.
4. Research **news sources** (blog, press, RSS, GitHub) for every vendor that has a versioned product.
5. For each confirmed source, pick and test the body selector and entry-split rule against the live page. The split must give sensible entries before the source goes `active`.
6. Present the list for the owner's single review: confirm or reject each, with rejections remembered.

**Weekly `discover` job:** re-runs steps 1–2 and proposes only **new** candidates into the review queue. A digest line says how many are waiting.

## 11. Data model (outline; builder refines)

DocExtractor ids are plain references, never cross-stack foreign keys.

| Table | One row per | Notes |
| --- | --- | --- |
| `catalog_vendor`, `catalog_product`, `catalog_source` | DocExtractor object (mirror, refreshed daily) | Includes `de_version`, `url_template`, an `excluded` flag for Veeam core products |
| `host_policy` | Host | User-Agent, concurrency, min delay, fetch mode, soft-404 markers, warm-up URL |
| `watched_source` | Release-notes source | Kind, URL, DE refs, selector, split rule, state, `confirmed_by` |
| `news_source` | Vendor news source | Kind (feed / listing / GitHub), URL, vendor, state |
| `fetch_attempt` | Every fetch | Outcome (`same`, `new`, `edited`, `suspect`, `error`, `gone`), status, bytes, detail |
| `snapshot` | Changed fetch, plus the baseline | Extracted Markdown and hash |
| `entry` | Entry seen on a source | Key, body hash, verbatim body, first/last seen, `removed_at` |
| `entry_event` | New or edited entry | Source, entry, kind, diff, summary, status (`queued`, `needs_look`, `sent`, `dismissed`), `sent_at` |
| `news_item` | Post seen on a news source | URL, title, published, text, LLM verdict |
| `version_profile` | Versioned product | Format regex, comparator |
| `version_candidate`, `version_evidence` | (product, version) / piece of evidence | As in the HLD, with strategy, quote, URL |
| `job_run` | Scheduled run | Job, start, end, counts, status |

## 12. Deployment and stack (as in the HLD)

- One Python 3.12 codebase and a Helm chart in **this repository**, deployed to the `release-watch` namespace.
- Libraries: httpx, selectolax or BeautifulSoup, trafilatura, SQLAlchemy plus Alembic, pytest, FastAPI. Playwright connected to the shared Browserless, and the shared Firecrawl's scrape API, where `browser` mode or the Eon login needs them.
- Components:
  - `web`: FastAPI with the review queue, "needs a look", dismissals, the diff viewer and `/healthz`. Ingress at `release-watch.k3s.home.lan`.
  - CronJobs: `sync` daily 05:00, `discover` Mondays 05:30, `watch` daily 06:00 (monitor 1, then monitor 2), `digest` daily 08:00.
  - `postgres` StatefulSet with a 1 GiB PVC.
- Each CronJob writes a `job_run` row and has `concurrencyPolicy: Forbid`.
- Secrets: the DocExtractor key, the webhook URL, the Azure OpenAI key and endpoint, the OpenRouter key, and AvePoint, Eon and Securiti.ai credentials.
- **Web view access:** LAN-only behind the ingress, no login (builder's call; it's a homelab service).
- **LLM:** **Azure OpenAI `gpt-5-mini`** is the primary model for summaries, classification and validation. A cheap OpenRouter model is the fallback when Azure fails (the fallback model is the builder's call). Use one small client interface with both providers behind it. LLM use is limited to: discovery classification, news-post version claims, and one-line summaries. Every LLM output that drives an alert is verified in code (section 8.2).

## 13. Phased delivery

Each phase ships something usable on its own.

1. **Catalog and discovery.** Scaffold the repo, chart and database. Build the catalog sync (with the Veeam core-product exclusion), the discovery pipeline and the web review queue. Do the initial discovery (section 10). *Done when* the owner has confirmed the release-notes and news-source list.
2. **Release-notes watch.** First the public sources: the direct fetcher with host policies, extraction, normalisation, entry splitting, confirmation re-fetch, plausibility gates, LLM summaries, and the Teams digest with health. Feeds (RSS, Atom, GitHub releases) and index-page release detection are included. **Then, still in this phase, the AvePoint, Eon and Securiti.ai logins:** `session` mode for AvePoint, the scripted Eon login, and the Securiti.ai help-center login, all through the shared Browserless. AvePoint and Eon don't use MFA (Securiti.ai still to be confirmed), so re-login runs unattended. *Done when* a week of digests has carried no false positives, all three login vendors produce baselines, and a login expiry shows up as a health item, not a change.
3. **Version watch.** Version profiles, news-source monitoring with LLM claims and code verification, release-notes evidence, template-probe confirmation, candidate states and immediate alerts. *Done when* replaying a known past bump would have alerted. Commvault 11.44 to 11.46 is the suggested test: feed the vendor's announcement posts for 11.46 through the pipeline.
4. **Coverage.** `browser` mode for Akamai-gated hosts (Dell, Red Hat), through the shared Browserless and Firecrawl, plus any sources that discovery or the health report show still need work.

## 14. Builder's environment

- The DocExtractor repository is checked out next to this one at `../doc-extractor`. Use it to reuse the per-platform body selectors from DocExtractor's profiles, and to mirror its CI, image registry and `helm upgrade` flow.
- The read-only DocExtractor key (`DOC-EXTRACTOR`) and the OpenRouter key (`OPENROUTER`) are in `.secrets`, with the Azure OpenAI key and endpoint to be added there, at the repo root. **It must be git-ignored before the first commit**, never logged, and in the cluster replaced by Kubernetes Secrets. AvePoint, Eon and Securiti.ai credentials will be added there for phase 2. The builder may use them to script and test the logins.
- `kubectl`, `helm` and `gh` are available on the build machine. DocExtractor's LAN API (`http://docextractor.k3s.home.lan/api`) is reachable and answers 401 without a key.

## 15. Left to the builder

- Exact table columns, indexes and migrations.
- The entry-split algorithm beyond the default rule, and per-source overrides.
- The LLM model, prompts, and how unsure discovery results are presented.
- Card layout details within the constraints in section 9.
- Thresholds not fixed above.
- CI setup: mirror DocExtractor's push → CI → `helm upgrade` flow. Contract tests against DocExtractor's `/openapi.json`.
