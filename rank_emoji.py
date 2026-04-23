"""Per-guild custom rank emoji lookups.

Populated by the `/upload-rank-emojis` admin command (see cogs/setup.py).
Other modules call `markdown_for(guild_id, rank_name)` to get a Discord
emoji reference (`<:t8_tekken_emperor:1234567890>`) for inline display,
or an empty string when the guild hasn't run the uploader yet.
"""
from __future__ import annotations

import db


async def markdown_for(guild_id: int, rank_name: str | None) -> str:
    """Return custom-emoji markdown for a rank in this guild, or an
    empty string if no emoji has been uploaded for that rank (or the
    rank_name itself is None).

    Callers should treat the return value as a prefix-insertable
    string — safe to concatenate into embed text or message content
    without breaking layout when the emoji is missing.
    """
    if rank_name is None:
        return ""
    row = await db.get_rank_emoji(guild_id, rank_name)
    if row is None:
        return ""
    return f"<:{row['emoji_name']}:{row['emoji_id']}>"
