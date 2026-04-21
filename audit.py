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

VERIFICATION_LOG_CHANNEL = "verification-log"
MOD_LOG_CHANNEL = "mod-log"

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
    channel = discord.utils.get(guild.text_channels, name=channel_name)
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
