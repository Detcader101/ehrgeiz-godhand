"""Audit log helper (spec §5.1).

Posts structured events to a guild's verification-log channel. The
channel is found by name (`#verification-log`), which the standard
/setup-server provisions in the staff-only category.

If the channel doesn't exist or the bot can't post, the call no-ops —
audit logging is best-effort and must never break a user-facing flow.

When the à-la-carte setup rework lands, the channel name (and whether
it's staff-only or public) becomes a per-guild preference.
"""
from __future__ import annotations

import logging

import discord

VERIFICATION_LOG_CHANNEL = "verification-log"

log = logging.getLogger(__name__)


async def post_event(
    guild: discord.Guild | None,
    *,
    title: str,
    color: discord.Color,
    fields: list[tuple[str, str, bool]] | None = None,
    description: str | None = None,
) -> None:
    if guild is None:
        return
    channel = discord.utils.get(guild.text_channels, name=VERIFICATION_LOG_CHANNEL)
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
        log.warning("audit log post failed in guild %s: %s", guild.id, e)
