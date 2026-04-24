"""Moderation cog (spec §9).

First command: /shutup — combined "purge their last few messages here +
short timeout." Designed for the common case where someone is spamming
or being a nuisance and you want one click to deal with both halves.

Two classes of caller can use /shutup:

  - **Moderators** (Discord perms: Moderate Members AND Manage Messages,
    or Administrator). No rate limit. Standard top-role hierarchy
    check on the target.

  - **Silencerz** (members holding the marker role `The Silencerz`,
    provisioned by /setup-server). Rate-limited to one /shutup per
    hour per silencer per guild, tracked in the `shutup_uses` table.
    Cannot /shutup mods, admins, or other Silencerz. Cooldown is only
    consumed on a *successful* shutup so failed attempts on protected
    targets don't burn the hour.

Future commands per the spec: /kick, /ban, /timeout, /warn,
/warnings, /purge. Same patterns; same #mod-log audit destination.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import audit
import db
import view_util

log = logging.getLogger(__name__)

# /shutup tunables. Kept as module constants rather than env so they
# don't fragment per-deployment — same UX everywhere the bot runs.
SHUTUP_TIMEOUT = timedelta(minutes=2)
SHUTUP_PURGE_COUNT = 5
SHUTUP_SCAN_DEPTH = 50  # how far back in channel history to look

# The Silencerz role: marker role provisioned by /setup-server. No Discord
# perms; authority is enforced here.
SILENCERZ_ROLE_NAME = "The Silencerz"
SILENCER_COOLDOWN = timedelta(hours=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_remaining(td: timedelta) -> str:
    total_s = int(td.total_seconds())
    days, rem = divmod(total_s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m"
    return "less than a minute"


def _is_mod(member: discord.Member) -> bool:
    """Discord-perm-based mod check: Manage Messages + Moderate Members,
    or full Administrator."""
    p = member.guild_permissions
    if p.administrator:
        return True
    return p.moderate_members and p.manage_messages


def _is_silencer(member: discord.Member) -> bool:
    return any(r.name == SILENCERZ_ROLE_NAME for r in member.roles)


def _silencer_cooldown_remaining(last_used_at_iso: str) -> timedelta | None:
    try:
        last = datetime.fromisoformat(last_used_at_iso)
    except ValueError:
        return None
    elapsed = datetime.now(timezone.utc) - last
    if elapsed >= SILENCER_COOLDOWN:
        return None
    return SILENCER_COOLDOWN - elapsed


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        await view_util.handle_app_command_error(interaction, error, log)

    @app_commands.command(
        name="shutup",
        description="Delete a member's last 5 messages here and time them out for 2 minutes.",
    )
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

        invoker = interaction.user
        if not isinstance(invoker, discord.Member):
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return

        # Authority resolution. Mods take precedence over silencer status —
        # a member who is both gets the unrate-limited mod path.
        if _is_mod(invoker):
            authority = "mod"
        elif _is_silencer(invoker):
            authority = "silencer"
        else:
            await interaction.response.send_message(
                "You need either Moderator permissions (Moderate Members + "
                f"Manage Messages) or **{SILENCERZ_ROLE_NAME}** role to use "
                "`/shutup`.",
                ephemeral=True, delete_after=12,
            )
            return

        # Sanity guards (apply to both authority types).
        if member.id == invoker.id:
            await interaction.response.send_message(
                "You can't /shutup yourself.", ephemeral=True, delete_after=8,
            )
            return
        if self.bot.user is not None and member.id == self.bot.user.id:
            await interaction.response.send_message(
                "Hey.", ephemeral=True, delete_after=8,
            )
            return

        # Authority-specific target restrictions.
        if authority == "mod":
            # Top-role hierarchy. Skip for admins.
            if (not invoker.guild_permissions.administrator
                    and invoker.top_role <= member.top_role):
                await interaction.response.send_message(
                    f"You can't /shutup {member.mention} — they have an "
                    "equal or higher role than you.",
                    ephemeral=True, delete_after=10,
                )
                return
        else:
            # Silencerz can't shutup mods, admins, or other Silencerz.
            if _is_mod(member):
                await interaction.response.send_message(
                    f"{SILENCERZ_ROLE_NAME} can't /shutup moderators or "
                    "admins.",
                    ephemeral=True, delete_after=10,
                )
                return
            if _is_silencer(member):
                await interaction.response.send_message(
                    f"{SILENCERZ_ROLE_NAME} can't /shutup each other. "
                    "No infighting.",
                    ephemeral=True, delete_after=10,
                )
                return

            # Cooldown gate. Cooldown is *only* consumed on a successful
            # action below — failed attempts on protected targets don't
            # burn the hour.
            last_use = await db.get_last_shutup_use(invoker.id, guild.id)
            if last_use is not None:
                remaining = _silencer_cooldown_remaining(last_use["last_used_at"])
                if remaining is not None:
                    embed = discord.Embed(
                        title="🔇 Silencer cooldown active",
                        description=(
                            f"Members of **{SILENCERZ_ROLE_NAME}** can use "
                            f"`/shutup` once per hour.\n\n"
                            f"You can use it again in **{_format_remaining(remaining)}**."
                        ),
                        color=discord.Color.orange(),
                    )
                    await interaction.response.send_message(
                        embed=embed, ephemeral=True,
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

        # "Did anything land" — used for both the user message and to decide
        # whether a Silencer should burn their cooldown.
        action_landed = bool(deleted) or not timeout_error

        # Silencer cooldown is consumed only on a successful action.
        if authority == "silencer" and action_landed:
            await db.record_shutup_use(invoker.id, guild.id, _now_iso())

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
        suffix = ""
        if authority == "silencer":
            if action_landed:
                suffix = "\n*Silencer cooldown started — next use in 1h.*"
            else:
                suffix = "\n*Cooldown not consumed (no action landed).*"
        await interaction.followup.send(
            f"{verb} {member.mention}: " + "; ".join(bits) + "." + suffix,
            ephemeral=True,
        )

        authority_label = (
            "Moderator" if authority == "mod"
            else f"{SILENCERZ_ROLE_NAME} (1/h)"
        )
        await audit.post_mod_event(
            guild,
            title="/shutup",
            color=(discord.Color.dark_red() if timeout_error
                   else discord.Color.red()),
            fields=[
                ("Target", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{invoker.mention} (`{invoker.id}`)", True),
                ("Authority", authority_label, True),
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
