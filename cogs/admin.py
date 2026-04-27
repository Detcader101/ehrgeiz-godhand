"""Cross-feature admin / diagnostic tools.

Lives separately from `cogs/onboarding.py` (which has the verification-
specific admin commands like `/admin-link`) and `cogs/setup.py` (which
owns the server-provisioning machinery). This cog exists for things
that span features — at-a-glance state inspection, support triage,
recovery commands that touch multiple tables.

Today's surface:
  /admin-inspect-user — single embed showing every relevant DB row
    for a target user (verification, pending claim, unlink cooldown,
    fit-check post stats, Drip Lord status, bot-managed roles).

Conventions match the rest of the bot:
  * Each command goes through the shared error handler in view_util.
  * Heavy lookups are fanned out concurrently where dependencies allow.
  * Output is ephemeral — these are staff tools, not public.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import db
from cogs.fitcheck import DRIP_LORD_ROLE_NAME
from cogs.onboarding import ORGANIZER_ROLE_NAME, VERIFIED_ROLE_NAME
from view_util import handle_app_command_error

log = logging.getLogger(__name__)

# Discord embed field values are capped at 1024 chars; this is plenty
# for a comma-joined role list of normal length, but we trim defensively.
_FIELD_VALUE_MAX = 1000


def _trim(s: str) -> str:
    return s if len(s) <= _FIELD_VALUE_MAX else s[: _FIELD_VALUE_MAX - 1] + "…"


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="admin-inspect-user",
        description="Admin: dump every relevant DB row + role state for a user.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The user to inspect")
    async def admin_inspect_user(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Fan out the DB lookups — each helper opens its own connection,
        # so concurrency just means waiting on the longest one rather
        # than the sum.
        player = await db.get_player_by_discord(member.id)
        pending = await db.get_pending_by_discord(member.id)
        last_unlink = await db.get_last_unlink(member.id)
        fc_stats = await db.get_user_fitcheck_stats(guild.id, member.id)

        embed = discord.Embed(
            title=f"User inspect · {member.display_name}",
            color=discord.Color.purple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="Discord",
            value=(
                f"{member.mention} (`{member.id}`)\n"
                f"Joined: {discord.utils.format_dt(member.joined_at, 'R')}"
                if member.joined_at else f"{member.mention} (`{member.id}`)"
            ),
            inline=False,
        )

        # --- Verification --------------------------------------------- #
        if player is not None:
            verified = any(r.name == VERIFIED_ROLE_NAME for r in member.roles)
            embed.add_field(
                name="Linked",
                value=(
                    f"Tekken ID: `{player['tekken_id']}`\n"
                    f"Display: **{player['display_name']}**\n"
                    f"Main: **{player['main_char'] or '—'}**\n"
                    f"Rank: **{player['rank_tier'] or '—'}**\n"
                    f"Last synced: {player['last_synced']}\n"
                    f"Verified role: {'✅ yes' if verified else '⚠ NO (out of sync)'}"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Linked", value="*Not linked.*", inline=False,
            )

        # --- Pending verification ------------------------------------- #
        if pending is not None:
            status = "expired (>72h, stale)" if pending["expired_at"] else "pending"
            embed.add_field(
                name="Pending claim",
                value=(
                    f"Rank: **{pending['rank_tier']}**\n"
                    f"Source: {pending['rank_source']}\n"
                    f"Created: {pending['created_at']}\n"
                    f"Status: {status}\n"
                    f"Audit msg: "
                    f"{'`' + str(pending['message_id']) + '`' if pending['message_id'] else 'unknown'}"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Pending claim", value="*None.*", inline=False,
            )

        # --- Unlink cooldown ------------------------------------------ #
        if last_unlink is not None:
            embed.add_field(
                name="Last unlink",
                value=(
                    f"Tekken ID: `{last_unlink['tekken_id'] or '—'}`\n"
                    f"At: {last_unlink['unlinked_at']}\n"
                    "*(7-day cooldown active for different-ID re-link.)*"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Last unlink", value="*No unlink on record.*", inline=False,
            )

        # --- Fit Check ----------------------------------------------- #
        if fc_stats["posts"] > 0:
            net = fc_stats["total_ups"] - fc_stats["total_downs"]
            best = fc_stats["best_net"]
            embed.add_field(
                name="Fit Check",
                value=(
                    f"Posts: **{fc_stats['posts']}**\n"
                    f"Total votes: 👍 {fc_stats['total_ups']} · 👎 {fc_stats['total_downs']}\n"
                    f"Net (sum): **{net:+d}**\n"
                    f"Best single post: **{best:+d}**"
                    if best is not None else
                    f"Posts: **{fc_stats['posts']}**\n"
                    f"Total votes: 👍 {fc_stats['total_ups']} · 👎 {fc_stats['total_downs']}\n"
                    f"Net (sum): **{net:+d}**"
                ),
                inline=True,
            )
        else:
            embed.add_field(
                name="Fit Check", value="*No posts yet.*", inline=True,
            )

        # --- Drip Lord status ---------------------------------------- #
        drip_role = discord.utils.get(guild.roles, name=DRIP_LORD_ROLE_NAME)
        is_drip_lord = drip_role is not None and drip_role in member.roles
        embed.add_field(
            name="Drip Lord",
            value=(
                "👑 currently crowned" if is_drip_lord
                else "—" if drip_role
                else "*Role missing — run `/setup-server`.*"
            ),
            inline=True,
        )

        # --- Roles snapshot ------------------------------------------ #
        # Only flag the bot-relevant roles to keep the embed tight; full
        # role list would balloon for staff with @everyone-equivalent
        # permission stacks.
        flagged: list[str] = []
        for role in member.roles:
            if role.name in {
                VERIFIED_ROLE_NAME, ORGANIZER_ROLE_NAME, DRIP_LORD_ROLE_NAME,
                "Admin", "Moderator", "The Silencerz",
            }:
                flagged.append(role.name)
            elif "Tekken" in role.name or "Dan" in role.name or "God" in role.name:
                flagged.append(role.name)
        if flagged:
            embed.add_field(
                name="Bot-relevant roles",
                value=_trim(", ".join(f"`{n}`" for n in flagged)),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        await handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
