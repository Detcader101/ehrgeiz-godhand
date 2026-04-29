"""Behaviour spec for audit.py.

Two contracts the rest of the bot relies on:

  * `notify_user_dm` is best-effort — it never raises, and reports back
    whether the DM landed so the calling admin command can surface
    "(DM'd)" / "(couldn't DM)" to staff.
  * `post_event` resolves the audit channel by base name AND by emoji
    prefix, so SERVER_PLAN's `🔍-verification-log` rebrand doesn't
    silently drop audit posts.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import audit


# --------------------------------------------------------------------------- #
# notify_user_dm                                                               #
# --------------------------------------------------------------------------- #

def _fake_member() -> MagicMock:
    member = MagicMock()
    member.id = 1
    member.send = AsyncMock(return_value=MagicMock())
    return member


@pytest.mark.asyncio
async def test_returns_true_when_dm_is_delivered():
    member = _fake_member()
    delivered = await audit.notify_user_dm(
        member, title="Linked", description="You are now verified.",
    )
    assert delivered is True


@pytest.mark.asyncio
async def test_returns_false_when_user_has_dms_closed():
    member = _fake_member()
    member.send.side_effect = discord.Forbidden(MagicMock(), "DMs closed")

    delivered = await audit.notify_user_dm(
        member, title="t", description="d",
    )
    assert delivered is False


@pytest.mark.asyncio
async def test_returns_false_on_transient_http_error():
    member = _fake_member()
    member.send.side_effect = discord.HTTPException(MagicMock(), "boom")

    delivered = await audit.notify_user_dm(
        member, title="t", description="d",
    )
    assert delivered is False


@pytest.mark.asyncio
async def test_does_not_raise_when_member_is_none():
    # Admin commands pass `guild.get_member(...)` which can be None for
    # users who left the guild — the helper must absorb that.
    delivered = await audit.notify_user_dm(
        None, title="t", description="d",
    )
    assert delivered is False


@pytest.mark.asyncio
async def test_caller_supplied_fields_appear_in_dm():
    member = _fake_member()
    fields = [("Action", "Linked", True), ("Polaris", "abc123", False)]

    await audit.notify_user_dm(
        member, title="t", description="d", fields=fields,
    )

    embed = member.send.await_args.kwargs["embed"]
    assert [(f.name, f.value, f.inline) for f in embed.fields] == fields


# --------------------------------------------------------------------------- #
# post_event channel resolution                                                #
# --------------------------------------------------------------------------- #

def _channel(name: str) -> MagicMock:
    ch = MagicMock()
    ch.name = name
    ch.send = AsyncMock()
    return ch


def _guild_with(*channels: MagicMock) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.text_channels = list(channels)
    return guild


@pytest.mark.asyncio
async def test_posts_to_channel_named_exactly_verification_log():
    ch = _channel("verification-log")
    await audit.post_event(
        _guild_with(ch), title="x", color=discord.Color.green(),
    )
    ch.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_posts_to_channel_with_emoji_prefix():
    # Regression — bare `discord.utils.get(name=...)` couldn't match
    # `🔍-verification-log` and audit posts silently disappeared in prod.
    ch = _channel("🔍-verification-log")
    await audit.post_event(
        _guild_with(ch), title="x", color=discord.Color.green(),
    )
    ch.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_op_when_audit_channel_missing():
    # Nothing in the guild matches — no exception, no error.
    await audit.post_event(
        _guild_with(_channel("general")),
        title="x", color=discord.Color.green(),
    )


@pytest.mark.asyncio
async def test_no_op_when_guild_is_none():
    await audit.post_event(None, title="x", color=discord.Color.green())


@pytest.mark.asyncio
async def test_swallows_send_failures():
    # Best-effort: a Forbidden during send must not bubble up and crash
    # the user-facing command that triggered the audit log.
    ch = _channel("verification-log")
    ch.send.side_effect = discord.Forbidden(MagicMock(), "no perms")

    await audit.post_event(
        _guild_with(ch), title="x", color=discord.Color.green(),
    )


@pytest.mark.asyncio
async def test_post_mod_event_targets_mod_log_not_verification_log():
    mod_ch = _channel("🛡️-mod-log")
    verif_ch = _channel("🔍-verification-log")

    await audit.post_mod_event(
        _guild_with(verif_ch, mod_ch),
        title="ban", color=discord.Color.red(),
    )

    mod_ch.send.assert_awaited_once()
    verif_ch.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_dump_event_targets_dump_channel():
    dump_ch = _channel("📦-mod-log-dump")
    await audit.post_dump_event(
        _guild_with(dump_ch),
        title="fitcheck", color=discord.Color.blurple(),
    )
    dump_ch.send.assert_awaited_once()
