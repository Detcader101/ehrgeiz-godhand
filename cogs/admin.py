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
import os
import random
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import db
import tournament_render
import wavu
from cogs.fitcheck import DRIP_LORD_ROLE_NAME
from cogs.onboarding import (
    ORGANIZER_ROLE_NAME, VERIFIED_ROLE_NAME, _profile_card_payload,
)
from view_util import ErrorHandledView, handle_app_command_error

log = logging.getLogger(__name__)

# Discord embed field values are capped at 1024 chars; this is plenty
# for a comma-joined role list of normal length, but we trim defensively.
_FIELD_VALUE_MAX = 1000


def _trim(s: str) -> str:
    return s if len(s) <= _FIELD_VALUE_MAX else s[: _FIELD_VALUE_MAX - 1] + "…"


# --------------------------------------------------------------------------- #
# Admin test panel — buttons for every visual / scheduled feature             #
# --------------------------------------------------------------------------- #


def _admin_only(interaction: discord.Interaction) -> bool:
    """Defensive double-check on the panel buttons. The slash command
    is gated by `default_permissions(manage_guild=True)` but Discord's
    default-permission gate is server-config-overridable, so we still
    enforce it server-side on every click. Belt-and-braces."""
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    return (
        member.guild_permissions.manage_guild
        or member.guild_permissions.administrator
    )


class AdminTestPanelView(ErrorHandledView):
    """Persistent ephemeral panel of test buttons for staff. Each
    button is split into its own row by side-effect class so a quick
    glance tells you which ones are safe (preview) and which actually
    mutate state or post to public channels.

    Row 0 — card previews (ephemeral image only, no public posts, no DB
            writes). Useful for iterating on Pillow visuals without
            having to engineer real game state.
    Row 1 — force triggers (public posts / DB mutations). The same
            things /fitcheck-rotate-now, /recap-now, /setup-server do.
    Row 2 — diagnostics (read-only inspections).
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _admin_only(interaction):
            await interaction.response.send_message(
                "Admin / manage-guild only.",
                ephemeral=True, delete_after=8,
            )
            return False
        return True

    # ------ Row 0: previews (no side effects) -------------------------- #

    @discord.ui.button(
        label="Profile Card", emoji="🪪",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_test:profile_card", row=0,
    )
    async def preview_profile_card(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _preview_profile_card(interaction)

    @discord.ui.button(
        label="Rank-Up Card", emoji="📈",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_test:rank_up_card", row=0,
    )
    async def preview_rank_up(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _preview_rank_up_card(interaction)

    @discord.ui.button(
        label="Champion Card", emoji="🏆",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_test:champion_card", row=0,
    )
    async def preview_champion(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _preview_champion_card(interaction)

    @discord.ui.button(
        label="Drip Lord Card", emoji="👑",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_test:drip_card", row=0,
    )
    async def preview_drip(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _preview_drip_lord_card(interaction)

    @discord.ui.button(
        label="Recap Preview", emoji="📊",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_test:recap_preview", row=0,
    )
    async def preview_recap(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _preview_weekly_recap(interaction)

    # ------ Row 1: force triggers (real side effects) ------------------ #

    @discord.ui.button(
        label="Force Drip Lord Rotation", emoji="🌀",
        style=discord.ButtonStyle.danger,
        custom_id="admin_test:rotate_drip", row=1,
    )
    async def force_rotate_drip(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _force_rotate_drip(interaction)

    @discord.ui.button(
        label="Force Weekly Recap Post", emoji="📢",
        style=discord.ButtonStyle.danger,
        custom_id="admin_test:force_recap", row=1,
    )
    async def force_recap(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _force_recap(interaction)

    @discord.ui.button(
        label="Re-run /setup-server", emoji="🔧",
        style=discord.ButtonStyle.danger,
        custom_id="admin_test:resetup", row=1,
    )
    async def force_setup(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _force_setup_server(interaction)

    # ------ Row 2: diagnostics ----------------------------------------- #

    @discord.ui.button(
        label="Inspect Me", emoji="🔍",
        style=discord.ButtonStyle.primary,
        custom_id="admin_test:inspect_me", row=2,
    )
    async def inspect_me(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _diagnostic_inspect_me(interaction)

    @discord.ui.button(
        label="Health JSON", emoji="💓",
        style=discord.ButtonStyle.primary,
        custom_id="admin_test:health", row=2,
    )
    async def health_json(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _diagnostic_health(interaction)


# --- Row 0 button handlers (previews) ------------------------------------- #

async def _preview_profile_card(interaction: discord.Interaction) -> None:
    """Render the caller's real player card with whatever badges they
    currently qualify for. Useful for confirming new badge logic
    without standing up an alt account."""
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    row = await db.get_player_by_discord(interaction.user.id)
    if row is None:
        # Synth a placeholder profile so the renderer still produces
        # something — admins testing visuals shouldn't have to /verify
        # first to see what the card looks like.
        profile = wavu.PlayerProfile(
            tekken_id="TEST-0000-0000",
            display_name=interaction.user.display_name,
            main_char="Kazuya",
            rating_mu=2400.0,
            rank_tier="Tekken King",
        )
    else:
        profile = wavu.PlayerProfile(
            tekken_id=row["tekken_id"],
            display_name=row["display_name"],
            main_char=row["main_char"],
            rating_mu=row["rating_mu"],
            rank_tier=row["rank_tier"],
        )
    embed, file = await _profile_card_payload(profile, member=interaction.user)
    if file is not None:
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


async def _preview_rank_up_card(interaction: discord.Interaction) -> None:
    """Fake Tenryu → Mighty Ruler so the section colour stripe crosses
    a band boundary (purple → amber). Best test of the visual."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    name = member.display_name if member else "Test Player"
    try:
        buf = await tournament_render.render_rank_up_card(
            player_name=name,
            character="Kazuya",
            from_rank="Tenryu",
            to_rank="Mighty Ruler",
        )
    except Exception:
        log.exception("[test-panel] rank-up render failed")
        await interaction.followup.send(
            "Render failed — check logs.", ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Preview · Rank-Up Card",
        description="Fake promotion (Tenryu → Mighty Ruler, Kazuya). "
                    "No actual rank change.",
        color=discord.Color.from_rgb(245, 180, 95),
    )
    embed.set_image(url="attachment://preview-rank-up.png")
    await interaction.followup.send(
        embed=embed,
        file=discord.File(buf, filename="preview-rank-up.png"),
        ephemeral=True,
    )


async def _preview_champion_card(interaction: discord.Interaction) -> None:
    """Fake tournament champion render with placeholder data."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    name = (
        interaction.user.display_name
        if isinstance(interaction.user, discord.Member) else "Test Player"
    )
    try:
        buf = await tournament_render.render_tournament_champion_card(
            tournament_name="Spring Showdown · Test",
            winner_name=name,
            winner_character="Heihachi",
            winner_rank="Tekken Emperor",
            runner_up_name="Runner-Up Dummy",
            entrants=16,
            rounds_played=4,
        )
    except Exception:
        log.exception("[test-panel] champion render failed")
        await interaction.followup.send(
            "Render failed — check logs.", ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Preview · Tournament Champion Card",
        description="Fake bracket. No tournament was harmed.",
        color=discord.Color.from_rgb(212, 175, 55),
    )
    embed.set_image(url="attachment://preview-champion.png")
    await interaction.followup.send(
        embed=embed,
        file=discord.File(buf, filename="preview-champion.png"),
        ephemeral=True,
    )


async def _preview_drip_lord_card(interaction: discord.Interaction) -> None:
    """Fake Drip Lord celebration with brand-fallback panel."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    name = (
        interaction.user.display_name
        if isinstance(interaction.user, discord.Member) else "Test Player"
    )
    try:
        buf = await tournament_render.render_drip_lord_card(
            winner_name=name,
            character="Lili",
            rank_tier="Bushin",
            fit_image_bytes=None,  # uses brand fallback panel
            net_score=42,
        )
    except Exception:
        log.exception("[test-panel] drip-lord render failed")
        await interaction.followup.send(
            "Render failed — check logs.", ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Preview · Drip Lord Card",
        description="Fake crowning, brand-fallback panel (no fit image).",
        color=discord.Color.from_rgb(212, 175, 55),
    )
    embed.set_image(url="attachment://preview-drip.png")
    await interaction.followup.send(
        embed=embed,
        file=discord.File(buf, filename="preview-drip.png"),
        ephemeral=True,
    )


async def _preview_weekly_recap(interaction: discord.Interaction) -> None:
    """Render the recap with current real stats but don't post — handy
    for verifying tile layout after a stats change without waiting on
    the weekly cycle."""
    if interaction.guild is None:
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    since_iso = since.isoformat()
    top_fits = await db.top_fitchecks_in_window(
        guild_id=guild.id, since_iso=since_iso, limit=1,
    )
    top_fit = top_fits[0] if top_fits else None
    new_members = await db.count_new_players_since(since_iso)
    fitchecks_posted = await db.count_fitchecks_since(guild.id, since_iso)
    tournaments_completed = await db.count_tournaments_completed_since(
        guild.id, since_iso,
    )

    drip_role = discord.utils.get(guild.roles, name=DRIP_LORD_ROLE_NAME)
    drip_holder = drip_role.members[0] if drip_role and drip_role.members else None
    drip_player = (
        await db.get_player_by_discord(drip_holder.id) if drip_holder else None
    )

    top_fit_poster = None
    top_fit_character = None
    top_fit_net: int | None = None
    if top_fit is not None:
        poster = guild.get_member(top_fit["poster_id"])
        top_fit_poster = poster.display_name if poster else f"<@{top_fit['poster_id']}>"
        top_fit_character = top_fit["character"]
        top_fit_net = int(top_fit["ups"]) - int(top_fit["downs"])

    week_label = f"{since:%Y-%m-%d} → {now:%Y-%m-%d}"

    try:
        buf = await tournament_render.render_weekly_recap_card(
            week_label=week_label,
            drip_lord_name=drip_holder.display_name if drip_holder else None,
            drip_lord_character=drip_player["main_char"] if drip_player else None,
            top_fit_poster=top_fit_poster,
            top_fit_character=top_fit_character,
            top_fit_net=top_fit_net,
            new_members=new_members,
            fitchecks_posted=fitchecks_posted,
            tournaments_completed=tournaments_completed,
        )
    except Exception:
        log.exception("[test-panel] recap render failed")
        await interaction.followup.send(
            "Render failed — check logs.", ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Preview · Weekly Recap",
        description=(
            "Live stats, current week. Not posted to "
            f"#{tournament_render.__name__ and 'announcements'}."
        ),
        color=discord.Color.from_rgb(212, 175, 55),
    )
    embed.set_image(url="attachment://preview-recap.png")
    await interaction.followup.send(
        embed=embed,
        file=discord.File(buf, filename="preview-recap.png"),
        ephemeral=True,
    )


# --- Row 1 button handlers (real side effects) ---------------------------- #

async def _force_rotate_drip(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return
    fitcheck_cog = interaction.client.get_cog("Fitcheck")
    if fitcheck_cog is None:
        await interaction.response.send_message(
            "Fitcheck cog not loaded — should be impossible if the bot booted.",
            ephemeral=True, delete_after=10,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    rotator = fitcheck_cog._rotator
    result = await rotator.rotate_one_guild(interaction.guild, force=True)
    await interaction.followup.send(f"Drip Lord rotation: {result}", ephemeral=True)


async def _force_recap(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return
    recap_cog = interaction.client.get_cog("Recap")
    if recap_cog is None:
        await interaction.response.send_message(
            "Recap cog not loaded.", ephemeral=True, delete_after=10,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    poster = recap_cog._poster
    result = await poster.post_for_guild(interaction.guild, force=True)
    await interaction.followup.send(f"Weekly recap: {result}", ephemeral=True)


async def _force_setup_server(interaction: discord.Interaction) -> None:
    """Re-runs /setup-server. The actual command method lives on the
    Setup cog; we delegate so any future setup updates apply uniformly
    here."""
    setup_cog = interaction.client.get_cog("Setup")
    if setup_cog is None:
        await interaction.response.send_message(
            "Setup cog not loaded.", ephemeral=True, delete_after=10,
        )
        return
    # The slash command itself takes the same interaction object — the
    # framework's perm checks won't trigger for direct callbacks, but
    # we're already gated by interaction_check above.
    await setup_cog.setup_server.callback(setup_cog, interaction)


# --- Row 2 button handlers (diagnostics) ---------------------------------- #

async def _diagnostic_inspect_me(interaction: discord.Interaction) -> None:
    cog = interaction.client.get_cog("Admin")
    if cog is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Couldn't run inspect.", ephemeral=True, delete_after=8)
        return
    # Direct delegation to the existing slash command handler — it
    # builds the embed we want and sends it ephemerally.
    await cog.admin_inspect_user.callback(cog, interaction, interaction.user)


async def _diagnostic_health(interaction: discord.Interaction) -> None:
    """Probe the bot's own health endpoint. Skips with a friendly
    message if BOT_HEALTH_PORT isn't set."""
    port_raw = os.environ.get("BOT_HEALTH_PORT")
    if not port_raw:
        await interaction.response.send_message(
            "Health endpoint isn't enabled. Set `BOT_HEALTH_PORT=<int>` "
            "in `/opt/tekken-bot/.env` and restart the service.",
            ephemeral=True, delete_after=12,
        )
        return
    try:
        port = int(port_raw)
    except ValueError:
        await interaction.response.send_message(
            f"BOT_HEALTH_PORT={port_raw!r} isn't an int.",
            ephemeral=True, delete_after=10,
        )
        return
    host = os.environ.get("BOT_HEALTH_HOST", "127.0.0.1")
    url = f"http://{host}:{port}/healthz"
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                body = await resp.text()
                status = resp.status
    except Exception as e:
        await interaction.followup.send(
            f"Probe failed: `{e}`",
            ephemeral=True,
        )
        return
    color = (
        discord.Color.green() if status == 200
        else discord.Color.orange() if status == 503
        else discord.Color.red()
    )
    embed = discord.Embed(
        title=f"GET {url} → HTTP {status}",
        description=f"```json\n{body[:1900]}\n```",
        color=color,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # Register the test-panel view so its button custom_ids resolve
        # after a bot restart. The panel itself is sent ephemerally per
        # invocation, but discord.py still wants the view registered.
        self.bot.add_view(AdminTestPanelView())

    @app_commands.command(
        name="admin-test-panel",
        description="(Admin) Ephemeral panel of test buttons for every visual + scheduled feature.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def admin_test_panel(self, interaction: discord.Interaction) -> None:
        if not _admin_only(interaction):
            await interaction.response.send_message(
                "Admin / manage-guild only.",
                ephemeral=True, delete_after=8,
            )
            return
        embed = discord.Embed(
            title="🧪 Admin Test Panel",
            description=(
                "Buttons grouped by side-effect class.\n\n"
                "**Row 1 — Previews** *(ephemeral image only, no posts)*\n"
                "Render each card type with placeholder or live data.\n\n"
                "**Row 2 — Force triggers** *(real public posts / DB writes)*\n"
                "Run scheduled tasks on demand.\n\n"
                "**Row 3 — Diagnostics** *(read-only)*\n"
                "Inspect your own state, probe the health endpoint."
            ),
            color=discord.Color.purple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=AdminTestPanelView(),
            ephemeral=True,
        )

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
