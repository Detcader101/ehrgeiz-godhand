"""
/setup-server — one-command build of the standard Ehrgeiz Godhand server layout.

Idempotent: existing channels/roles with the same name are reused, not
duplicated. Safe to re-run after schema tweaks.

The SERVER_PLAN and ROLE_PLAN lists are the declarative source of truth;
edit those to change the structure.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import db
import media
import tournament_render
import wavu
from cogs.onboarding import (
    PANEL_KIND_PLAYER_HUB,
    PlayerHubView,
    _player_hub_embed,
)
from cogs.tournament import TournamentsPanelView
from cogs.matchmaking import LFGPanelView

log = logging.getLogger(__name__)


@dataclass
class ChannelSpec:
    name: str
    kind: str  # "text" or "voice"
    topic: str | None = None
    # Roles to grant view+send to *in addition* to the category default. Used
    # for staff-only categories where one specific channel needs broader read
    # access (e.g. #verification-log inside Staff, but Organizers must also
    # see + click the Confirm/Reject buttons there).
    extra_access_roles: list[str] = field(default_factory=list)


@dataclass
class CategorySpec:
    name: str
    channels: list[ChannelSpec]
    staff_only: bool = False


SERVER_PLAN: list[CategorySpec] = [
    CategorySpec("📋 Info", [
        ChannelSpec("rules", "text",
                    "📜 Server rules. Breaking them gets you warned, timed out, or banned."),
        ChannelSpec("announcements", "text",
                    "📣 Server-wide announcements. Staff-only posting."),
        ChannelSpec("player-hub", "text",
                    "🎴 Your account, ranks, and profile. Click the buttons."),
    ]),
    CategorySpec("💬 General", [
        ChannelSpec("general", "text",
                    "💬 Main hangout chat."),
        ChannelSpec("clips-and-highlights", "text",
                    "🎬 Drop your clips. Use threads for per-character discussion."),
        ChannelSpec("off-topic", "text",
                    "🌀 Non-Tekken stuff."),
    ]),
    CategorySpec("🥊 Tekken", [
        ChannelSpec("tech-talk", "text",
                    "🧠 Frame data, strategy, combo routes, meta."),
        ChannelSpec("fundamentals", "text",
                    "📚 Newbies welcome. Ask the basics here without judgement."),
        ChannelSpec("combos", "text",
                    "🎯 Labbing, combo routes, optimisation."),
        ChannelSpec("matchup-help", "text",
                    "🆚 Ask about specific matchups."),
    ]),
    CategorySpec("🔎 Matchmaking", [
        ChannelSpec("matchmaking-na", "text",
                    "🇺🇸 Looking for games — North America."),
        ChannelSpec("matchmaking-eu", "text",
                    "🇪🇺 Looking for games — Europe."),
        ChannelSpec("matchmaking-asia", "text",
                    "🌏 Looking for games — Asia."),
        ChannelSpec("matchmaking-oce", "text",
                    "🦘 Looking for games — Oceania."),
    ]),
    CategorySpec("🏆 Competitive", [
        ChannelSpec("tournaments", "text",
                    "🏆 Tournament signups and chat. Organizers post here."),
        ChannelSpec("tournament-history", "text",
                    "📜 Archived brackets and results. Posted by the bot."),
    ]),
    CategorySpec("🔊 Voice", [
        ChannelSpec("General VC", "voice"),
    ]),
    CategorySpec("🛠️ Staff", [
        ChannelSpec("mod-log", "text",
                    "📋 Every mod action the bot performs is logged here."),
        ChannelSpec("verification-log", "text",
                    "🔍 Audit trail for player verification: links, unlinks, "
                    "rank changes, admin overrides, and high-rank pending "
                    "claims (Confirm/Reject buttons live here).",
                    extra_access_roles=["Organizer"]),
        ChannelSpec("staff-chat", "text",
                    "🤐 Private admin + moderator discussion."),
    ], staff_only=True),
]


@dataclass
class RoleSpec:
    name: str
    color: discord.Color
    permissions: discord.Permissions
    hoist: bool  # show separately in the member list
    mentionable: bool


# --------------------------------------------------------------------------- #
# Channel banners                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class BannerSpec:
    """Declarative config for the pinned Ehrgeiz banner at the top of
    each user-facing text channel. `kind` is the db.panels key so the
    bot can find the existing message on re-setup instead of posting a
    duplicate. `view_factory`, when set, attaches a persistent
    discord.ui.View to the banner (interactive buttons)."""
    channel_name: str
    kind: str
    kicker: str
    title: str
    subtitle: str
    body: str
    view_factory: Callable[[], discord.ui.View] | None = None


BANNER_PLAN: list[BannerSpec] = [
    # ---- Info ---- #
    BannerSpec(
        channel_name="rules", kind="banner_rules",
        kicker="Welcome", title="House Rules",
        subtitle="Read before you swing",
        body=(
            "**Basics:** be kind, don't harass, don't cheat.\n"
            "**Tekken talk:** hype is good, tilt is fine, slurs are not.\n"
            "**Onboarding:** verify your Tekken ID in **#player-hub** to "
            "unlock the rest of the server.\n\n"
            "Breaking the rules gets you warned, timed out, or banned. "
            "Moderators' call is final; use DMs for appeals."
        ),
    ),
    BannerSpec(
        channel_name="announcements", kind="banner_announcements",
        kicker="Server News", title="Announcements",
        subtitle="Stay in the loop",
        body=(
            "Server-wide news, event dates, bot updates, tournament "
            "kickoffs. Staff-only posting — watch for the pings."
        ),
    ),
    # #player-hub deliberately skipped: it already carries the Player Hub panel.
    # ---- General ---- #
    BannerSpec(
        channel_name="general", kind="banner_general",
        kicker="Hangout", title="General Chat",
        subtitle="Home of the Ehrgeiz crowd",
        body=(
            "Your main hangout. Tekken chat, meme chat, whatever chat. "
            "Keep it friendly."
        ),
    ),
    BannerSpec(
        channel_name="clips-and-highlights", kind="banner_clips",
        kicker="Replay Culture", title="Clips & Highlights",
        subtitle="Drop the tape",
        body=(
            "Post your best sets, sick combos, clutch comebacks. Thread "
            "the talk under each clip so the feed stays watchable."
        ),
    ),
    BannerSpec(
        channel_name="off-topic", kind="banner_offtopic",
        kicker="Anything Else", title="Off-Topic",
        subtitle="Non-Tekken is fine here",
        body="Everything that's not Tekken goes here. Same server rules apply.",
    ),
    # ---- Tekken ---- #
    BannerSpec(
        channel_name="tech-talk", kind="banner_techtalk",
        kicker="Theory", title="Tech Talk",
        subtitle="Frame data · meta · strategy",
        body=(
            "Advanced discussion — frame data, matchup theory, meta shifts, "
            "patch analysis. Bring receipts."
        ),
    ),
    BannerSpec(
        channel_name="fundamentals", kind="banner_fundamentals",
        kicker="Study Hall", title="Fundamentals",
        subtitle="Newbies welcome, always",
        body=(
            "Ask the basic questions here without judgement. Veterans: "
            "this is where you pay it forward."
        ),
    ),
    BannerSpec(
        channel_name="combos", kind="banner_combos",
        kicker="Lab Notes", title="Combos",
        subtitle="Routes · optimisation · tech",
        body=(
            "Character combo routes, damage optimisations, new tech. "
            "Include the character + notation + (where known) wall carry."
        ),
    ),
    BannerSpec(
        channel_name="matchup-help", kind="banner_matchup",
        kicker="Call for Help", title="Matchup Help",
        subtitle="Ask about specific matchups",
        body=(
            "Stuck against a character? Post the pair (you vs them), "
            "screenshot or describe the problem spot, and ask."
        ),
    ),
    # ---- Matchmaking ---- #
    BannerSpec(
        channel_name="matchmaking-na", kind="banner_mm_na",
        kicker="North America", title="Matchmaking · NA",
        subtitle="Looking for games",
        body=(
            "Click **I'm Looking for Games** below and the bot posts an "
            "LFG ping with your rank + main so others know who they're "
            "playing. Auto-clears after 30 minutes."
        ),
        view_factory=LFGPanelView,
    ),
    BannerSpec(
        channel_name="matchmaking-eu", kind="banner_mm_eu",
        kicker="Europe", title="Matchmaking · EU",
        subtitle="Looking for games",
        body=(
            "Click **I'm Looking for Games** below and the bot posts an "
            "LFG ping with your rank + main. Auto-clears after 30 minutes."
        ),
        view_factory=LFGPanelView,
    ),
    BannerSpec(
        channel_name="matchmaking-asia", kind="banner_mm_asia",
        kicker="Asia", title="Matchmaking · Asia",
        subtitle="Looking for games",
        body=(
            "Click **I'm Looking for Games** below and the bot posts an "
            "LFG ping with your rank + main. Auto-clears after 30 minutes."
        ),
        view_factory=LFGPanelView,
    ),
    BannerSpec(
        channel_name="matchmaking-oce", kind="banner_mm_oce",
        kicker="Oceania", title="Matchmaking · OCE",
        subtitle="Looking for games",
        body=(
            "Click **I'm Looking for Games** below and the bot posts an "
            "LFG ping with your rank + main. Auto-clears after 30 minutes."
        ),
        view_factory=LFGPanelView,
    ),
    # ---- Competitive ---- #
    BannerSpec(
        channel_name="tournaments", kind="banner_tournaments",
        kicker="Competitive", title="Tournaments",
        subtitle="Swiss brackets · rank-weighted seeding",
        body=(
            "Click **Active Tournaments** to see what's live. Organizers "
            "click **Create Tournament (FT3)** for quick-start, or run "
            "`/tournament-create` for full control over format and player cap."
        ),
        view_factory=TournamentsPanelView,
    ),
    BannerSpec(
        channel_name="tournament-history", kind="banner_tournament_history",
        kicker="Archive", title="Tournament History",
        subtitle="Past brackets · past champions",
        body=(
            "Closed tournaments are archived here by the bot — final "
            "bracket image + results. Scroll or search for past events."
        ),
    ),
]


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
    # Marker role: holders gain access to /shutup at a 1-per-hour rate
    # limit. No Discord-level perms — authority is checked in cogs/mod.py.
    RoleSpec("The Silencerz", discord.Color.from_rgb(180, 0, 200),
             discord.Permissions.none(),
             hoist=True, mentionable=False),
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
    banners_posted: int = 0

    def to_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="✅ Server setup complete",
            description="Your Ehrgeiz Godhand server is provisioned. "
                        "Summary below — anything that already existed was left alone.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=media.LOGO_URL)

        def section(created: list[str], existing: list[str]) -> str:
            parts = []
            if created:
                parts.append(f"🆕 **Created ({len(created)}):** " + ", ".join(created))
            if existing:
                parts.append(f"☑️ **Already existed ({len(existing)}):** " + ", ".join(existing))
            return "\n".join(parts) if parts else "—"

        embed.add_field(name="📂 Categories",
                        value=section(self.categories_created, self.categories_existing),
                        inline=False)
        embed.add_field(name="📺 Channels",
                        value=section(self.channels_created, self.channels_existing),
                        inline=False)
        embed.add_field(name="👥 Roles",
                        value=section(self.roles_created, self.roles_existing),
                        inline=False)

        # Next-steps panel: what the admin still has to do manually.
        next_steps = [
            "**1️⃣** Server Settings → Roles: drag the bot's role **above** the rank "
            "roles (and Admin/Moderator if you want the bot to manage them).",
        ]
        if self.panel_posted_in:
            next_steps.append(
                f"**2️⃣** 🎴 Player Hub panel is live in **#{self.panel_posted_in}**. "
                "Nothing else needed there."
            )
        else:
            reason = f" ({self.panel_skip_reason})" if self.panel_skip_reason else ""
            next_steps.append(
                f"**2️⃣** 🎴 Player Hub panel was **not** auto-posted{reason}. "
                "Go to your preferred channel and run `/post-player-panel`."
            )
        if self.banners_posted:
            next_steps.append(
                f"**3️⃣** 🖼️ Pinned **{self.banners_posted}** channel "
                "banners. Edit copy in `BANNER_PLAN` (cogs/setup.py) and "
                "re-run to refresh in place."
            )
        next_steps.append("**4️⃣** 📜 Read the #rules banner and personalise it if you want.")
        embed.add_field(name="📝 Next steps", value="\n".join(next_steps), inline=False)

        if self.errors:
            embed.add_field(name="⚠ Errors",
                            value="\n".join(self.errors[:5]), inline=False)
            embed.color = discord.Color.orange()
        embed.set_footer(text="Ehrgeiz Godhand • Idempotent — safe to re-run later")
        return embed


async def _delete_pin_notification(channel: discord.TextChannel) -> None:
    """Nuke the transient 'X pinned a message' system message after a pin,
    mirroring the pattern used by the tournament signup panel so /setup
    doesn't litter channels with 14 pin notifications."""
    try:
        async for m in channel.history(limit=5):
            if m.type == discord.MessageType.pins_add:
                await m.delete()
                return
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _post_or_refresh_banner(
    guild: discord.Guild, spec: BannerSpec, report: SetupReport,
) -> None:
    """Post (or refresh in place) the pinned banner for a single channel.
    Idempotent: on re-run we fetch the existing message from db.panels,
    edit it with a freshly-rendered banner, and skip the pin step since
    it's already pinned."""
    channel = discord.utils.get(guild.text_channels, name=spec.channel_name)
    if channel is None:
        # Channel isn't in the server — user either renamed it or opted
        # out of this part of the layout. Silent skip; not an error.
        return

    try:
        buf = await tournament_render.render_banner(
            kicker=spec.kicker,
            title=spec.title,
            subtitle=spec.subtitle,
        )
    except Exception as e:
        report.errors.append(f"Banner render for #{spec.channel_name}: {e}")
        return

    embed = discord.Embed(
        description=spec.body,
        color=discord.Color.red(),
    )
    embed.set_image(url="attachment://banner.png")

    view = spec.view_factory() if spec.view_factory else None

    existing = await db.get_panel(guild.id, spec.kind)
    if existing is not None:
        try:
            ch = guild.get_channel(existing["channel_id"]) or channel
            msg = await ch.fetch_message(existing["message_id"])
            edit_kwargs: dict = {
                "embed": embed,
                "attachments": [discord.File(buf, filename="banner.png")],
            }
            if view is not None:
                edit_kwargs["view"] = view
            await msg.edit(**edit_kwargs)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Old message is gone or unreachable — fall through and
            # post a fresh one.
            pass

    try:
        send_kwargs: dict = {
            "embed": embed,
            "file": discord.File(buf, filename="banner.png"),
        }
        if view is not None:
            send_kwargs["view"] = view
        msg = await channel.send(**send_kwargs)
    except discord.Forbidden:
        report.errors.append(
            f"Banner for #{spec.channel_name}: no send permission"
        )
        return
    except discord.HTTPException as e:
        report.errors.append(f"Banner for #{spec.channel_name}: {e}")
        return

    try:
        await msg.pin()
        await _delete_pin_notification(channel)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("pin failed for banner %s: %s", spec.kind, e)

    await db.set_panel(guild.id, spec.kind, channel.id, msg.id)


async def _post_channel_banners(
    guild: discord.Guild, report: SetupReport,
) -> None:
    """Loop every banner spec, posting (or refreshing) its pinned panel."""
    posted = 0
    for spec in BANNER_PLAN:
        before = len(report.errors)
        await _post_or_refresh_banner(guild, spec, report)
        if len(report.errors) == before:
            posted += 1
    report.banners_posted = posted


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

            new_channel = None
            if existing is not None:
                report.channels_existing.append(ch_spec.name)
                # Still apply extra_access_roles in case the channel exists from
                # an older /setup-server run that didn't set them.
                if ch_spec.extra_access_roles and isinstance(existing, discord.TextChannel):
                    new_channel = existing
            else:
                try:
                    if ch_spec.kind == "voice":
                        await guild.create_voice_channel(
                            ch_spec.name, category=category,
                            reason="Ehrgeiz Godhand /setup-server",
                        )
                    else:
                        new_channel = await guild.create_text_channel(
                            ch_spec.name, category=category, topic=ch_spec.topic,
                            reason="Ehrgeiz Godhand /setup-server",
                        )
                    report.channels_created.append(ch_spec.name)
                except discord.HTTPException as e:
                    report.errors.append(f"Channel '{ch_spec.name}': {e}")
                    continue

            if new_channel is not None and ch_spec.extra_access_roles:
                for role_name in ch_spec.extra_access_roles:
                    role = role_by_name.get(role_name)
                    if role is None:
                        continue
                    try:
                        await new_channel.set_permissions(
                            role, view_channel=True, send_messages=True,
                            reason="Ehrgeiz Godhand /setup-server (extra access)",
                        )
                    except (discord.Forbidden, discord.HTTPException) as e:
                        report.errors.append(
                            f"Channel '{ch_spec.name}' grant {role_name}: {e}"
                        )

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
        await _post_channel_banners(interaction.guild, report)
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
        title="🛠️ Confirm server setup",
        description=(
            "Going to create the standard **Ehrgeiz Godhand** server layout. "
            "Anything that already exists by name is **skipped** — re-running "
            "later is safe."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=media.LOGO_URL)
    embed.add_field(name="📂 Categories & channels",
                    value="\n".join(lines)[:1024], inline=False)
    role_list = ", ".join(f"`{r.name}`" for r in ROLE_PLAN)
    embed.add_field(name="👥 Roles", value=role_list, inline=False)
    embed.set_footer(text="Click Build it to provision, or Cancel.")
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

    @app_commands.command(
        name="purge-server",
        description="[Admin] DESTRUCTIVE: remove bot-created channels, roles, panels, and rank emojis.",
    )
    @app_commands.default_permissions(administrator=True)
    async def purge_server(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        preview = await _compute_purge_preview(guild)
        await interaction.response.send_message(
            embed=preview.to_embed(rebuild=False),
            view=_ConfirmPurgeView(interaction.user.id, rebuild=False),
            ephemeral=True,
        )

    @app_commands.command(
        name="reset-server",
        description="[Admin] DESTRUCTIVE: purge everything AND rebuild from scratch.",
    )
    @app_commands.default_permissions(administrator=True)
    async def reset_server(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        preview = await _compute_purge_preview(guild)
        await interaction.response.send_message(
            embed=preview.to_embed(rebuild=True),
            view=_ConfirmPurgeView(interaction.user.id, rebuild=True),
            ephemeral=True,
        )

    @app_commands.command(
        name="upload-rank-emojis",
        description="[Admin] Upload Tekken rank icons to this server as custom emojis.",
    )
    @app_commands.default_permissions(manage_emojis=True)
    async def upload_rank_emojis(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        # Requires the bot to have Manage Emojis — warn early instead of
        # crashing halfway through the upload loop.
        if not guild.me.guild_permissions.manage_emojis_and_stickers:
            await interaction.response.send_message(
                "⚠ I need the **Manage Emojis and Stickers** permission "
                "to upload rank icons. Grant it to my role, then re-run.",
                ephemeral=True, delete_after=20)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _upload_rank_emojis_for_guild(guild)
        await interaction.followup.send(embed=result.to_embed(), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Setup(bot))


# --------------------------------------------------------------------------- #
# Rank emoji upload — slice C                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class RankEmojiResult:
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    icon_fetch_errors: list[str] = field(default_factory=list)

    def to_embed(self) -> discord.Embed:
        total = len(self.created) + len(self.reused)
        color = (discord.Color.green() if not (self.failed or self.icon_fetch_errors)
                 else discord.Color.orange())
        embed = discord.Embed(
            title="🎌 Rank emoji upload",
            description=(
                f"Mapped **{total}** Tekken ranks to custom emojis in this server "
                f"({len(self.created)} newly created, {len(self.reused)} already existed)."
            ),
            color=color,
        )
        embed.set_thumbnail(url=media.LOGO_URL)
        if self.created:
            embed.add_field(
                name=f"🆕 Created ({len(self.created)})",
                value=", ".join(f"`{n}`" for n in self.created[:20])
                      + ("…" if len(self.created) > 20 else ""),
                inline=False,
            )
        if self.reused:
            embed.add_field(
                name=f"♻️ Reused ({len(self.reused)})",
                value=", ".join(f"`{n}`" for n in self.reused[:20])
                      + ("…" if len(self.reused) > 20 else ""),
                inline=False,
            )
        if self.failed:
            embed.add_field(
                name=f"⚠ Failed ({len(self.failed)})",
                value="\n".join(f"`{n}` — {err}" for n, err in self.failed[:5]),
                inline=False,
            )
        if self.icon_fetch_errors:
            embed.add_field(
                name="🌐 Icon fetch errors",
                value=", ".join(self.icon_fetch_errors[:10]),
                inline=False,
            )
        embed.set_footer(
            text="Ephemeral mapping stored in guild_rank_emojis • safe to re-run"
        )
        return embed


def _emoji_name_for_rank(rank_name: str) -> str:
    """Turn a Tekken rank name into a valid Discord emoji name.

    Discord emoji names must be [A-Za-z0-9_] and 2-32 chars. We use the
    rank name directly (snake_cased) — admins picking emojis from the
    server picker can type `:garyu` or `:tekken_emperor` and get a hit
    without needing to remember a namespace prefix.
    """
    slug = rank_name.lower()
    slug = slug.replace("∞", "inf")
    slug = re.sub(r"[^a-z0-9_]+", "_", slug).strip("_")
    return slug[:32]


# The rank emojis we'd upload today, plus the `t8_*` names from the
# earlier prefixed era — both variants are recognised as Ehrgeiz
# artifacts so /purge-server cleans up legacy installations in place.
def _all_rank_emoji_names() -> set[str]:
    current = {_emoji_name_for_rank(r) for r in wavu.ALL_RANK_NAMES}
    legacy = {f"t8_{n}" for n in current}
    return current | legacy


async def _upload_rank_emojis_for_guild(
    guild: discord.Guild,
) -> RankEmojiResult:
    """Upload (or re-discover) a custom emoji for every Tekken rank tier.

    Idempotent — running it twice against the same guild reuses existing
    emojis with matching names. Makes one HTTP call to ewgf per
    never-before-seen rank icon (most installs hit cache after the
    first roster render).
    """
    result = RankEmojiResult()
    now = datetime.now(timezone.utc).isoformat()

    # Map existing guild emoji by name for fast lookup.
    existing_by_name = {e.name: e for e in guild.emojis}

    async with aiohttp.ClientSession() as session:
        for rank_name in wavu.ALL_RANK_NAMES:
            emoji_name = _emoji_name_for_rank(rank_name)

            # Case 1: emoji already exists in the guild — just record it.
            if emoji_name in existing_by_name:
                emoji = existing_by_name[emoji_name]
                await db.set_rank_emoji(
                    guild.id, rank_name, emoji.id, emoji_name, now,
                )
                result.reused.append(rank_name)
                continue

            # Case 2: fetch the icon bytes (cache, then network fallback).
            url = media.rank_icon_url(rank_name)
            if url is None:
                result.failed.append((rank_name, "no icon URL"))
                continue
            cache_path = tournament_render.RANK_CACHE_DIR / url.rsplit("/", 1)[-1]
            image_bytes = await _read_or_fetch_icon(cache_path, url, session)
            if image_bytes is None:
                result.icon_fetch_errors.append(rank_name)
                continue

            # Case 3: create the emoji. Discord accepts PNG/JPEG up to
            # 256 KiB — webp (our source) is also fine.
            try:
                emoji = await guild.create_custom_emoji(
                    name=emoji_name,
                    image=image_bytes,
                    reason="Ehrgeiz Godhand /upload-rank-emojis",
                )
                await db.set_rank_emoji(
                    guild.id, rank_name, emoji.id, emoji_name, now,
                )
                result.created.append(rank_name)
                # Small stagger so we don't spike Discord's emoji
                # create-rate-limit budget on a cold guild.
                await asyncio.sleep(0.4)
            except discord.Forbidden:
                result.failed.append(
                    (rank_name, "forbidden (check Manage Emojis perm)")
                )
            except discord.HTTPException as e:
                result.failed.append((rank_name, str(e)))

    return result


# --------------------------------------------------------------------------- #
# Purge + Reset                                                                #
# --------------------------------------------------------------------------- #

PLANNED_CATEGORY_NAMES = {c.name for c in SERVER_PLAN}
PLANNED_CHANNEL_NAMES = {ch.name for cat in SERVER_PLAN for ch in cat.channels}
PLANNED_ROLE_NAMES = {r.name for r in ROLE_PLAN}


@dataclass
class PurgePreview:
    """Snapshot of what /purge-server or /reset-server would remove.
    Computed up-front so the admin sees real counts before they click
    through the destructive confirm."""
    categories: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    emojis: list[str] = field(default_factory=list)
    panels: int = 0
    tournaments: int = 0

    def to_embed(self, *, rebuild: bool) -> discord.Embed:
        verb = "RESET" if rebuild else "PURGE"
        suffix = (" and then rebuild the standard Ehrgeiz layout from scratch."
                  if rebuild else ".")
        embed = discord.Embed(
            title=f"⚠ {verb} SERVER — confirm",
            description=(
                f"This will **permanently delete** the Ehrgeiz-managed "
                f"artifacts below from **{self.summary_total()} objects**"
                f"{suffix}\n\n"
                "**Kept** (not touched): player verifications (Tekken ID "
                "links), warnings, silencer cooldowns, pending verifications."
            ),
            color=discord.Color.red(),
        )
        embed.set_thumbnail(url=media.LOGO_URL)
        embed.add_field(
            name=f"📂 Categories ({len(self.categories)})",
            value=", ".join(self.categories) or "—",
            inline=False,
        )
        embed.add_field(
            name=f"📺 Channels ({len(self.channels)})",
            value=(", ".join(self.channels) or "—") + "\n*(includes all message history)*",
            inline=False,
        )
        embed.add_field(
            name=f"👥 Roles ({len(self.roles)})",
            value=(", ".join(self.roles) or "—") + "\n*(removes from every member)*",
            inline=False,
        )
        embed.add_field(
            name=f"🎌 Custom rank emojis ({len(self.emojis)})",
            value=(", ".join(self.emojis[:15])
                   + ("…" if len(self.emojis) > 15 else ""))
                  or "—",
            inline=False,
        )
        embed.add_field(
            name="🗂 DB records",
            value=(f"Panel records: **{self.panels}**\n"
                   f"Tournament rows (with participants + matches): "
                   f"**{self.tournaments}**"),
            inline=False,
        )
        embed.set_footer(
            text="This cannot be undone. Click the red button to proceed, "
                 "or Cancel to back out."
        )
        return embed

    def summary_total(self) -> int:
        return (len(self.categories) + len(self.channels) + len(self.roles)
                + len(self.emojis) + self.panels + self.tournaments)


async def _compute_purge_preview(guild: discord.Guild) -> PurgePreview:
    p = PurgePreview()
    for cat in guild.categories:
        if cat.name in PLANNED_CATEGORY_NAMES:
            p.categories.append(cat.name)
    for ch in guild.channels:
        if ch.name in PLANNED_CHANNEL_NAMES:
            p.channels.append(ch.name)
    for role in guild.roles:
        if role.name in PLANNED_ROLE_NAMES:
            p.roles.append(role.name)
    rank_emoji_names = _all_rank_emoji_names()
    for emo in guild.emojis:
        if emo.name in rank_emoji_names:
            p.emojis.append(emo.name)
    panel_rows = await _count_panels(guild.id)
    p.panels = panel_rows
    tournament_rows = await _count_tournaments(guild.id)
    p.tournaments = tournament_rows
    return p


async def _count_panels(guild_id: int) -> int:
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM panels WHERE guild_id = ?", (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def _count_tournaments(guild_id: int) -> int:
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM tournaments WHERE guild_id = ?", (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


@dataclass
class PurgeReport:
    categories_deleted: int = 0
    channels_deleted: int = 0
    roles_deleted: int = 0
    emojis_deleted: int = 0
    panels_wiped: int = 0
    tournaments_wiped: int = 0
    errors: list[str] = field(default_factory=list)


async def _execute_purge(guild: discord.Guild) -> PurgeReport:
    """Actually delete the bot-managed artifacts. Order matters: remove
    banner messages before the channels they live in (the channel delete
    would otherwise throw 404s); wipe DB rows after the Discord objects
    are gone so a partial failure doesn't leave orphan references."""
    report = PurgeReport()

    # Unpin + delete tracked banner messages up front. Channels get
    # nuked next anyway, but this keeps the db.panels wipe clean.
    panels_cleaned = await _unpin_and_remove_panels(guild, report)
    _ = panels_cleaned  # informational

    # Delete matching channels first (including voice), then categories.
    # Iterate guild.channels once and filter to avoid mutating live list.
    for ch in list(guild.channels):
        if isinstance(ch, discord.CategoryChannel):
            continue
        if ch.name in PLANNED_CHANNEL_NAMES:
            try:
                await ch.delete(reason="Ehrgeiz Godhand /purge-server")
                report.channels_deleted += 1
            except (discord.Forbidden, discord.HTTPException) as e:
                report.errors.append(f"channel '{ch.name}': {e}")

    for cat in list(guild.categories):
        if cat.name in PLANNED_CATEGORY_NAMES:
            try:
                await cat.delete(reason="Ehrgeiz Godhand /purge-server")
                report.categories_deleted += 1
            except (discord.Forbidden, discord.HTTPException) as e:
                report.errors.append(f"category '{cat.name}': {e}")

    # Roles — skip @everyone and the bot's own managed role.
    bot_role = guild.me.top_role if guild.me else None
    for role in list(guild.roles):
        if role.name not in PLANNED_ROLE_NAMES:
            continue
        if role.is_default() or (bot_role and role.id == bot_role.id):
            continue
        try:
            await role.delete(reason="Ehrgeiz Godhand /purge-server")
            report.roles_deleted += 1
        except (discord.Forbidden, discord.HTTPException) as e:
            report.errors.append(f"role '{role.name}': {e}")

    # Custom rank emojis — matches the exact name set of uploaded rank
    # icons (plus legacy t8_ names from the earlier prefix era).
    rank_emoji_names = _all_rank_emoji_names()
    for emo in list(guild.emojis):
        if emo.name not in rank_emoji_names:
            continue
        try:
            await emo.delete(reason="Ehrgeiz Godhand /purge-server")
            report.emojis_deleted += 1
        except (discord.Forbidden, discord.HTTPException) as e:
            report.errors.append(f"emoji '{emo.name}': {e}")

    # DB wipe happens last so errors in Discord deletions don't orphan
    # records (a failed channel delete would still leave a usable panel
    # row). Panels already unpinned above, now wipe the DB rows.
    report.panels_wiped = await db.purge_panels_for_guild(guild.id)
    report.tournaments_wiped = await db.purge_tournaments_for_guild(guild.id)
    await db.purge_rank_emojis_for_guild(guild.id)

    return report


async def _unpin_and_remove_panels(
    guild: discord.Guild, report: PurgeReport,
) -> int:
    """Best-effort unpin + delete of every tracked panel message in this
    guild BEFORE we nuke the channels. Failures are silent because the
    subsequent channel deletion will take care of the message anyway."""
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT channel_id, message_id FROM panels WHERE guild_id = ?",
            (guild.id,),
        ) as cur:
            rows = await cur.fetchall()
    for row in rows:
        channel = guild.get_channel(row["channel_id"])
        if channel is None:
            continue
        try:
            msg = await channel.fetch_message(row["message_id"])
            try:
                await msg.unpin()
            except (discord.Forbidden, discord.HTTPException):
                pass
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    return len(rows)


class _ConfirmPurgeView(discord.ui.View):
    """Confirmation UI for the destructive /purge-server and
    /reset-server commands. The same view handles both — if
    `rebuild=True`, we kick off /setup-server's build after the purge
    completes."""

    def __init__(self, invoker_id: int, *, rebuild: bool):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.rebuild = rebuild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This isn't for you.", ephemeral=True, delete_after=8,
            )
            return False
        return True

    @discord.ui.button(
        label="Wipe and REBUILD", style=discord.ButtonStyle.danger,
    )
    async def confirm(self, interaction: discord.Interaction, btn: discord.ui.Button):
        # Button label + copy swap depending on rebuild flag. We set the
        # label here rather than at __init__ so the dataclass init order
        # is simpler; either label makes the intent clear.
        label = "Wipe and REBUILD" if self.rebuild else "WIPE IT ALL"
        btn.label = label  # only affects the view that was sent; harmless

        await interaction.response.edit_message(
            content=f"{'Purging and rebuilding' if self.rebuild else 'Purging'}… "
                    "this may take a couple of minutes.",
            embed=None, view=None,
        )

        guild = interaction.guild
        report = await _execute_purge(guild)

        if self.rebuild:
            build = await _build_server(guild)
            # Re-run banner + player hub posts against the fresh server.
            await _post_player_hub_if_channel_exists(
                interaction.client, guild, build,
            )
            await _post_channel_banners(guild, build)
            await interaction.edit_original_response(
                content=None,
                embed=_combined_report_embed(report, build),
            )
        else:
            await interaction.edit_original_response(
                content=None, embed=_purge_only_report_embed(report),
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled. Nothing was deleted.",
            embed=None, view=None,
        )


def _purge_only_report_embed(r: PurgeReport) -> discord.Embed:
    embed = discord.Embed(
        title="🧹 Purge complete",
        description=(
            "All Ehrgeiz-managed artifacts have been removed. Run "
            "`/setup-server` to rebuild when ready, or `/reset-server` "
            "next time to do both in one shot."
        ),
        color=discord.Color.dark_red() if r.errors else discord.Color.green(),
    )
    embed.set_thumbnail(url=media.LOGO_URL)
    embed.add_field(
        name="Removed",
        value=(f"📂 Categories: **{r.categories_deleted}**\n"
               f"📺 Channels: **{r.channels_deleted}**\n"
               f"👥 Roles: **{r.roles_deleted}**\n"
               f"🎌 Rank emojis: **{r.emojis_deleted}**\n"
               f"🗂 Panel records: **{r.panels_wiped}**\n"
               f"🏆 Tournaments: **{r.tournaments_wiped}**"),
        inline=False,
    )
    if r.errors:
        embed.add_field(
            name=f"⚠ Errors ({len(r.errors)})",
            value="\n".join(r.errors[:5]),
            inline=False,
        )
    return embed


def _combined_report_embed(
    purge: PurgeReport, build: SetupReport,
) -> discord.Embed:
    """Merge the purge + rebuild outputs into one narrative embed so the
    admin sees the whole story in a single message."""
    color = (discord.Color.green() if not (purge.errors or build.errors)
             else discord.Color.orange())
    embed = discord.Embed(
        title="♻️ Reset complete",
        description=(
            "The server was purged and rebuilt from the standard Ehrgeiz "
            "layout. Banners are pinned; run `/upload-rank-emojis` to "
            "re-upload custom rank emojis."
        ),
        color=color,
    )
    embed.set_thumbnail(url=media.LOGO_URL)
    embed.add_field(
        name="🧹 Purged",
        value=(f"📂 Categories: **{purge.categories_deleted}**, "
               f"📺 Channels: **{purge.channels_deleted}**, "
               f"👥 Roles: **{purge.roles_deleted}**, "
               f"🎌 Emojis: **{purge.emojis_deleted}**, "
               f"🗂 Panels: **{purge.panels_wiped}**, "
               f"🏆 Tournaments: **{purge.tournaments_wiped}**"),
        inline=False,
    )
    embed.add_field(
        name="🏗 Rebuilt",
        value=(f"📂 Categories: **{len(build.categories_created)}**, "
               f"📺 Channels: **{len(build.channels_created)}**, "
               f"👥 Roles: **{len(build.roles_created)}**, "
               f"🖼 Banners: **{build.banners_posted}**"),
        inline=False,
    )
    combined_errors = purge.errors + build.errors
    if combined_errors:
        embed.add_field(
            name=f"⚠ Errors ({len(combined_errors)})",
            value="\n".join(combined_errors[:5]),
            inline=False,
        )
    embed.set_footer(
        text="Ehrgeiz Godhand • player verifications were preserved"
    )
    return embed


async def _read_or_fetch_icon(
    cache_path: Path, url: str, session: aiohttp.ClientSession,
) -> bytes | None:
    """Return the raw bytes of an icon — from the local cache if present,
    otherwise download + cache. None on fetch failure."""
    if cache_path.exists():
        try:
            return cache_path.read_bytes()
        except OSError as e:
            log.warning("local icon read failed %s: %s", cache_path, e)
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
            return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("icon fetch %s failed: %s", url, e)
        return None
