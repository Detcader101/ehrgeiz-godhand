"""Audit log helper (spec §5.1, §9).

Posts structured events to one of two staff-only channels:
  - #verification-log — identity actions (link/unlink/rank changes/admin
    overrides). Spec §5.1.
  - #mod-log — moderation actions (kick/ban/timeout/warn/purge). Spec §9.

Channels are found by name; both are provisioned by the standard
/setup-server in the staff-only category.

If the target channel doesn't exist or the bot can't post, the call
no-ops — audit logging is best-effort and must never break a user flow.

When the à-la-carte setup rework lands, channel names (and staff-only
vs public) become per-guild preferences.
"""
from __future__ import annotations

import logging

import discord

import channel_util

VERIFICATION_LOG_CHANNEL = "verification-log"
MOD_LOG_CHANNEL = "mod-log"
# Low-priority audit feed: routine bot events that don't need mod
# attention but should still leave a trail. Fit-check posts/deletes,
# Drip Lord rotations, future low-stakes feature events all go here so
# #mod-log stays readable as a "things that need a human" channel.
MOD_LOG_DUMP_CHANNEL = "mod-log-dump"

log = logging.getLogger(__name__)


async def post_event(
    guild: discord.Guild | None,
    *,
    title: str,
    color: discord.Color,
    fields: list[tuple[str, str, bool]] | None = None,
    description: str | None = None,
    channel_name: str = VERIFICATION_LOG_CHANNEL,
) -> None:
    if guild is None:
        return
    # Matches both the bare name and any emoji-prefixed variant
    # (`🛡️-mod-log`, `🔍-verification-log`) so SERVER_PLAN rebranding
    # doesn't silently break audit logging.
    channel = channel_util.find_text_channel(guild, channel_name)
    if channel is None:
        return
    embed = discord.Embed(
        title=title, description=description, color=color,
        timestamp=discord.utils.utcnow(),
    )
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=value, inline=inline)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(
            "audit log post failed in guild %s (#%s): %s",
            guild.id, channel_name, e,
        )


async def post_mod_event(guild: discord.Guild | None, **kwargs) -> None:
    """Convenience wrapper: post to #mod-log instead of #verification-log."""
    await post_event(guild, channel_name=MOD_LOG_CHANNEL, **kwargs)


async def post_dump_event(guild: discord.Guild | None, **kwargs) -> None:
    """Convenience wrapper: post to #mod-log-dump (low-priority feed).
    Falls back silently if the dump channel hasn't been provisioned yet
    so an older guild without it doesn't error on routine events."""
    await post_event(guild, channel_name=MOD_LOG_DUMP_CHANNEL, **kwargs)
