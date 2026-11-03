"""Read-only DocExtractor API client. GET is the only method it can send."""

import asyncio
import time
from collections.abc import AsyncIterator

import httpx

PAGE_LIMIT = 200
ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0


class DocExtractorError(Exception):
    pass


class DocExtractorClient:
    def __init__(self, base_url: str, api_key: str, *, max_rps: float = 4.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"X-API-Key": api_key},
            timeout=httpx.Timeout(30.0), transport=transport,
        )
        self._interval = 1.0 / max_rps
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    async def __aenter__(self) -> "DocExtractorClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_start + self._interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_start = time.monotonic()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        last = ""
        for attempt in range(ATTEMPTS):
            await self._throttle()
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last = f"GET {path}: {type(exc).__name__}"
            else:
                if resp.is_success:
                    return resp.json()
                last = f"GET {path}: HTTP {resp.status_code}"
                if resp.status_code < 500:
                    break
            if attempt < ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * 2**attempt)
        raise DocExtractorError(last)

    async def _list_all(self, path: str, key: str) -> list[dict]:
        items: list[dict] = []
        while True:
            page = await self._get(path, {"skip": len(items), "limit": PAGE_LIMIT})
            batch = page[key]
            items.extend(batch)
            if not batch or len(items) >= page["total"]:
                return items

    async def list_vendors(self) -> list[dict]:
        return await self._list_all("/vendors", "vendors")

    async def list_products(self) -> list[dict]:
        return await self._list_all("/products", "products")

    async def list_sources(self) -> list[dict]:
        return await self._list_all("/sources", "sources")

    async def get_toc(self, source_id: str) -> list[dict]:
        return (await self._get(f"/articles/toc/{source_id}"))["entries"]

    async def search_articles(self, q: str, source_id: str | None = None) -> AsyncIterator[dict]:
        params: dict = {"q": q, "limit": PAGE_LIMIT}
        if source_id:
            params["source_id"] = source_id
        while True:
            page = await self._get("/articles", params)
            for item in page["articles"]:
                yield item
            if not page.get("has_more") or not page.get("next_cursor"):
                return
            params = {**params, "cursor": page["next_cursor"]}

    async def get_article(self, article_id: str) -> dict:
        return await self._get(f"/articles/{article_id}")
