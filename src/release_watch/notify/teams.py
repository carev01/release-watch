"""POST Adaptive Cards to the owner's Power Automate webhook. The URL carries a signature: never log it."""

import asyncio
import logging

import httpx

from release_watch.notify.cards import envelope

# httpx logs every request URL at INFO and httpcore at DEBUG; the webhook URL is a credential.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

RETRY_DELAY = 5.0


class TeamsError(Exception):
    pass


class Teams:
    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep):
        self._url, self._transport, self._sleep = url, transport, sleep

    async def post(self, card: dict) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), transport=self._transport) as http:
            for attempt in range(2):
                try:
                    resp = await http.post(self._url, json=envelope(card))
                except httpx.TransportError as exc:
                    if attempt == 0:
                        await self._sleep(RETRY_DELAY)
                        continue
                    raise TeamsError(f"Teams webhook unreachable: {type(exc).__name__}") from None
                if resp.is_success:
                    return
                if attempt == 0 and (resp.status_code == 429 or resp.status_code >= 500):
                    await self._sleep(RETRY_DELAY)
                    continue
                raise TeamsError(f"Teams webhook answered HTTP {resp.status_code}")
