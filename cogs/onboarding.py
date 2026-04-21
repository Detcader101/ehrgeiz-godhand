from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import audit
import db
import ewgf
import wavu

log = logging.getLogger(__name__)

VERIFIED_ROLE_NAME = os.environ.get("VERIFIED_ROLE_NAME", "Verified")

# Spec §5.2 — different-ID re-links wait this long after an unlink.
# Same-ID re-link is allowed immediately (a user who unlinks themselves by
# mistake should be able to recover without waiting a week).
RELINK_COOLDOWN = timedelta(days=7)

_ID_NORMALIZE_RE = re.compile(r"[\s\-_]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_id(s: str | None) -> str:
    return _ID_NORMALIZE_RE.sub("", s or "").lower()


def _cooldown_remaining(unlinked_at_iso: str) -> timedelta | None:
    """Returns time left in the relink cooldown, or None if it's already over."""
    try:
        unlinked = datetime.fromisoformat(unlinked_at_iso)
    except ValueError:
        return None
    elapsed = datetime.now(timezone.utc) - unlinked
    if elapsed >= RELINK_COOLDOWN:
        return None
    return RELINK_COOLDOWN - elapsed


def _format_duration(td: timedelta) -> str:
    total_s = int(td.total_seconds())
    days, rem = divmod(total_s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 1)}m"


async def _resolve_rank(tekken_id: str, *, force_refresh: bool = False) -> str | None:
    """Try wavu's replay stream first (authoritative for very recent matches);
    fall back to ewgf.gg (covers inactive players). Returns None if neither
    source has a parseable rank.

    `force_refresh=True` bypasses the cache in both sources — used by the
    Refresh Rank flow where the user is signalling "I just played, re-check."
    """
    try:
        result = await wavu.find_player_rank(tekken_id, force_refresh=force_refresh)
        if result is not None:
            return result[1]
    except wavu.WavuError as e:
        log.warning("wavu rank lookup failed for %s: %s", tekken_id, e)
    try:
        return await ewgf.find_player_rank(tekken_id, force_refresh=force_refresh)
    except ewgf.EwgfError as e:
        log.warning("ewgf rank lookup failed for %s: %s", tekken_id, e)
        return None


async def _ensure_role(guild: discord.Guild, name: str, *, reason: str) -> discord.Role:
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        role = await guild.create_role(name=name, reason=reason, mentionable=False)
    return role


# Role names this bot has historically created that are NOT in the current
# valid-rank list. Kept forever so we can strip them on re-sync even after the
# canonical rank table changes.
_LEGACY_BOT_RANK_ROLES: set[str] = {
    "Unranked",
    # Old μ-derived tier names from the first buggy iteration:
    "Vindicator", "Initiate", "Usurper",
    "Revered Ruler", "Divine Ruler",
    "Yaksa", "Ryujin",
    "True God of Destruction",
}


def _bot_managed_rank_names() -> set[str]:
    return set(wavu.ALL_RANK_NAMES) | _LEGACY_BOT_RANK_ROLES


async def _apply_rank_and_verified(member: discord.Member, profile: wavu.PlayerProfile) -> None:
    guild = member.guild
    verified = await _ensure_role(guild, VERIFIED_ROLE_NAME, reason="Onboarding")

    managed = _bot_managed_rank_names()
    if profile.rank_tier:
        rank_role = await _ensure_role(guild, profile.rank_tier, reason="Tekken rank sync")
        to_remove = [r for r in member.roles
                     if r.name in managed and r.id != rank_role.id]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank re-sync")
        await member.add_roles(verified, rank_role, reason="Onboarding verified")
    else:
        # No rank resolved — grant Verified only, strip any stale rank role.
        to_remove = [r for r in member.roles if r.name in managed]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank cleared")
        await member.add_roles(verified, reason="Onboarding verified (no rank)")


async def _auto_delete(interaction: discord.Interaction, delay: float = 12.0) -> None:
    """Delete the interaction's ephemeral response after `delay` seconds."""
    try:
        await asyncio.sleep(delay)
        await interaction.delete_original_response()
    except (discord.NotFound, discord.HTTPException, asyncio.CancelledError):
        pass


def _schedule_delete(interaction: discord.Interaction, delay: float = 12.0) -> None:
    asyncio.create_task(_auto_delete(interaction, delay))


# --------------------------------------------------------------------------- #
# Interactive flow                                                             #
# --------------------------------------------------------------------------- #

class TekkenIdModal(discord.ui.Modal, title="Enter your Tekken ID"):
    tekken_id: discord.ui.TextInput = discord.ui.TextInput(
        label="Tekken ID (Polaris Battle ID)",
        placeholder="e.g. 3mN929qaBEEG",
        min_length=8,
        max_length=20,
        required=True,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        entered = str(self.tekken_id.value).strip()

        # Uniqueness check BEFORE we hit wavu (saves a request on obvious collisions).
        existing = await db.get_player_by_tekken_id(entered)
        if existing and existing["discord_id"] != interaction.user.id:
            await interaction.followup.send(
                f"That Tekken ID is already claimed by <@{existing['discord_id']}>. "
                "If that's wrong, ask an admin to run `/admin-link` to correct it.",
                ephemeral=True, delete_after=15,
            )
            return

        # Relink cooldown (spec §5.2). Same-ID re-link is allowed immediately;
        # different-ID re-link waits 7 days. Admin /admin-link bypasses.
        last_unlink = await db.get_last_unlink(interaction.user.id)
        if last_unlink is not None:
            remaining = _cooldown_remaining(last_unlink["unlinked_at"])
            if remaining is not None and _normalize_id(last_unlink["tekken_id"]) != _normalize_id(entered):
                await interaction.followup.send(
                    f"You unlinked recently. You can re-link a *different* Tekken ID "
                    f"in **{_format_duration(remaining)}**. "
                    f"Re-linking your previous ID (`{last_unlink['tekken_id']}`) "
                    "is allowed immediately. An admin can override with `/admin-link`.",
                    ephemeral=True, delete_after=20,
                )
                return

        try:
            profile = await wavu.lookup_player(entered)
        except wavu.PlayerNotFound as e:
            await interaction.followup.send(f"{e}", ephemeral=True, delete_after=15)
            return
        except wavu.WavuError as e:
            await interaction.followup.send(
                f"Data source error: {e}\nTry again in a minute.",
                ephemeral=True, delete_after=15,
            )
            return

        # Auto-detect rank: wavu replays first, then ewgf as fallback.
        profile.rank_tier = await _resolve_rank(entered)

        if profile.rank_tier:
            view = ConfirmProfileView(self.bot, interaction.user.id, profile)
            await interaction.followup.send(
                "Is this you? Confirm to get your rank role.",
                embed=_profile_embed(profile), view=view, ephemeral=True,
            )
        else:
            view = RankGroupSelectView(self.bot, interaction.user.id, profile)
            await interaction.followup.send(
                f"Found **{profile.display_name}** on wavu.wiki but couldn't find a "
                "recent ranked match. Pick your current rank:",
                view=view, ephemeral=True,
            )


def _profile_embed(p: wavu.PlayerProfile) -> discord.Embed:
    embed = discord.Embed(title=p.display_name, color=discord.Color.blurple())
    embed.add_field(name="Tekken ID", value=f"`{p.tekken_id}`", inline=False)
    embed.add_field(name="Main", value=p.main_char or "—", inline=True)
    embed.add_field(name="Rank", value=p.rank_tier or "—", inline=True)
    if p.rating_mu is not None:
        embed.add_field(name="Rating (μ)", value=f"{p.rating_mu:.0f}", inline=True)
    embed.set_footer(text="Source: wank.wavu.wiki")
    return embed


# --------------------------------------------------------------------------- #
# Rank self-report (two-stage dropdown when replay lookup fails)               #
# --------------------------------------------------------------------------- #

# Discord SelectMenus cap at 25 options. T8 has ~34 ranks so we split into
# color-coded groups first, then show the ranks inside that group.
_RANK_GROUPS: list[tuple[str, list[str]]] = [
    ("Beginner ranks", [wavu.TEKKEN_RANKS[i] for i in range(0, 3)]),
    ("Green ranks (Fighter → Eliminator)",
     [wavu.TEKKEN_RANKS[i] for i in range(3, 15)]),
    ("Blue ranks (Garyu → Battle Ruler)",
     [wavu.TEKKEN_RANKS[i] for i in range(15, 21)]),
    ("Purple ranks (Fujin → Bushin)",
     [wavu.TEKKEN_RANKS[i] for i in range(21, 25)]),
    ("Tekken ranks (King → God Supreme)",
     [wavu.TEKKEN_RANKS[i] for i in range(25, 29)]),
    ("God of Destruction",
     [wavu.TEKKEN_RANKS[i] for i in range(29, 34)]),
]


class _RankSpecificSelect(discord.ui.Select):
    def __init__(self, parent_view: "RankGroupSelectView", ranks: list[str]):
        options = [discord.SelectOption(label=r, value=r) for r in ranks]
        super().__init__(placeholder="Pick your exact rank…", options=options,
                         min_values=1, max_values=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        profile = self.parent_view.profile
        profile.rank_tier = chosen
        confirm = ConfirmProfileView(self.parent_view.bot, self.parent_view.user_id, profile)
        await interaction.response.edit_message(
            content="Is this correct? Confirm to get your role.",
            embed=_profile_embed(profile), view=confirm,
        )


class _RankGroupSelect(discord.ui.Select):
    def __init__(self, parent_view: "RankGroupSelectView"):
        options = [discord.SelectOption(label=name, value=str(idx))
                   for idx, (name, _) in enumerate(_RANK_GROUPS)]
        super().__init__(placeholder="Pick your rank tier…", options=options,
                         min_values=1, max_values=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        _group_name, ranks = _RANK_GROUPS[idx]
        # Replace the view's select with the specific-rank select.
        self.parent_view.clear_items()
        self.parent_view.add_item(_RankSpecificSelect(self.parent_view, ranks))
        await interaction.response.edit_message(view=self.parent_view)


class RankGroupSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, profile: wavu.PlayerProfile):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        self.profile = profile
        self.add_item(_RankGroupSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This isn't for you.", ephemeral=True
            )
            return False
        return True


class ConfirmProfileView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, profile: wavu.PlayerProfile):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.profile = profile

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This confirmation isn't for you.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes, that's me", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This has to be done from within the server.", ephemeral=True
            )
            return
        member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)

        try:
            await db.upsert_player(
                discord_id=member.id,
                tekken_id=self.profile.tekken_id,
                display_name=self.profile.display_name,
                main_char=self.profile.main_char,
                rating_mu=self.profile.rating_mu,
                rank_tier=self.profile.rank_tier,
                linked_by=None,
                now_iso=_now_iso(),
            )
        except sqlite3.IntegrityError:
            await interaction.response.send_message(
                "That Tekken ID was just claimed by someone else. Ask an admin for help.",
                ephemeral=True, delete_after=15,
            )
            return

        try:
            await _apply_rank_and_verified(member, self.profile)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I verified you in the database but I couldn't assign your roles — "
                "my role needs to be positioned above the rank roles. Ask an admin.",
                ephemeral=True, delete_after=15,
            )
            return

        await interaction.response.edit_message(
            content=f"Verified. Welcome, {self.profile.display_name}.",
            embed=None, view=None,
        )
        _schedule_delete(interaction, delay=8)

        await audit.post_event(
            guild,
            title="Player linked",
            color=discord.Color.green(),
            fields=[
                ("Discord", f"{member.mention} (`{member.id}`)", True),
                ("Tekken ID", f"`{self.profile.tekken_id}`", True),
                ("Display name", self.profile.display_name, True),
                ("Main", self.profile.main_char or "—", True),
                ("Rank", self.profile.rank_tier or "—", True),
            ],
        )

    @discord.ui.button(label="No, re-enter", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(TekkenIdModal(self.bot))


# --------------------------------------------------------------------------- #
# Shared flows (called by both slash commands and panel buttons)               #
# --------------------------------------------------------------------------- #

async def _flow_verify_start(interaction: discord.Interaction, bot: commands.Bot) -> None:
    existing = await db.get_player_by_discord(interaction.user.id)
    if existing:
        await interaction.response.send_message(
            f"You're already verified as **{existing['display_name']}** "
            f"(`{existing['tekken_id']}`). Use **Refresh Rank** to update your rank, "
            "or ask an admin to change your link.",
            ephemeral=True, delete_after=15,
        )
        return
    await interaction.response.send_modal(TekkenIdModal(bot))


async def _flow_refresh(interaction: discord.Interaction, bot: commands.Bot) -> None:
    row = await db.get_player_by_discord(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "You're not linked yet. Click **Verify** first.",
            ephemeral=True, delete_after=10,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        profile = await wavu.lookup_player(row["tekken_id"], force_refresh=True)
    except (wavu.PlayerNotFound, wavu.WavuError) as e:
        await interaction.followup.send(f"{e}", ephemeral=True, delete_after=15)
        return

    rank_name = await _resolve_rank(row["tekken_id"], force_refresh=True)

    member = interaction.guild.get_member(interaction.user.id)
    stored = row["rank_tier"]
    stored_is_valid = stored in wavu.ALL_RANK_NAMES

    async def _save_and_apply(rank_tier: str | None) -> None:
        profile.rank_tier = rank_tier
        await db.upsert_player(
            discord_id=member.id,
            tekken_id=profile.tekken_id,
            display_name=profile.display_name,
            main_char=profile.main_char,
            rating_mu=profile.rating_mu,
            rank_tier=profile.rank_tier,
            linked_by=row["linked_by"],
            now_iso=_now_iso(),
        )
        await _apply_rank_and_verified(member, profile)
        if rank_tier is not None and rank_tier != stored:
            await audit.post_event(
                interaction.guild,
                title="Rank changed",
                color=discord.Color.gold(),
                fields=[
                    ("Discord", f"{member.mention} (`{member.id}`)", True),
                    ("Tekken ID", f"`{profile.tekken_id}`", True),
                    ("From", stored or "—", True),
                    ("To", rank_tier, True),
                    ("Trigger", "self-refresh", True),
                ],
            )

    if rank_name is not None:
        await _save_and_apply(rank_name)
        await interaction.followup.send(
            content="Updated.", embed=_profile_embed(profile),
            ephemeral=True, delete_after=12,
        )
    elif stored_is_valid:
        await _save_and_apply(stored)
        await interaction.followup.send(
            content="Updated. *(Couldn't auto-detect rank — kept your existing one.)*",
            embed=_profile_embed(profile), ephemeral=True, delete_after=12,
        )
    else:
        view = RankGroupSelectView(bot, interaction.user.id, profile)
        await interaction.followup.send(
            f"Couldn't auto-detect a rank for **{profile.display_name}**. "
            "Pick your current rank:",
            view=view, ephemeral=True,
        )


async def _flow_set_rank(interaction: discord.Interaction, bot: commands.Bot) -> None:
    row = await db.get_player_by_discord(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "You're not linked yet. Click **Verify** first.",
            ephemeral=True, delete_after=10,
        )
        return
    profile = wavu.PlayerProfile(
        tekken_id=row["tekken_id"],
        display_name=row["display_name"],
        main_char=row["main_char"],
        rating_mu=row["rating_mu"],
        rank_tier=None,
    )
    view = RankGroupSelectView(bot, interaction.user.id, profile)
    await interaction.response.send_message(
        "Pick your current rank:", view=view, ephemeral=True,
    )


async def _flow_profile(interaction: discord.Interaction) -> None:
    row = await db.get_player_by_discord(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "You're not linked yet. Click **Verify** first.",
            ephemeral=True, delete_after=10,
        )
        return
    profile = wavu.PlayerProfile(
        tekken_id=row["tekken_id"],
        display_name=row["display_name"],
        main_char=row["main_char"],
        rating_mu=row["rating_mu"],
        rank_tier=row["rank_tier"],
    )
    await interaction.response.send_message(
        embed=_profile_embed(profile), ephemeral=True, delete_after=20,
    )


class _ConfirmUnlinkView(discord.ui.View):
    def __init__(self, user_id: int, tekken_id: str | None, display_name: str | None):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.tekken_id = tekken_id
        self.display_name = display_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Yes, unlink me", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await db.delete_player(interaction.user.id)
        await db.record_unlink(interaction.user.id, self.tekken_id, _now_iso())
        member = interaction.guild.get_member(interaction.user.id)
        if member is not None:
            managed = _bot_managed_rank_names()
            to_remove = [r for r in member.roles if r.name in managed
                         or r.name == VERIFIED_ROLE_NAME]
            if to_remove:
                try:
                    await member.remove_roles(*to_remove, reason="Self-unlink")
                except discord.Forbidden:
                    pass
        await interaction.response.edit_message(
            content="Unlinked. Click **Verify** anytime to link again.",
            embed=None, view=None,
        )
        _schedule_delete(interaction, delay=10)

        await audit.post_event(
            interaction.guild,
            title="Player unlinked (self)",
            color=discord.Color.dark_grey(),
            fields=[
                ("Discord", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Tekken ID", f"`{self.tekken_id}`" if self.tekken_id else "—", True),
                ("Display name", self.display_name or "—", True),
            ],
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled.", embed=None, view=None,
        )
        _schedule_delete(interaction, delay=5)


async def _flow_unlink(interaction: discord.Interaction) -> None:
    row = await db.get_player_by_discord(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "You're not linked.", ephemeral=True, delete_after=8,
        )
        return
    await interaction.response.send_message(
        f"Unlink **{row['display_name']}** (`{row['tekken_id']}`)? "
        "This removes your rank role. You can re-verify anytime — note the "
        "7-day cooldown if you re-link to a *different* Tekken ID.",
        view=_ConfirmUnlinkView(
            interaction.user.id,
            tekken_id=row["tekken_id"],
            display_name=row["display_name"],
        ),
        ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# Persistent Player Hub panel                                                  #
# --------------------------------------------------------------------------- #

PANEL_KIND_PLAYER_HUB = "player_hub"


class PlayerHubView(discord.ui.View):
    """Persistent unified panel. Custom IDs must stay stable across restarts."""

    def __init__(self, bot: commands.Bot | None = None):
        super().__init__(timeout=None)
        # Bot is None when the View is reconstructed on startup from custom_id;
        # we resolve it from interaction.client inside callbacks.
        self._bot = bot

    def _resolve_bot(self, interaction: discord.Interaction) -> commands.Bot:
        return self._bot or interaction.client  # type: ignore[return-value]

    @discord.ui.button(label="Verify",
                       style=discord.ButtonStyle.primary,
                       custom_id="hub:verify", row=0)
    async def verify(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_verify_start(interaction, self._resolve_bot(interaction))

    @discord.ui.button(label="My Profile",
                       style=discord.ButtonStyle.secondary,
                       custom_id="hub:profile", row=0)
    async def profile(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_profile(interaction)

    @discord.ui.button(label="Refresh Rank",
                       style=discord.ButtonStyle.success,
                       custom_id="hub:refresh", row=1)
    async def refresh(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_refresh(interaction, self._resolve_bot(interaction))

    @discord.ui.button(label="Set Rank Manually",
                       style=discord.ButtonStyle.secondary,
                       custom_id="hub:set_rank", row=1)
    async def set_rank(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_set_rank(interaction, self._resolve_bot(interaction))

    @discord.ui.button(label="Unlink Me",
                       style=discord.ButtonStyle.danger,
                       custom_id="hub:unlink", row=1)
    async def unlink(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_unlink(interaction)


def _player_hub_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Tekken 8 Player Hub",
        description=(
            "**New here?** Click **Verify** and enter your **Tekken ID** "
            "(the ~12-character Polaris Battle ID from *Main Menu → Community → "
            "My Profile*). The bot checks wavu.wiki, confirms it's you, and "
            "gives you your rank role.\n\n"
            "**Already verified?**\n"
            "• **Refresh Rank** — pull your latest rank from your recent matches.\n"
            "• **Set Rank Manually** — pick your rank from a dropdown (for when "
            "auto-detect can't find a recent match).\n"
            "• **My Profile** — see what the bot has on file for you.\n"
            "• **Unlink Me** — remove your link (with confirmation)."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="One Tekken ID per Discord account • Admins can override")
    return embed


# --------------------------------------------------------------------------- #
# Cog                                                                          #
# --------------------------------------------------------------------------- #

class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # Persistent view: custom_ids let Discord route button clicks back to this
        # View even after a bot restart.
        self.bot.add_view(PlayerHubView(self.bot))

    async def _delete_old_panel(self, guild: discord.Guild, kind: str) -> None:
        row = await db.get_panel(guild.id, kind)
        if row is None:
            return
        channel = guild.get_channel(row["channel_id"])
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(row["message_id"])
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # message was already deleted or inaccessible — proceed

    @app_commands.command(name="post-player-panel",
                          description="Admin: post (or repost) the Player Hub in this channel.")
    @app_commands.default_permissions(manage_guild=True)
    async def post_player_panel(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        await self._delete_old_panel(guild, PANEL_KIND_PLAYER_HUB)
        msg = await interaction.channel.send(
            embed=_player_hub_embed(), view=PlayerHubView(self.bot),
        )
        await db.set_panel(guild.id, PANEL_KIND_PLAYER_HUB, interaction.channel.id, msg.id)
        await interaction.response.send_message(
            "Player Hub posted.", ephemeral=True, delete_after=8,
        )

    @app_commands.command(name="refresh",
                          description="Re-sync your rank from wavu.wiki.")
    async def refresh(self, interaction: discord.Interaction):
        await _flow_refresh(interaction, self.bot)

    @app_commands.command(name="set-rank",
                          description="Manually set your rank (use if auto-detect is wrong).")
    async def set_rank(self, interaction: discord.Interaction):
        await _flow_set_rank(interaction, self.bot)

    @app_commands.command(name="admin-link",
                          description="Admin: force-link a Discord user to a Tekken ID.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        tekken_id="The player's Polaris Battle ID",
        rank="Override rank tier (exact name, e.g. 'Tekken Emperor'). "
             "Omit to auto-detect from recent replays.",
    )
    async def admin_link(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tekken_id: str,
        rank: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await wavu.lookup_player(tekken_id)
        except (wavu.PlayerNotFound, wavu.WavuError) as e:
            await interaction.followup.send(f"{e}", ephemeral=True, delete_after=15)
            return

        if rank is not None:
            if rank not in wavu.ALL_RANK_NAMES:
                await interaction.followup.send(
                    f"`{rank}` is not a known T8 rank. Valid ranks:\n"
                    + ", ".join(wavu.ALL_RANK_NAMES),
                    ephemeral=True, delete_after=20,
                )
                return
            profile.rank_tier = rank
        else:
            profile.rank_tier = await _resolve_rank(profile.tekken_id)

        existing = await db.get_player_by_tekken_id(profile.tekken_id)
        if existing and existing["discord_id"] != member.id:
            await db.delete_player(existing["discord_id"])

        await db.upsert_player(
            discord_id=member.id,
            tekken_id=profile.tekken_id,
            display_name=profile.display_name,
            main_char=profile.main_char,
            rating_mu=profile.rating_mu,
            rank_tier=profile.rank_tier,
            linked_by=interaction.user.id,
            now_iso=_now_iso(),
        )
        # Spec §5.4: admin override clears any pending relink cooldown.
        await db.clear_unlink(member.id)
        try:
            await _apply_rank_and_verified(member, profile)
        except discord.Forbidden:
            await interaction.followup.send(
                "Linked in DB but couldn't assign roles (role hierarchy).",
                ephemeral=True, delete_after=15,
            )
            return
        await interaction.followup.send(
            content=f"Linked {member.mention}.", embed=_profile_embed(profile),
            ephemeral=True, delete_after=12,
        )

        await audit.post_event(
            interaction.guild,
            title="Player linked (admin override)",
            color=discord.Color.purple(),
            fields=[
                ("Target", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Tekken ID", f"`{profile.tekken_id}`", True),
                ("Display name", profile.display_name, True),
                ("Rank", profile.rank_tier or "—", True),
                ("Rank source", "manual override" if rank else "auto-detect", True),
            ],
        )

    @app_commands.command(name="admin-unlink",
                          description="Admin: remove a Discord user's Tekken link.")
    @app_commands.default_permissions(manage_guild=True)
    async def admin_unlink(self, interaction: discord.Interaction, member: discord.Member):
        row = await db.get_player_by_discord(member.id)
        if row is None:
            await interaction.response.send_message(
                f"{member.mention} isn't linked.", ephemeral=True, delete_after=10,
            )
            return
        await db.delete_player(member.id)
        await db.record_unlink(member.id, row["tekken_id"], _now_iso())
        managed = _bot_managed_rank_names()
        to_remove = [r for r in member.roles if r.name in managed
                     or r.name == VERIFIED_ROLE_NAME]
        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason="Admin unlink")
            except discord.Forbidden:
                pass
        await interaction.response.send_message(
            f"Unlinked {member.mention} (was `{row['tekken_id']}`).",
            ephemeral=True, delete_after=12,
        )

        await audit.post_event(
            interaction.guild,
            title="Player unlinked (admin)",
            color=discord.Color.purple(),
            fields=[
                ("Target", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Tekken ID", f"`{row['tekken_id']}`", True),
                ("Display name", row["display_name"], True),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
