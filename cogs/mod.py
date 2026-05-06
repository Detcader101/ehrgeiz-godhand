"""Moderation cog (spec §9).

Commands:

  - /shutup — combined purge-here + 2-minute timeout. Available to mods
    (no rate limit) and to **The Silencerz** marker role (1/hour, can't
    target staff). Original "spam hammer" command.
  - /kick, /ban, /timeout, /warn, /warnings, /purge — standard mod tools.
    Each gated by `default_permissions(...)` using the minimum Discord
    permission. All actions log to #mod-log via audit.post_mod_event;
    target is DM'd via audit.notify_user_dm where applicable so they
    learn what happened without staff needing a side ping.

Auto-escalation on /warn: 3rd warning → 1h timeout, 5th → kick. The
escalation runs in the same handler as the warn itself so the audit log
gets one merged "warn + escalation" embed rather than two disjoint posts.
"""
from __future__ import annotations

import logging
import re
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

# /warn auto-escalation tiers. Picked deliberately conservative — three
# strikes = a real cooldown, five = ejection. Both the timeout and kick
# fire automatically inside the warn handler so the mod doesn't have to
# remember to escalate manually.
WARN_ESCALATE_TIMEOUT_AT = 3
WARN_ESCALATE_TIMEOUT_DURATION = timedelta(hours=1)
WARN_ESCALATE_KICK_AT = 5

# Discord-imposed cap on bulk delete in a single request.
PURGE_MAX = 100

# Discord allows up to 28 days of timeout; reject anything longer at
# parse time so the user gets a helpful error instead of an API rejection.
TIMEOUT_MAX = timedelta(days=28)


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
}


def parse_duration(text: str) -> timedelta:
    """Parse '15m', '1h', '2d', '30s', '1w' into a timedelta.

    Raises ValueError on unparseable input or a duration of zero.
    Caller is responsible for the upper-bound check (Discord caps timeouts
    at 28 days; warn-escalation uses a fixed 1h regardless)."""
    if not text:
        raise ValueError("empty duration")
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(
            f"can't parse {text!r}; use forms like '15m', '1h', '2d'",
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if n <= 0:
        raise ValueError("duration must be positive")
    return timedelta(seconds=n * _DURATION_UNITS[unit])


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


    # ----------------------------------------------------------------- #
    # /kick / /ban / /timeout / /warn / /warnings / /purge               #
    # ----------------------------------------------------------------- #
    #
    # Permission gating uses Discord's `default_permissions(...)` rather
    # than in-code role checks (per spec §9). Discord enforces this at
    # the API layer — the slash command literally won't appear for users
    # without the named permission. We still apply hierarchy checks in
    # the handler since `default_permissions` doesn't stop a high-perm
    # user targeting someone equal/above them in role order.

    @app_commands.command(
        name="kick",
        description="Kick a member from the server.",
    )
    @app_commands.default_permissions(kick_members=True)
    @app_commands.describe(
        member="The member to kick",
        reason="Why? (shown in audit log + DM to the member)",
    )
    async def kick(
        self, interaction: discord.Interaction,
        member: discord.Member, reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        invoker = interaction.user
        if guild is None or not isinstance(invoker, discord.Member):
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        if not _hierarchy_ok(invoker, member, interaction):
            await interaction.response.send_message(
                _hierarchy_msg(member), ephemeral=True, delete_after=10,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        # DM *before* kicking — once they're out of the guild, mutual-server
        # check inside notify_user_dm starts depending on whether we share
        # any other guild with them. Sending first is the simplest path.
        dmed = await audit.notify_user_dm(
            member,
            title="You were kicked",
            description=(
                f"You've been kicked from **{guild.name}**.\n\n"
                f"**Reason:** {reason or '*(none provided)*'}"
            ),
            color=discord.Color.orange(),
        )

        try:
            await member.kick(reason=_audit_reason(invoker, reason))
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't kick — bot lacks permission or role hierarchy "
                "(bot's role must be above the target's).", ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Discord error: {e}", ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Kicked {member.mention}." + ("" if dmed else " *(couldn't DM)*"),
            ephemeral=True,
        )
        await audit.post_mod_event(
            guild,
            title="/kick",
            color=discord.Color.orange(),
            fields=_action_fields(
                target=member, invoker=invoker, reason=reason, dmed=dmed,
            ),
        )

    @app_commands.command(
        name="ban",
        description="Ban a member from the server.",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(
        member="The member to ban",
        reason="Why? (shown in audit log + DM to the member)",
        delete_message_days="How many days of their messages to also delete (0–7).",
    )
    async def ban(
        self, interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        guild = interaction.guild
        invoker = interaction.user
        if guild is None or not isinstance(invoker, discord.Member):
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        if not _hierarchy_ok(invoker, member, interaction):
            await interaction.response.send_message(
                _hierarchy_msg(member), ephemeral=True, delete_after=10,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        dmed = await audit.notify_user_dm(
            member,
            title="You were banned",
            description=(
                f"You've been banned from **{guild.name}**.\n\n"
                f"**Reason:** {reason or '*(none provided)*'}"
            ),
            color=discord.Color.dark_red(),
        )

        try:
            await member.ban(
                reason=_audit_reason(invoker, reason),
                delete_message_days=delete_message_days,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't ban — bot lacks permission or role hierarchy.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Discord error: {e}", ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Banned {member.mention}." + ("" if dmed else " *(couldn't DM)*"),
            ephemeral=True,
        )
        extra = []
        if delete_message_days:
            extra.append(("Msg history wiped", f"{delete_message_days} day(s)", True))
        await audit.post_mod_event(
            guild,
            title="/ban",
            color=discord.Color.dark_red(),
            fields=_action_fields(
                target=member, invoker=invoker, reason=reason, dmed=dmed,
            ) + extra,
        )

    @app_commands.command(
        name="timeout",
        description="Time a member out. Duration like '15m', '1h', '2d'.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        member="The member to time out",
        duration="How long: 15m, 1h, 2d, 1w (max 28 days).",
        reason="Why? (shown in audit log + DM to the member)",
    )
    async def timeout(
        self, interaction: discord.Interaction,
        member: discord.Member, duration: str, reason: str | None = None,
    ) -> None:
        guild = interaction.guild
        invoker = interaction.user
        if guild is None or not isinstance(invoker, discord.Member):
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        try:
            td = parse_duration(duration)
        except ValueError as e:
            await interaction.response.send_message(
                f"Bad duration: {e}", ephemeral=True, delete_after=10,
            )
            return
        if td > TIMEOUT_MAX:
            await interaction.response.send_message(
                "Discord caps timeouts at 28 days.",
                ephemeral=True, delete_after=10,
            )
            return
        if not _hierarchy_ok(invoker, member, interaction):
            await interaction.response.send_message(
                _hierarchy_msg(member), ephemeral=True, delete_after=10,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await member.timeout(td, reason=_audit_reason(invoker, reason))
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't time out — bot lacks permission or role hierarchy.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Discord error: {e}", ephemeral=True,
            )
            return

        dmed = await audit.notify_user_dm(
            member,
            title="You were timed out",
            description=(
                f"You've been timed out in **{guild.name}** for "
                f"**{_format_remaining(td)}**.\n\n"
                f"**Reason:** {reason or '*(none provided)*'}"
            ),
            color=discord.Color.orange(),
        )

        await interaction.followup.send(
            f"Timed out {member.mention} for **{_format_remaining(td)}**."
            + ("" if dmed else " *(couldn't DM)*"),
            ephemeral=True,
        )
        await audit.post_mod_event(
            guild,
            title="/timeout",
            color=discord.Color.orange(),
            fields=_action_fields(
                target=member, invoker=invoker, reason=reason, dmed=dmed,
            ) + [("Duration", _format_remaining(td), True)],
        )

    @app_commands.command(
        name="warn",
        description="Warn a member; auto-escalates after 3 (timeout) and 5 (kick).",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        member="The member to warn",
        reason="What did they do? (stored in history)",
    )
    async def warn(
        self, interaction: discord.Interaction,
        member: discord.Member, reason: str,
    ) -> None:
        guild = interaction.guild
        invoker = interaction.user
        if guild is None or not isinstance(invoker, discord.Member):
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        if member.id == invoker.id:
            await interaction.response.send_message(
                "You can't warn yourself.", ephemeral=True, delete_after=8,
            )
            return
        if not _hierarchy_ok(invoker, member, interaction):
            await interaction.response.send_message(
                _hierarchy_msg(member), ephemeral=True, delete_after=10,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        total = await db.add_warning(
            discord_id=member.id, issued_by=invoker.id,
            reason=reason, now_iso=_now_iso(),
        )

        # Escalation. We try auto-actions but tolerate failure (hierarchy
        # might block the bot from kicking despite the mod having perms).
        # Whatever lands gets stamped into the audit fields.
        escalation: str | None = None
        if total >= WARN_ESCALATE_KICK_AT:
            try:
                await member.kick(
                    reason=f"Auto-escalation: {total} warnings. Last: {reason}",
                )
                escalation = f"Auto-kicked at {total} warnings"
            except (discord.Forbidden, discord.HTTPException) as e:
                escalation = f"Auto-kick failed ({e})"
        elif total >= WARN_ESCALATE_TIMEOUT_AT:
            try:
                await member.timeout(
                    WARN_ESCALATE_TIMEOUT_DURATION,
                    reason=f"Auto-escalation: {total} warnings. Last: {reason}",
                )
                escalation = (
                    f"Auto-timeout for "
                    f"{_format_remaining(WARN_ESCALATE_TIMEOUT_DURATION)} "
                    f"at {total} warnings"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                escalation = f"Auto-timeout failed ({e})"

        dm_body = (
            f"You've received a warning in **{guild.name}**.\n\n"
            f"**Reason:** {reason}\n"
            f"**Total warnings:** {total}\n"
        )
        if escalation:
            dm_body += f"\n*{escalation}*"
        dmed = await audit.notify_user_dm(
            member,
            title="Warning issued",
            description=dm_body,
            color=discord.Color.gold(),
        )

        ack = (
            f"Warned {member.mention} (now **{total}** total)."
            + ("" if dmed else " *(couldn't DM)*")
        )
        if escalation:
            ack += f"\n*{escalation}.*"
        await interaction.followup.send(ack, ephemeral=True)

        fields = _action_fields(
            target=member, invoker=invoker, reason=reason, dmed=dmed,
        ) + [("Total warnings", str(total), True)]
        if escalation:
            fields.append(("Escalation", escalation, False))
        await audit.post_mod_event(
            guild, title="/warn",
            color=discord.Color.gold(),
            fields=fields,
        )

    @app_commands.command(
        name="warnings",
        description="Show a member's warning history.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(member="The member to look up")
    async def warnings_cmd(
        self, interaction: discord.Interaction, member: discord.Member,
    ) -> None:
        rows = await db.list_warnings(member.id)
        if not rows:
            await interaction.response.send_message(
                f"{member.mention} has no warnings on file.",
                ephemeral=True,
            )
            return
        lines = []
        for r in rows[:25]:  # Discord embed field cap
            issuer = f"<@{r['issued_by']}>"
            lines.append(
                f"• `{r['issued_at'][:19]}` — by {issuer}: {r['reason']}"
            )
        embed = discord.Embed(
            title=f"Warnings for {member} ({len(rows)} total)",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        if len(rows) > 25:
            embed.set_footer(text=f"Showing 25 most recent of {len(rows)}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="purge",
        description="Delete the last N messages in this channel (1–100).",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(count="How many messages to delete (1–100).")
    async def purge(
        self, interaction: discord.Interaction,
        count: app_commands.Range[int, 1, PURGE_MAX],
    ) -> None:
        guild = interaction.guild
        invoker = interaction.user
        channel = interaction.channel
        if guild is None or not isinstance(invoker, discord.Member):
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "This channel doesn't support purge.",
                ephemeral=True, delete_after=8,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            deleted = await channel.purge(limit=count)
        except discord.Forbidden:
            await interaction.followup.send(
                "Bot doesn't have Manage Messages here.", ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Discord error: {e}", ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Deleted **{len(deleted)}** message(s).", ephemeral=True,
        )
        await audit.post_mod_event(
            guild, title="/purge",
            color=discord.Color.dark_grey(),
            fields=[
                ("Acted by", f"{invoker.mention} (`{invoker.id}`)", True),
                ("Channel", channel.mention, True),
                ("Deleted", str(len(deleted)), True),
            ],
        )


# --------------------------------------------------------------------------- #
# Helpers (module-level so the new commands above and the existing /shutup     #
# share them, and so unit tests can call them without a Cog instance.)         #
# --------------------------------------------------------------------------- #

def _audit_reason(invoker: discord.Member, reason: str | None) -> str:
    """Format the `reason` string we pass to Discord's audit log API.
    Always includes the invoker so the server's native audit log shows
    who acted, even before a mod opens #mod-log."""
    base = f"By {invoker} ({invoker.id})"
    return f"{base}: {reason}" if reason else base


def _hierarchy_ok(
    invoker: discord.Member, target: discord.Member,
    interaction: discord.Interaction,
) -> bool:
    """Standard "you can't act on someone with an equal-or-higher top
    role" guard. Admins bypass. Self-target also bypasses (the caller
    handles self-action separately if disallowed)."""
    if target.id == invoker.id:
        return True
    if invoker.guild_permissions.administrator:
        return True
    return invoker.top_role > target.top_role


def _hierarchy_msg(target: discord.Member) -> str:
    return (
        f"You can't act on {target.mention} — they have an equal or "
        "higher role than you."
    )


def _action_fields(
    *, target: discord.Member, invoker: discord.Member,
    reason: str | None, dmed: bool,
) -> list[tuple[str, str, bool]]:
    return [
        ("Target", f"{target.mention} (`{target.id}`)", True),
        ("Acted by", f"{invoker.mention} (`{invoker.id}`)", True),
        ("Reason", reason or "*(none provided)*", False),
        ("DM delivered", "✅" if dmed else "❌", True),
    ]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
