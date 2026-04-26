"""Banner orphan-sweep tests for /setup-server.

Real-world scenario these guard against: a redeploy into the same
Discord guild with a fresh bot.db (e.g. moving from a local dev DB to
the shed-tekken bind-mount). The DB has no panel rows but the prior
banners are still pinned in the channels — re-running /setup-server
without an orphan sweep produces visible duplicates.

The pin sweep in `_post_or_refresh_banner` looks at `channel.pins()`,
keeps the message tracked in db.panels (if any), and deletes other
bot-authored pins that carry a banner.png attachment.
"""
from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest

import db
import cogs.setup as setup_mod


def _pinned_message(*, msg_id: int, author_id: int, has_banner: bool):
    """Build a fake pinned message with the surface attributes the sweep
    actually reads (author.id, id, attachments[*].filename, delete())."""
    msg = MagicMock()
    msg.id = msg_id
    msg.author = MagicMock()
    msg.author.id = author_id
    if has_banner:
        attachment = MagicMock()
        attachment.filename = "banner.png"
        msg.attachments = [attachment]
    else:
        msg.attachments = []
    msg.delete = AsyncMock()
    return msg


@pytest.fixture
def stub_render(monkeypatch):
    """Skip the real Pillow render — return a tiny in-memory PNG buffer."""
    async def _fake_render(**kwargs):
        return BytesIO(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(
        setup_mod.tournament_render, "render_banner", _fake_render,
    )


@pytest.fixture
def fake_channel():
    """A channel with: configurable pins, an awaitable send/edit/pin chain,
    and a name so log lines have something printable."""
    ch = MagicMock()
    ch.name = "rules"
    ch.id = 100_000_001
    ch._pins: list = []

    async def _pins():
        return list(ch._pins)
    ch.pins = _pins

    async def _send(**kwargs):
        m = MagicMock()
        m.id = 555_000_001
        m.pinned = False
        m.pin = AsyncMock()
        m.edit = AsyncMock()
        return m
    ch.send = _send
    return ch


@pytest.fixture
def fake_guild(fake_channel, monkeypatch):
    """Guild with the bot present (so .me.id is the author filter), the
    fake channel reachable via channel_util.find_text_channel, and
    pin-notification cleanup stubbed (it tries to read recent history)."""
    guild = MagicMock()
    guild.id = 999_000_001
    guild.me = MagicMock()
    guild.me.id = 7_777_777  # bot's own user id
    guild.get_channel = lambda cid: fake_channel if cid == fake_channel.id else None

    monkeypatch.setattr(
        setup_mod.channel_util, "find_text_channel",
        lambda g, name: fake_channel,
    )
    # _delete_pin_notification tries to scan history; bypass it.
    monkeypatch.setattr(
        setup_mod, "_delete_pin_notification", AsyncMock(),
    )
    return guild


@pytest.fixture
def banner_spec():
    """One BannerSpec is enough to drive _post_or_refresh_banner."""
    return setup_mod.BannerSpec(
        channel_name="rules", kind="banner_rules",
        kicker="K", title="T", subtitle="S", body="B",
    )


@pytest.fixture
def report():
    return setup_mod.SetupReport()


@pytest.mark.asyncio
async def test_orphan_pin_is_deleted_when_db_has_no_row(
    tmp_db, stub_render, fake_guild, fake_channel, banner_spec, report,
):
    """Empty panels table + an existing bot-authored banner pinned in the
    channel = the orphan must be deleted before the fresh post lands."""
    orphan = _pinned_message(
        msg_id=111, author_id=fake_guild.me.id, has_banner=True,
    )
    fake_channel._pins = [orphan]

    await setup_mod._post_or_refresh_banner(fake_guild, banner_spec, report)

    orphan.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_tracked_pin_is_preserved(
    tmp_db, stub_render, fake_guild, fake_channel, banner_spec, report,
):
    """The pin we already track in db.panels must NOT be swept — the
    code path is supposed to edit it in place, not delete and repost."""
    tracked_id = 222
    await db.set_panel(
        fake_guild.id, banner_spec.kind, fake_channel.id, tracked_id,
    )
    tracked_pin = _pinned_message(
        msg_id=tracked_id, author_id=fake_guild.me.id, has_banner=True,
    )
    # Make the in-place edit succeed by handing it back from fetch_message.
    tracked_pin.edit = AsyncMock()
    tracked_pin.pinned = True
    fake_channel.fetch_message = AsyncMock(return_value=tracked_pin)
    fake_channel._pins = [tracked_pin]

    await setup_mod._post_or_refresh_banner(fake_guild, banner_spec, report)

    tracked_pin.delete.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_swept_alongside_tracked_edit(
    tmp_db, stub_render, fake_guild, fake_channel, banner_spec, report,
):
    """Both at once: tracked banner from a prior run is edited in place,
    orphan from an even earlier run gets deleted in the same pass."""
    tracked_id = 333
    await db.set_panel(
        fake_guild.id, banner_spec.kind, fake_channel.id, tracked_id,
    )
    tracked_pin = _pinned_message(
        msg_id=tracked_id, author_id=fake_guild.me.id, has_banner=True,
    )
    tracked_pin.edit = AsyncMock()
    tracked_pin.pinned = True
    fake_channel.fetch_message = AsyncMock(return_value=tracked_pin)

    orphan = _pinned_message(
        msg_id=999, author_id=fake_guild.me.id, has_banner=True,
    )
    fake_channel._pins = [tracked_pin, orphan]

    await setup_mod._post_or_refresh_banner(fake_guild, banner_spec, report)

    tracked_pin.delete.assert_not_called()
    orphan.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_bot_pins_are_left_alone(
    tmp_db, stub_render, fake_guild, fake_channel, banner_spec, report,
):
    """A staff-pinned message in the same channel must survive the
    sweep — the filter is `author.id == bot.id`, deliberately tight."""
    user_pin = _pinned_message(
        msg_id=444, author_id=12_345_678, has_banner=True,  # not the bot
    )
    fake_channel._pins = [user_pin]

    await setup_mod._post_or_refresh_banner(fake_guild, banner_spec, report)

    user_pin.delete.assert_not_called()


@pytest.mark.asyncio
async def test_bot_pin_without_banner_attachment_is_left_alone(
    tmp_db, stub_render, fake_guild, fake_channel, banner_spec, report,
):
    """Sweep is keyed on banner.png — a bot pin from some other feature
    (e.g. a pinned tournament announcement) must not get hoovered up."""
    other_bot_pin = _pinned_message(
        msg_id=555, author_id=fake_guild.me.id, has_banner=False,
    )
    fake_channel._pins = [other_bot_pin]

    await setup_mod._post_or_refresh_banner(fake_guild, banner_spec, report)

    other_bot_pin.delete.assert_not_called()


@pytest.mark.asyncio
async def test_pins_fetch_failure_does_not_break_posting(
    tmp_db, stub_render, fake_guild, fake_channel, banner_spec, report,
):
    """If channel.pins() fails (Forbidden / HTTPException), the function
    must still post a fresh banner instead of erroring out — the sweep
    is best-effort."""
    import discord as discord_mod

    async def _raises():
        raise discord_mod.Forbidden(MagicMock(status=403, reason="x"), "no")
    fake_channel.pins = _raises

    await setup_mod._post_or_refresh_banner(fake_guild, banner_spec, report)

    # No errors recorded for the actual post path.
    assert not any("Banner" in e for e in report.errors), report.errors
