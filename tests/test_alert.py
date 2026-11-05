from sqlalchemy import select

from release_watch.models import Digest
from release_watch.notify.alert import send_alert


class DownTeams:
    async def post(self, card):
        raise ConnectionError("teams down")


async def test_alert_is_kept_for_the_web_even_when_teams_is_down(session_factory):
    try:
        await send_alert(DownTeams(), session_factory, "Release Watch: the weekly login to A failed (x).")
    except ConnectionError:
        pass
    with session_factory() as s:
        [d] = s.scalars(select(Digest))
    assert "weekly login to A failed" in str(d.cards)
