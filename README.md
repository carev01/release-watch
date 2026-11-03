# Release Watch

Tells you every morning which vendor release notes gained new entries, and which products shipped a
version newer than the one DocExtractor tracks. Design: `docs/superpowers/specs/2026-11-03-release-watch-design.md`.

Phase 1 (this code): daily catalog sync from DocExtractor, discovery of release-notes and news sources,
and a web review queue where the owner confirms or rejects each one.

## Commands

| Command | What it does | Schedule in the cluster |
| --- | --- | --- |
| `release-watch sync` | Mirror DocExtractor's vendors, products and sources | daily 05:00 |
| `release-watch discover [--relabel]` | Sync, propose candidates, fetch and label them | Mondays 05:30 |
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

Verify:

```bash
kubectl -n release-watch get pods,cronjobs,ingress
curl http://release-watch.k3s.home.lan/healthz
kubectl -n release-watch create job --from=cronjob/release-watch-sync sync-manual
```

Secrets never go in values files. The DocExtractor key is read-only, and Release Watch only ever sends GET to DocExtractor.
