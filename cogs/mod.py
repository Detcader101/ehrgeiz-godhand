"""Moderation cog (spec §9).

First command: /shutup — combined "purge their last few messages here +
short timeout." Designed for the common case where someone is spamming
or being a nuisance and you want one click to deal with both halves.

Future commands per the spec: /kick, /ban, /timeout, /warn,
/warnings, /purge. Same patterns; same #mod-log audit destination.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import audit

log = logging.getLogger(__name__)

# /shutup tunables. Kept as module constants rather than env so they
# don't fragment per-deployment — same UX everywhere the bot runs.
SHUTUP_TIMEOUT = timedelta(minutes=2)
SHUTUP_PURGE_COUNT = 5
SHUTUP_SCAN_DEPTH = 50  # how far back in channel history to look


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        cmd = interaction.command.name if interaction.command else "<unknown>"
        log.exception("Slash command /%s raised: %s", cmd, error)
        msg = f"⚠ `/{cmd}` failed: `{type(error).__name__}: {error}`\n*Check the bot console for the traceback.*"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    @app_commands.command(
        name="shutup",
        description="Delete a member's last 5 messages here and time them out for 2 minutes.",
    )
    @app_commands.default_permissions(moderate_members=True, manage_messages=True)
    @app_commands.describe(member="The member to silence")
    async def shutup(
        self, interaction: discord.Interaction, member: discord.Member,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return

        # Sanity guards.
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't /shutup yourself.", ephemeral=True, delete_after=8,
            )
            return
        if self.bot.user is not None and member.id == self.bot.user.id:
            await interaction.response.send_message(
                "Hey.", ephemeral=True, delete_after=8,
            )
            return
        # Role hierarchy: don't let a mod /shutup someone with an equal-
        # or-higher top role unless the invoker is a server admin.
        invoker = interaction.user
        if (isinstance(invoker, discord.Member)
                and not invoker.guild_permissions.administrator
                and invoker.top_role <= member.top_role):
            await interaction.response.send_message(
                f"You can't /shutup {member.mention} — they have an equal "
                "or higher role than you.",
                ephemeral=True, delete_after=10,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.channel
        deleted = 0
        purge_error: str | None = None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                target_msgs: list[discord.Message] = []
                async for msg in channel.history(limit=SHUTUP_SCAN_DEPTH):
                    if msg.author.id == member.id:
                        target_msgs.append(msg)
                        if len(target_msgs) >= SHUTUP_PURGE_COUNT:
                            break
                if len(target_msgs) >= 2:
                    # Bulk delete is one API call; falls back to individual
                    # if any message is older than 14 days (Discord limit).
                    try:
                        await channel.delete_messages(target_msgs)
                        deleted = len(target_msgs)
                    except discord.HTTPException:
                        for m in target_msgs:
                            try:
                                await m.delete()
                                deleted += 1
                            except discord.HTTPException:
                                pass
                elif len(target_msgs) == 1:
                    try:
                        await target_msgs[0].delete()
                        deleted = 1
                    except discord.HTTPException as e:
                        purge_error = str(e)
            except discord.Forbidden:
                purge_error = "no permission to delete messages here"
            except discord.HTTPException as e:
                purge_error = str(e)
        else:
            purge_error = "channel type doesn't support purging"

        timeout_error: str | None = None
        try:
            await member.timeout(
                SHUTUP_TIMEOUT,
                reason=f"/shutup by {invoker} ({invoker.id})",
            )
        except discord.Forbidden:
            timeout_error = "no permission (role hierarchy?)"
        except discord.HTTPException as e:
            timeout_error = str(e)

        # Compose ephemeral confirmation back to the invoker.
        bits: list[str] = []
        if deleted:
            bits.append(f"deleted **{deleted}** message{'s' if deleted != 1 else ''}")
        elif purge_error:
            bits.append(f"couldn't purge ({purge_error})")
        else:
            bits.append(f"no messages to delete in the last {SHUTUP_SCAN_DEPTH}")
        if timeout_error:
            bits.append(f"couldn't time out ({timeout_error})")
        else:
            bits.append("timed out for **2 min**")
        verb = "Shut up" if not timeout_error else "Tried to shut up"
        await interaction.followup.send(
            f"{verb} {member.mention}: " + "; ".join(bits) + ".",
            ephemeral=True,
        )

        await audit.post_mod_event(
            guild,
            title="/shutup",
            color=(discord.Color.dark_red() if timeout_error
                   else discord.Color.red()),
            fields=[
                ("Target", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{invoker.mention} (`{invoker.id}`)", True),
                ("Channel",
                 channel.mention if hasattr(channel, "mention") else str(channel),
                 True),
                ("Messages purged", str(deleted) +
                 (f" *(error: {purge_error})*" if purge_error else ""),
                 True),
                ("Timeout",
                 ("2 minutes" if not timeout_error
                  else f"failed: {timeout_error}"),
                 True),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
