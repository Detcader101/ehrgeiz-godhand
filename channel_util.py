"""Channel lookup helpers robust to emoji prefixes.

SERVER_PLAN in cogs/setup.py brands channel names with emoji prefixes
like `🏆-tournaments`. Rest of the codebase (tournament cog,
matchmaking, audit logs, banner provisioner) still wants to find
channels by their semantic base name — "tournaments", "mod-log", etc.
These helpers match either form so a rename in SERVER_PLAN doesn't
shatter lookups everywhere else.
"""
from __future__ import annotations

import discord


def find_text_channel(
    guild: discord.Guild, base_name: str,
) -> discord.TextChannel | None:
    """Return the text channel whose name equals `base_name` or ends
    with `-{base_name}` (i.e. has an emoji prefix like `🏆-`). None if
    no such channel exists in this guild."""
    for ch in guild.text_channels:
        if ch.name == base_name:
            return ch
        if ch.name.endswith(f"-{base_name}"):
            return ch
    return None


def base_name_of(channel: discord.abc.GuildChannel) -> str:
    """Strip a leading emoji-dash prefix to recover the semantic base
    name. `🏆-tournaments` -> `tournaments`. If the channel has no
    prefix the name is returned unchanged."""
    name = channel.name
    if "-" not in name:
        return name
    # The prefix is considered "emoji" if it contains any non-ASCII
    # character — plain-ascii channel names like `off-topic` keep their
    # dashes intact.
    prefix, _, rest = name.partition("-")
    if prefix and any(ord(c) > 127 for c in prefix):
        return rest
    return name
