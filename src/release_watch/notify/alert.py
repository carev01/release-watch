"""Immediate one-line messages (spec 9): to Teams, and kept on the web Notifications page."""

from release_watch.models import Digest
from release_watch.notify.cards import text_card


async def send_alert(teams, session_factory, text: str) -> None:
    card = text_card(text)
    if teams is not None:
        await teams.post(card)
    with session_factory() as s:
        s.add(Digest(channel="teams" if teams else "web", cards=[card]))
        s.commit()
