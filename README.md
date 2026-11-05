# Release Watch

Tells you every morning which vendor release notes gained new entries, and which products shipped a
version newer than the one DocExtractor tracks. Design: `docs/superpowers/specs/2026-11-03-release-watch-design.md`.

Phase 1 (this code): daily catalog sync from DocExtractor, discovery of release-notes and news sources,
and a web review queue where the owner confirms or rejects each one.

## Commands

| Command | What it does | Schedule in the cluster |
| --- | --- | --- |
| `release-watch sync` | Mirror DocExtractor's vendors, products and sources | daily 08:00 UTC |
| `release-watch discover [--relabel]` | Sync, propose candidates, fetch and label them, follow DocExtractor URL changes | Daily 08:30 UTC |
| `release-watch seed seeds/sources.yaml` | Load hand-researched sources as candidates | by hand |
| `release-watch web` | Review queue on :8000, `/healthz` | Deployment |
| `release-watch migrate` | `alembic upgrade head` | web init container |

Every job writes a `job_run` row.

## Local development

```bash
uv sync
docker run -d --name rw-test-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw_test -p 55432:5432 postgres:16-alpine
RW_TEST_DATABASE_URL=postgresql+psycopg://postgres:rw@localhost:55432/rw_test uv run pytest -q
```

Settings come from `RW_*` environment variables. For local runs, a git-ignored `.secrets` file in the
repo root supplies `DOC-EXTRACTOR`, `OPENROUTER`, `AZURE-OPENAI-KEY` and `AZURE-OPENAI-ENDPOINT`.
Set `RW_DATABASE_URL` yourself.

## Deploying to k3s

Images are built and pushed to `ghcr.io/carev01/release-watch` by CI on pushes to `main`.

```bash
kubectl create namespace release-watch
kubectl -n release-watch create secret generic release-watch-secrets \
  --from-literal=POSTGRES_PASSWORD='<url-safe password>' \
  --from-literal=RW_DOCEXTRACTOR_API_KEY='dxk_…' \
  --from-literal=RW_AZURE_OPENAI_API_KEY='…' \
  --from-literal=RW_AZURE_OPENAI_ENDPOINT='https://….openai.azure.com/' \
  --from-literal=RW_OPENROUTER_API_KEY='…'
helm upgrade --install release-watch deploy/helm/release-watch \
  --namespace release-watch -f deploy/helm/release-watch/values-homelab.yaml
```

Add `release-watch.k3s.home.lan` to DNS, pointing at the Traefik ingress.

Red Hat docs and Dell's KB pages refuse plain HTTP clients, so they are read through the shared in-cluster
Browserless, as DocExtractor does. Copy its token into the Secret without printing it:

```bash
kubectl -n release-watch patch secret release-watch-secrets --type merge -p "{\"stringData\":{\"RW_BROWSERLESS_TOKEN\":\"$(kubectl -n browserless get secret browserless-token -o jsonpath='{.data.token}' | base64 -d)\"}}"
```

Without it, those pages are fetched over plain HTTP and show as suspect in the health block.

### Teams notifications (Power Automate)

The daily digest (11:00 UTC) and the immediate alerts are posted to a Teams group chat through a Power Automate webhook.

1. In Power Automate, create an **instant cloud flow** with the trigger **"When a Teams webhook request is received"** (Teams connector). Set *Who can trigger the flow* to **Anyone**.
2. Add the action **"Post card in a chat or channel"**. Post as **Flow bot**, post in **Group chat**, and pick the chat.
3. For **Adaptive Card**, use the expression `triggerBody()?['attachments']?[0]?['content']`. Release Watch sends the Teams message envelope `{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{…}}]}`, one card per request.
4. Save, copy the trigger's HTTP URL, and add it to the Secret. The URL carries a signature, so treat it as a password:

```bash
kubectl -n release-watch patch secret release-watch-secrets --type merge \
  -p '{"stringData":{"RW_TEAMS_WEBHOOK_URL":"<flow URL>"}}'
```

Until the URL is set, the digest and job-failure alerts are kept in the web UI instead, on the Notifications page (http://release-watch.k3s.home.lan/notifications), which also previews the next digest. To print the cards as JSON without sending: `kubectl -n release-watch exec deploy/release-watch-web -- release-watch digest --dry-run` (or locally, `uv run release-watch digest --dry-run`).

### Daily jobs

| Job | Schedule (UTC) | What it does |
|---|---|---|
| `sync` | 08:00 daily | Mirrors DocExtractor's catalog |
| `discover` | 08:30 daily | Proposes new release-notes candidates for the review queue; when DocExtractor moves a URL (a version bump), the watch follows it |
| `watch` | 09:00 daily | Reads every active source, confirms changes about 10 minutes later, gates them, writes events |
| `digest` | 11:00 daily | Posts the summary card and one card per new or edited entry (kept on the Notifications page while no webhook is set). The summary lists, per vendor and product, versions newer than DocExtractor tracks and DocExtractor pages that now redirect or are gone; the watch following a page to its next-version URL is not reported |

Every job runs `migrate` first. The first `watch` after a source becomes active only records a baseline. Held-back items wait under **Needs a look** in the web view. Per-source selectors, split rules and newest-child rules live in `seeds/sources.yaml` under `source_config:`; apply them with `release-watch seed seeds/sources.yaml`.

Verify:

```bash
kubectl -n release-watch get pods,cronjobs,ingress
curl http://release-watch.k3s.home.lan/healthz
kubectl -n release-watch create job --from=cronjob/release-watch-sync sync-manual
```

Secrets never go in values files. The DocExtractor key is read-only, and Release Watch only ever sends GET to DocExtractor.
