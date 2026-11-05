# Weekly login visit for AvePoint and Securiti — design

Date: 2026-11-05. Supersedes the "daily login, keep the session" plan for AvePoint, Eon and Securiti.ai in
`2026-11-03-release-watch-design.md` (vendor table and phase 3).

## Why

54 active release-notes sources sit behind a vendor login and have never been read: 26 on `learn.avepoint.com`
(AvePoint) and 28 on `helpcenter.securiti.ai` (Securiti.ai, filed under vendor "Veeam"), plus one on
`docs.securiti.ai`. Release Watch exists so that DocExtractor tracks the latest live content; these vendors are
currently invisible to it. The owner has accounts on both sites and wants them read.

Eon no longer needs a login: since October 2026 its product updates are published only on its public blog, which
is watched as a normal public source (done 2026-11-05).

## Goals

- Once a week, sign in to AvePoint and to Securiti, read every login-walled source of that vendor, and feed the
  results into the existing watch pipeline, so new and edited entries reach the daily digest like any other source.
- A failed login is visible (one Teams alert, a health line), never mistaken for a change.

## Non-goals

- Daily reads or keeping sessions alive between runs. The sites have short sessions; the owner chose one weekly
  visit. Updates on these sites may reach the digest up to 7 days late, which is within the owner's limit.
- Eon (public now), Rubrik and Cohesity (never logged in to; unchanged policy).
- Discovering new login-walled sources. The existing review queue and seeds stay the way sources are added.

## Anti-goals (a failure even if it works)

- **Credentials in any log, error message, job_run row, event, digest card or Browserless reply printed by the
  app.** The passwords live only in the Kubernetes Secret.
- **Suspicious login patterns on the owner's vendor accounts:** retry loops, several logins per run, logins on
  every daily job. Exactly one login attempt per vendor per weekly run, no retry.
- **False alerts.** A login page, an expired-session redirect or an empty help-center shell must never be read as
  a page whose entries were removed or changed. The owner ranks false positives in Teams as the worst outcome.

## Decided by the owner

- Cadence: one weekly visit per vendor: log in, read everything, leave.
- No MFA on either account.
- Credentials are in Secret `release-watch-secrets` as `RW_AVEPOINT_USER`, `RW_AVEPOINT_PASSWORD`,
  `RW_SECURITI_USER`, `RW_SECURITI_PASSWORD` (added by the owner).
- On a failed login: one Teams alert, nothing else reported for that vendor that week.
- **Edits are confirmed within the same visit** (decided 2026-11-05). The daily watch's 20-hour edit hold
  (`EDIT_HOLD`) does not apply to these sources: a changed page is re-read a few minutes later in the same weekly
  visit, and an edit seen on both reads is reported. Accepted cost: a slightly higher chance of reporting an edit
  the vendor would have reverted within a day. Edits then reach the digest within 7 days instead of 7–14.

## The login routes (verified by trial logins on 2026-11-05)

Both run in the shared Browserless (`/function`, as `fetch.py` already uses), desktop user agent, viewport at
least 1440×900 (Securiti warns below 1320 px).

**AvePoint**
1. Open `https://learn.avepoint.com/api/auth/aos-sso/commercial?returnUrl=%2F`. It redirects to the AvePoint
   Online Services login (`identity.avepointonlineservices.com`).
2. Fill `#account_login_username` and `#account_login_password`, press Enter.
3. Success: the browser lands on `https://learn.avepoint.com/`. Source pages then load normally (status 200, full
   text). A signed-out page is a ~400-byte "Redirecting…" document pointing at `/login?returnUrl=…`.

**Securiti**
1. Open `https://app.securiti.ai/#/login`. Type the user into `#email`, click the submit button ("Continue").
2. A "Select Domain" drop-down appears. Open it and click the option starting with `Privaci Demo New (`
   (full text "Privaci Demo New (privacidemo.com)"; the account sees other organizations too — never pick those).
   Click "Continue" again.
3. A password field appears; type the password, press Enter. Success: the app shows the Reporting Dashboard
   (`app.securiti.ai/#/reporting-dashboard…`).
4. Unlock the help center: click the button with `aria-label="Help Center"` (top right). A side pane opens; its
   article is rendered from a Document360 frame. Click the pane's open-in-new-window icon (top right of the
   article; in the trial it was clicked by position, 1410×155 at 1440×900 — find a sturdier handle if one exists).
   A new window opens on `https://helpcenter.securiti.ai/docs/…`.
5. Read the sources in that new window's session. Without step 4, `helpcenter.securiti.ai` sends the browser to a
   separate Document360 login (`identity.us.document360.io`), which counts as a failed visit.

`https://docs.securiti.ai/release-notes` (1 source) bounced into the app (`#/?redirectToDoc=…`) and showed a blank
page in the trial. Check it on the first run; if it has no content, propose rejecting it to the owner (it looks
replaced by the help center).

## How it fits

- A new CLI command and weekly CronJob (default: Mondays 06:30 America/Sao_Paulo, before the 08:00 digest). It
  records its own `job_run` job name, separate from the daily `watch`, so the daily health counts stay correct.
- Per vendor: one Browserless session that logs in and then reads that vendor's active, login-walled sources,
  handing each page's HTML to the same reading, comparison, gating and event code the daily watch uses
  (baseline on first read, then new/edited entries, unclear cases held in "Needs a look").
- The daily watch keeps skipping login-walled sources (as now).
- The confirmation re-read the daily watch does ~10 minutes later needs care: it must not trigger a second login.
- Health: replace the "Waiting for login" line with the last weekly visit per vendor, e.g. "Weekly login:
  AvePoint 26 read Mon 10 Nov, Securiti 28 read Mon 10 Nov", and show a failed or missing visit (older than 8
  days) as a health problem.
- Helm: pass the four credential keys only to the weekly job's pod.
- After the first visit, look at how each source split into entries and tune `source_config` in
  `seeds/sources.yaml` the way other vendors were tuned (none of these 54 has a split rule or follow config yet;
  2 per vendor are `index_page`, the rest `entry_page`).

## Settled during review (builder's calls, marked as such; the owner saw them)

- **Which sources:** active `login_walled` sources on `learn.avepoint.com` (AvePoint, 26), `helpcenter.securiti.ai`
  and `docs.securiti.ai` (Securiti, 29). Checked in the live DB on 2026-11-05: there are no other active
  login-walled sources, none for Rubrik or Cohesity. Select by host, not vendor name (the Securiti sources are
  filed under "Veeam").
- **Session length:** the shared Browserless has `TIMEOUT=300000` (5 minutes per session), `CONCURRENT=15`,
  `QUEUED=30`. Reading ~28 pages plus a login will not reliably fit one session. Log in once per vendor, then read
  in several sessions reusing the signed-in cookies (still exactly one login per vendor per run). If reused cookies
  turn out not to work for the Securiti help center, stop and report back rather than logging in again.
- **Partial runs:** a source that wasn't read (session time-out, page error) is recorded per source as an error
  ("not read this week") and shows in the health block; it is never compared or marked changed.
- **Login failure reporting:** one Teams alert when it happens (not through the whole-job failure path: the other
  vendor's visit still counts as done). After that, a "weekly login failed" health line appears in each daily
  digest until the next successful visit.
- **DocExtractor moved/gone:** 49 of these sources carry a `de_article_id`, so `_docextractor_moved` would fire on
  redirects. Redirects inside a signed-in session are often harmless (return URLs, Document360 slug redirects).
  For these sources a moved or gone result goes to "Needs a look", never straight to the digest.
- **Logins during the build:** the first visit stores each page's raw HTML (snapshots), so split rules can be tuned
  offline. At most one extra login per vendor while building. After tuning, clear these sources' baseline entries
  and take the first read again, so tuning never produces "new entry" alerts (as was done for Eon's blog source).

## Constraints

- Browserless is shared with DocExtractor: keep to its existing concurrency limit and be polite (a short pause
  between pages). It has a per-session time limit; ~28 pages in one session may need splitting or cookie reuse.
- Never print Browserless' raw reply on error (it can echo the function context, which holds the credentials).
- DocExtractor: GET only, as everywhere.
- Tests must not need real credentials or the network; the existing fakes (`tests/fakes.py`) show the pattern.

## Left to the builder

- How the browser-side login steps are expressed (one script per vendor is fine) and how pages are handed back
  (all HTML from one session, or exported cookies plus plain fetches, whichever survives the Browserless time limit).
- The exact health wording and the alert text, within the rules above.
