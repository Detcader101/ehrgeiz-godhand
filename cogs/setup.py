"""
/setup-server — one-command build of the standard Ehrgeiz Godhand server layout.

Idempotent: existing channels/roles with the same name are reused, not
duplicated. Safe to re-run after schema tweaks.

The SERVER_PLAN and ROLE_PLAN lists are the declarative source of truth;
edit those to change the structure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

import db
from cogs.onboarding import (
    PANEL_KIND_PLAYER_HUB,
    PlayerHubView,
    _player_hub_embed,
)

log = logging.getLogger(__name__)


@dataclass
class ChannelSpec:
    name: str
    kind: str  # "text" or "voice"
    topic: str | None = None


@dataclass
class CategorySpec:
    name: str
    channels: list[ChannelSpec]
    staff_only: bool = False


SERVER_PLAN: list[CategorySpec] = [
    CategorySpec("📋 Info", [
        ChannelSpec("rules", "text", "Server rules. Breaking them gets you warned, timed out, or banned."),
        ChannelSpec("announcements", "text", "Server-wide announcements. Staff-only posting."),
        ChannelSpec("player-hub", "text", "Your account, ranks, and profile. Click the buttons."),
    ]),
    CategorySpec("💬 General", [
        ChannelSpec("general", "text", "Main hangout chat."),
        ChannelSpec("clips-and-highlights", "text",
                    "Drop your clips. Use threads for per-character discussion."),
        ChannelSpec("off-topic", "text", "Non-Tekken stuff."),
    ]),
    CategorySpec("🥊 Tekken", [
        ChannelSpec("tech-talk", "text", "Frame data, strategy, combo routes, meta."),
        ChannelSpec("fundamentals", "text",
                    "Newbies welcome. Ask the basics here without judgement."),
        ChannelSpec("combos", "text", "Labbing, combo routes, optimisation."),
        ChannelSpec("matchup-help", "text", "Ask about specific matchups."),
    ]),
    CategorySpec("🔎 Matchmaking", [
        ChannelSpec("matchmaking-na", "text", "Looking for games — North America."),
        ChannelSpec("matchmaking-eu", "text", "Looking for games — Europe."),
        ChannelSpec("matchmaking-asia", "text", "Looking for games — Asia."),
        ChannelSpec("matchmaking-oce", "text", "Looking for games — Oceania."),
    ]),
    CategorySpec("🏆 Competitive", [
        ChannelSpec("tournaments", "text",
                    "Tournament signups and chat. Organizers post here."),
        ChannelSpec("tournament-history", "text",
                    "Archived brackets and results. Posted by the bot."),
    ]),
    CategorySpec("🔊 Voice", [
        ChannelSpec("General VC", "voice"),
    ]),
    CategorySpec("🛠️ Staff", [
        ChannelSpec("mod-log", "text", "Every mod action the bot performs is logged here."),
        ChannelSpec("verification-log", "text",
                    "Audit trail for player verification: links, unlinks, "
                    "rank changes, admin overrides. Spot suspicious patterns here."),
        ChannelSpec("staff-chat", "text", "Private admin + moderator discussion."),
    ], staff_only=True),
]


@dataclass
class RoleSpec:
    name: str
    color: discord.Color
    permissions: discord.Permissions
    hoist: bool  # show separately in the member list
    mentionable: bool


ROLE_PLAN: list[RoleSpec] = [
    RoleSpec("Admin", discord.Color.red(),
             discord.Permissions(administrator=True),
             hoist=True, mentionable=False),
    RoleSpec("Moderator", discord.Color.orange(),
             discord.Permissions(
                 kick_members=True, ban_members=True, moderate_members=True,
                 manage_messages=True, view_audit_log=True,
                 manage_nicknames=True,
             ),
             hoist=True, mentionable=True),
    RoleSpec("Organizer", discord.Color.blue(),
             discord.Permissions.none(),  # marker role; power enforced in cog
             hoist=True, mentionable=True),
    RoleSpec("Verified", discord.Color.green(),
             discord.Permissions.none(),
             hoist=False, mentionable=False),
]


# --------------------------------------------------------------------------- #
# Builder                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class SetupReport:
    categories_created: list[str] = field(default_factory=list)
    categories_existing: list[str] = field(default_factory=list)
    channels_created: list[str] = field(default_factory=list)
    channels_existing: list[str] = field(default_factory=list)
    roles_created: list[str] = field(default_factory=list)
    roles_existing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    panel_posted_in: str | None = None  # channel name if Player Hub was auto-posted
    panel_skip_reason: str | None = None  # human-readable reason if skipped

    def to_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Server setup complete", color=discord.Color.green())

        def section(created: list[str], existing: list[str]) -> str:
            parts = []
            if created:
                parts.append(f"**Created ({len(created)}):** " + ", ".join(created))
            if existing:
                parts.append(f"**Already existed ({len(existing)}):** " + ", ".join(existing))
            return "\n".join(parts) if parts else "—"

        embed.add_field(name="Categories",
                        value=section(self.categories_created, self.categories_existing),
                        inline=False)
        embed.add_field(name="Channels",
                        value=section(self.channels_created, self.channels_existing),
                        inline=False)
        embed.add_field(name="Roles",
                        value=section(self.roles_created, self.roles_existing),
                        inline=False)

        # Next-steps panel: what the admin still has to do manually.
        next_steps = [
            "**1.** Server Settings → Roles: drag the bot's role **above** the rank "
            "roles (and Admin/Moderator if you want the bot to be able to manage them).",
        ]
        if self.panel_posted_in:
            next_steps.append(
                f"**2.** Player Hub panel is live in **#{self.panel_posted_in}**. "
                "Nothing else needed there."
            )
        else:
            reason = f" ({self.panel_skip_reason})" if self.panel_skip_reason else ""
            next_steps.append(
                f"**2.** Player Hub panel was **not** auto-posted{reason}. "
                "Go to your preferred channel and run `/post-player-panel`."
            )
        next_steps.append("**3.** Write your rules in **#rules** and pin them.")
        embed.add_field(name="📝 Next steps", value="\n".join(next_steps), inline=False)

        if self.errors:
            embed.add_field(name="⚠ Errors",
                            value="\n".join(self.errors[:5]), inline=False)
            embed.color = discord.Color.orange()
        return embed


async def _post_player_hub_if_channel_exists(
    bot: commands.Bot, guild: discord.Guild, report: SetupReport,
) -> None:
    """If a #player-hub text channel exists, post the Player Hub panel there
    (deleting any prior bot-tracked panel first) and record outcome on report."""
    channel = discord.utils.get(guild.text_channels, name="player-hub")
    if channel is None:
        report.panel_skip_reason = "no #player-hub channel found"
        return
    # Clean up the previous panel if we've ever posted one in this guild.
    existing = await db.get_panel(guild.id, PANEL_KIND_PLAYER_HUB)
    if existing is not None:
        old_channel = guild.get_channel(existing["channel_id"])
        if old_channel is not None:
            try:
                old_msg = await old_channel.fetch_message(existing["message_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    try:
        msg = await channel.send(
            embed=_player_hub_embed(), view=PlayerHubView(bot),
        )
    except discord.Forbidden:
        report.panel_skip_reason = "bot can't post in #player-hub (permissions)"
        return
    await db.set_panel(guild.id, PANEL_KIND_PLAYER_HUB, channel.id, msg.id)
    report.panel_posted_in = channel.name


async def _build_server(guild: discord.Guild) -> SetupReport:
    report = SetupReport()

    # --- Roles first, so we can reference them for category perms --- #
    role_by_name: dict[str, discord.Role] = {r.name: r for r in guild.roles}
    for spec in ROLE_PLAN:
        existing = role_by_name.get(spec.name)
        if existing is not None:
            report.roles_existing.append(spec.name)
            continue
        try:
            role = await guild.create_role(
                name=spec.name, color=spec.color, permissions=spec.permissions,
                hoist=spec.hoist, mentionable=spec.mentionable,
                reason="Ehrgeiz Godhand /setup-server",
            )
            role_by_name[spec.name] = role
            report.roles_created.append(spec.name)
        except discord.HTTPException as e:
            report.errors.append(f"Role '{spec.name}': {e}")

    admin_role = role_by_name.get("Admin")
    mod_role = role_by_name.get("Moderator")

    # --- Categories + channels --- #
    for cat_spec in SERVER_PLAN:
        category = discord.utils.get(guild.categories, name=cat_spec.name)
        if category is None:
            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
            if cat_spec.staff_only:
                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
                if admin_role:
                    overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True)
                if mod_role:
                    overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True)
                # Bot needs to see staff channels for mod-log writes
                overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True)
            try:
                category = await guild.create_category(
                    cat_spec.name, overwrites=overwrites,
                    reason="Ehrgeiz Godhand /setup-server",
                )
                report.categories_created.append(cat_spec.name)
            except discord.HTTPException as e:
                report.errors.append(f"Category '{cat_spec.name}': {e}")
                continue
        else:
            report.categories_existing.append(cat_spec.name)

        for ch_spec in cat_spec.channels:
            existing = discord.utils.get(
                category.channels if category else guild.channels,
                name=ch_spec.name,
            ) or discord.utils.get(guild.channels, name=ch_spec.name)
            if existing is not None:
                report.channels_existing.append(ch_spec.name)
                continue
            try:
                if ch_spec.kind == "voice":
                    await guild.create_voice_channel(
                        ch_spec.name, category=category,
                        reason="Ehrgeiz Godhand /setup-server",
                    )
                else:
                    await guild.create_text_channel(
                        ch_spec.name, category=category, topic=ch_spec.topic,
                        reason="Ehrgeiz Godhand /setup-server",
                    )
                report.channels_created.append(ch_spec.name)
            except discord.HTTPException as e:
                report.errors.append(f"Channel '{ch_spec.name}': {e}")

    return report


# --------------------------------------------------------------------------- #
# Slash command + confirm view                                                 #
# --------------------------------------------------------------------------- #

class _ConfirmSetupView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This isn't for you.", ephemeral=True, delete_after=8,
            )
            return False
        return True

    @discord.ui.button(label="Build it", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(
            content="Building server structure… (this can take up to a minute)",
            embed=None, view=None,
        )
        report = await _build_server(interaction.guild)
        await _post_player_hub_if_channel_exists(
            interaction.client, interaction.guild, report,
        )
        await interaction.edit_original_response(content=None, embed=report.to_embed())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled. No changes made.", embed=None, view=None,
        )


def _preview_embed() -> discord.Embed:
    lines: list[str] = []
    for cat in SERVER_PLAN:
        lock = " 🔒" if cat.staff_only else ""
        lines.append(f"**{cat.name}**{lock}")
        for ch in cat.channels:
            prefix = "🔊" if ch.kind == "voice" else "#"
            lines.append(f"  {prefix} {ch.name}")
        lines.append("")

    embed = discord.Embed(
        title="Confirm server setup",
        description="Going to create (or skip-if-exists):",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Channels", value="\n".join(lines)[:1024], inline=False)
    role_list = ", ".join(r.name for r in ROLE_PLAN)
    embed.add_field(name="Roles", value=role_list, inline=False)
    embed.set_footer(text="Safe to run again later — existing items are skipped.")
    return embed


class Setup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="setup-server",
        description="Admin: create the standard Ehrgeiz Godhand server structure.",
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_server(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        await interaction.response.send_message(
            embed=_preview_embed(),
            view=_ConfirmSetupView(interaction.user.id),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Setup(bot))
