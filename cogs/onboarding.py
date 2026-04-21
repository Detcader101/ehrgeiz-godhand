from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
import wavu

log = logging.getLogger(__name__)

ONBOARDING_CHANNEL_ID = int(os.environ["ONBOARDING_CHANNEL_ID"])
VERIFIED_ROLE_NAME = os.environ.get("VERIFIED_ROLE_NAME", "Verified")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

        # Try to auto-detect rank from recent replays.
        try:
            rank_result = await wavu.find_player_rank(entered)
        except wavu.WavuError:
            rank_result = None
        if rank_result is not None:
            _rid, rank_name = rank_result
            profile.rank_tier = rank_name

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

    @discord.ui.button(label="No, re-enter", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(TekkenIdModal(self.bot))


class VerifyView(discord.ui.View):
    """Persistent view posted in the onboarding channel."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Verify with Tekken ID",
        style=discord.ButtonStyle.primary,
        custom_id="tekken_bot:verify",
    )
    async def verify(self, interaction: discord.Interaction, _button: discord.ui.Button):
        existing = await db.get_player_by_discord(interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"You're already verified as **{existing['display_name']}** "
                f"(`{existing['tekken_id']}`). Use `/refresh` to update your rank, "
                "or ask an admin to change your link.",
                ephemeral=True, delete_after=15,
            )
            return
        await interaction.response.send_modal(TekkenIdModal(self.bot))


# --------------------------------------------------------------------------- #
# Cog                                                                          #
# --------------------------------------------------------------------------- #

class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyView(self.bot))

    @app_commands.command(name="post-verify-panel",
                          description="Admin: post the verification button in this channel.")
    @app_commands.default_permissions(manage_guild=True)
    async def post_verify_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Welcome. Verify to get in.",
            description=(
                "Click the button below and enter your **Tekken ID** "
                "(the ~12-character Polaris Battle ID on your player card).\n\n"
                "The bot will check wavu.wiki, confirm it's you, and grant you "
                "your rank role. One Tekken ID per Discord account — admins can "
                "override if you need a change."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.channel.send(embed=embed, view=VerifyView(self.bot))
        await interaction.response.send_message(
            "Panel posted.", ephemeral=True, delete_after=8,
        )

    @app_commands.command(name="refresh",
                          description="Re-sync your rank from wavu.wiki.")
    async def refresh(self, interaction: discord.Interaction):
        row = await db.get_player_by_discord(interaction.user.id)
        if row is None:
            await interaction.response.send_message(
                "You're not linked yet. Use the Verify button first.",
                ephemeral=True, delete_after=10,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await wavu.lookup_player(row["tekken_id"])
        except (wavu.PlayerNotFound, wavu.WavuError) as e:
            await interaction.followup.send(f"{e}", ephemeral=True, delete_after=15)
            return

        # Auto-detect rank from recent replays.
        try:
            rank_result = await wavu.find_player_rank(row["tekken_id"])
        except wavu.WavuError:
            rank_result = None

        member = interaction.guild.get_member(interaction.user.id)

        stored = row["rank_tier"]
        stored_is_valid = stored in wavu.ALL_RANK_NAMES

        if rank_result is not None:
            profile.rank_tier = rank_result[1]
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
            await interaction.followup.send(
                content="Updated.", embed=_profile_embed(profile),
                ephemeral=True, delete_after=12,
            )
        elif stored_is_valid:
            profile.rank_tier = stored
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
            await interaction.followup.send(
                content="Updated. *(No recent ranked match found — kept your existing rank.)*",
                embed=_profile_embed(profile), ephemeral=True, delete_after=12,
            )
        else:
            # No replay match, no prior valid rank → offer the self-report dropdown.
            view = RankGroupSelectView(self.bot, interaction.user.id, profile)
            await interaction.followup.send(
                f"Couldn't find a recent ranked match for **{profile.display_name}**. "
                "Pick your current rank:",
                view=view, ephemeral=True,
            )

    @app_commands.command(name="set-rank",
                          description="Manually set your rank (use if auto-detect is wrong).")
    async def set_rank(self, interaction: discord.Interaction):
        row = await db.get_player_by_discord(interaction.user.id)
        if row is None:
            await interaction.response.send_message(
                "You're not linked yet. Use the Verify button first.",
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
        view = RankGroupSelectView(self.bot, interaction.user.id, profile)
        await interaction.response.send_message(
            "Pick your current rank:", view=view, ephemeral=True,
        )

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
            try:
                rank_result = await wavu.find_player_rank(profile.tekken_id)
            except wavu.WavuError:
                rank_result = None
            profile.rank_tier = rank_result[1] if rank_result else None

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
