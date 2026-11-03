"""JSON completions: Azure OpenAI first, OpenRouter as the fallback. Never logs keys or prompts."""

import json
import logging

import httpx

from release_watch.config import Settings

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport
        self.last_provider: str | None = None

    def _providers(self, messages: list[dict], max_tokens: int):
        s = self._settings
        if s.azure_openai_endpoint and s.azure_openai_api_key.get_secret_value():
            yield "azure", (
                f"{s.azure_openai_endpoint.rstrip('/')}/openai/v1/chat/completions",
                {"api-key": s.azure_openai_api_key.get_secret_value()},
                {"model": s.azure_openai_model, "messages": messages,
                 "response_format": {"type": "json_object"},
                 # gpt-5-mini reasons before answering; reasoning tokens count against this budget.
                 "max_completion_tokens": max_tokens, "reasoning_effort": "low"},
            )
        if s.openrouter_api_key.get_secret_value():
            yield "openrouter", (
                OPENROUTER_URL,
                {"Authorization": f"Bearer {s.openrouter_api_key.get_secret_value()}"},
                {"model": s.openrouter_model, "messages": messages,
                 "response_format": {"type": "json_object"}, "max_tokens": max_tokens},
            )

    async def complete_json(self, system: str, user: str, *, max_output_tokens: int = 2000) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        failures = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0), transport=self._transport) as http:
            for name, (url, headers, body) in self._providers(messages, max_output_tokens):
                try:
                    resp = await http.post(url, headers=headers, json=body)
                except httpx.TransportError as exc:
                    failures.append(f"{name}: {type(exc).__name__}")
                    continue
                if not resp.is_success:
                    failures.append(f"{name}: HTTP {resp.status_code}")
                    continue
                try:
                    result = json.loads(resp.json()["choices"][0]["message"]["content"] or "")
                except (ValueError, KeyError, IndexError, TypeError):
                    failures.append(f"{name}: unparseable reply")
                    continue
                if not isinstance(result, dict):
                    failures.append(f"{name}: reply is not a JSON object")
                    continue
                self.last_provider = name
                log.info("llm ok provider=%s", name)
                return result
        log.warning("llm failed: %s", "; ".join(failures))
        raise LLMError("; ".join(failures) or "no LLM provider configured")
