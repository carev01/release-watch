import httpx
import pytest
import respx

from release_watch.docextractor import DocExtractorClient, DocExtractorError

BASE = "http://de.test/api"
KEY = "dxk_secret"


def client(**kw):
    return DocExtractorClient(BASE, KEY, max_rps=1000, **kw)


@respx.mock
async def test_list_products_paginates():
    route = respx.get(f"{BASE}/products").mock(side_effect=[
        httpx.Response(200, json={"products": [{"id": i} for i in range(200)], "total": 250}),
        httpx.Response(200, json={"products": [{"id": i} for i in range(200, 250)], "total": 250}),
    ])
    async with client() as c:
        items = await c.list_products()
    assert len(items) == 250
    second = route.calls[1].request.url.params
    assert second["skip"] == "200" and second["limit"] == "200"


@respx.mock
async def test_api_key_header_sent():
    route = respx.get(f"{BASE}/vendors").mock(
        return_value=httpx.Response(200, json={"vendors": [], "total": 0}))
    async with client() as c:
        await c.list_vendors()
    assert route.calls[0].request.headers["X-API-Key"] == KEY


@respx.mock
async def test_search_follows_cursor():
    route = respx.get(f"{BASE}/articles").mock(side_effect=[
        httpx.Response(200, json={"articles": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}),
        httpx.Response(200, json={"articles": [{"id": "b"}], "has_more": False, "next_cursor": None}),
    ])
    async with client() as c:
        items = [a async for a in c.search_articles("release notes")]
    assert [a["id"] for a in items] == ["a", "b"]
    params = route.calls[1].request.url.params
    assert params["cursor"] == "c1" and params["q"] == "release notes"


@respx.mock
async def test_5xx_retried_then_raises(monkeypatch):
    monkeypatch.setattr("release_watch.docextractor.RETRY_BASE_DELAY", 0)
    route = respx.get(f"{BASE}/vendors").mock(return_value=httpx.Response(503))
    async with client() as c:
        with pytest.raises(DocExtractorError):
            await c.list_vendors()
    assert route.call_count == 3


@respx.mock
async def test_4xx_not_retried():
    route = respx.get(f"{BASE}/vendors").mock(return_value=httpx.Response(401, text="bad key dxk_secret"))
    async with client() as c:
        with pytest.raises(DocExtractorError) as err:
            await c.list_vendors()
    assert route.call_count == 1
    assert KEY not in str(err.value)
    assert "401" in str(err.value)


@respx.mock
async def test_short_pagination_read_raises():
    respx.get(f"{BASE}/sources").mock(side_effect=[
        httpx.Response(200, json={"sources": [{"id": 1}], "total": 3}),
        httpx.Response(200, json={"sources": [], "total": 3}),
    ])
    async with client() as c:
        with pytest.raises(DocExtractorError, match="1 of 3"):
            await c.list_sources()
