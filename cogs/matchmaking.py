"""Matchmaking regional LFG panels.

Each `#matchmaking-<region>` channel gets a pinned banner (provisioned by
/setup-server) with a single "I'm Looking for Games" button. Clicking
posts an LFG message in the channel, tagged with the caller's rank +
main character, and auto-deletes after LFG_LIFETIME so the feed stays
current without manual janitor work.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.ext import commands

import channel_util
import db
import rank_emoji
from cogs.onboarding import VERIFIED_ROLE_NAME
from view_util import ErrorHandledView

log = logging.getLogger(__name__)

LFG_LIFETIME = timedelta(minutes=30)

REGION_LABELS: dict[str, str] = {
    "matchmaking-na":   "🇺🇸 NA",
    "matchmaking-eu":   "🇪🇺 EU",
    "matchmaking-asia": "🌏 Asia",
    "matchmaking-oce":  "🦘 OCE",
}


class LFGPanelView(ErrorHandledView):
    """Persistent 'I'm Looking for Games' button — one registration serves
    every matchmaking channel's banner because the callback resolves the
    region from `interaction.channel.name`."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I'm Looking for Games", emoji="🎮",
        style=discord.ButtonStyle.success,
        custom_id="mm:lfg",
    )
    async def lfg(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_lfg(interaction)


async def _flow_lfg(interaction: discord.Interaction) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return

    if not any(r.name == VERIFIED_ROLE_NAME for r in member.roles):
        await interaction.response.send_message(
            f"🔒 You need the **{VERIFIED_ROLE_NAME}** role to post LFG. "
            "Head to **#player-hub** and click **Verify** to link your "
            "Tekken ID.",
            ephemeral=True, delete_after=15,
        )
        return

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "Wrong channel type.", ephemeral=True, delete_after=8)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Snapshot rank + character for the LFG ping.
    player = await db.get_player_by_discord(member.id)
    rank = player["rank_tier"] if player else None
    main_char = player["main_char"] if player else None

    # Inline custom rank emoji if the guild has run /upload-rank-emojis,
    # otherwise empty string — graceful degradation.
    rank_emoji_md = await rank_emoji.markdown_for(interaction.guild_id, rank)

    tag_bits: list[str] = []
    if rank:
        prefix = f"{rank_emoji_md} " if rank_emoji_md else ""
        tag_bits.append(f"{prefix}**{rank}**")
    if main_char:
        tag_bits.append(main_char)
    tag = " · ".join(tag_bits) if tag_bits else "Unranked"

    # Strip any emoji prefix before looking up the pretty region label.
    # Keeps REGION_LABELS indexed by the stable base name.
    region_label = REGION_LABELS.get(
        channel_util.base_name_of(channel), channel.name,
    )

    lfg_content = (
        f"🎮 {member.mention} is **looking for games** in {region_label}!\n"
        f"{tag} · Auto-clears in 30 min · @mention or DM to play."
    )

    try:
        await channel.send(
            lfg_content,
            delete_after=int(LFG_LIFETIME.total_seconds()),
            allowed_mentions=discord.AllowedMentions(
                users=[member], roles=False, everyone=False,
            ),
        )
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"Couldn't post LFG: {e}", ephemeral=True)
        return

    await interaction.followup.send(
        "✅ LFG posted — auto-clears in 30 min. Click again any time to refresh.",
        ephemeral=True,
    )


class Matchmaking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(LFGPanelView())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Matchmaking(bot))
