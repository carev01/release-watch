import json

import httpx
import pytest
import respx

from release_watch.config import Settings
from release_watch.llm import LLMClient, LLMError

AZURE = "https://e.openai.azure.com"
AZURE_URL = f"{AZURE}/openai/v1/chat/completions"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"


def settings(endpoint=AZURE):
    return Settings(database_url="postgresql+psycopg://x@y/z", docextractor_api_key="k",
                    azure_openai_endpoint=endpoint, azure_openai_api_key="az-key",
                    openrouter_api_key="or-key")


def reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@respx.mock
async def test_azure_success():
    azure = respx.post(AZURE_URL).mock(return_value=reply('{"a": 1}'))
    openrouter = respx.post(OR_URL).mock(return_value=reply('{"b": 2}'))
    llm = LLMClient(settings())
    assert await llm.complete_json("sys", "user") == {"a": 1}
    assert llm.last_provider == "azure"
    assert not openrouter.called
    req = azure.calls[0].request
    assert req.headers["api-key"] == "az-key"
    body = json.loads(req.content)
    assert body["model"] == "gpt-5-mini"
    assert body["response_format"] == {"type": "json_object"}


@respx.mock
async def test_falls_back_on_azure_500():
    respx.post(AZURE_URL).mock(return_value=httpx.Response(500))
    openrouter = respx.post(OR_URL).mock(return_value=reply('{"b": 2}'))
    llm = LLMClient(settings())
    assert await llm.complete_json("sys", "user") == {"b": 2}
    assert llm.last_provider == "openrouter"
    assert openrouter.calls[0].request.headers["Authorization"] == "Bearer or-key"


@respx.mock
async def test_falls_back_on_malformed_json():
    respx.post(AZURE_URL).mock(return_value=reply("not json"))
    respx.post(OR_URL).mock(return_value=reply('{"b": 2}'))
    llm = LLMClient(settings())
    assert await llm.complete_json("sys", "user") == {"b": 2}
    assert llm.last_provider == "openrouter"


@respx.mock
async def test_both_fail_raises():
    respx.post(AZURE_URL).mock(return_value=httpx.Response(500))
    respx.post(OR_URL).mock(return_value=reply("[1, 2]"))
    with pytest.raises(LLMError) as err:
        await LLMClient(settings()).complete_json("sys", "user")
    assert "az-key" not in str(err.value) and "or-key" not in str(err.value)


@respx.mock
async def test_no_azure_config_uses_openrouter():
    azure = respx.post(AZURE_URL).mock(return_value=reply('{"a": 1}'))
    respx.post(OR_URL).mock(return_value=reply('{"b": 2}'))
    llm = LLMClient(settings(endpoint=""))
    assert await llm.complete_json("sys", "user") == {"b": 2}
    assert not azure.called
