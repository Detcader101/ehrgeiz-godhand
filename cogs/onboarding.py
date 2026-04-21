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
ORGANIZER_ROLE_NAME = os.environ.get("ORGANIZER_ROLE_NAME", "Organizer")

# Spec §5.2 — different-ID re-links wait this long after an unlink.
# Same-ID re-link is allowed immediately (a user who unlinks themselves by
# mistake should be able to recover without waiting a week).
RELINK_COOLDOWN = timedelta(days=7)

# Spec §5.3 — claims at this rank or higher require organizer confirmation
# before the rank role is granted. The string must match a name in
# wavu.TEKKEN_RANKS (case-sensitive).
PENDING_THRESHOLD_RANK = "Tekken King"
PENDING_EXPIRY = timedelta(hours=72)
PENDING_SWEEP_INTERVAL = timedelta(hours=1)

# Roles permitted to confirm/reject a pending verification.
_RESOLVER_ROLES = {"Admin", "Moderator", ORGANIZER_ROLE_NAME}

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


# --- Pending-verification helpers (spec §5.3) ----------------------------- #

def _rank_ordinal(name: str | None) -> int | None:
    if name is None:
        return None
    for k, v in wavu.TEKKEN_RANKS.items():
        if v == name:
            return k
    return None


_PENDING_THRESHOLD_ORDINAL: int | None = _rank_ordinal(PENDING_THRESHOLD_RANK)
if _PENDING_THRESHOLD_ORDINAL is None:
    log.warning("PENDING_THRESHOLD_RANK %r not found in wavu.TEKKEN_RANKS — "
                "pending-verification check will be a no-op", PENDING_THRESHOLD_RANK)


def _requires_pending(rank_name: str | None) -> bool:
    if rank_name is None or _PENDING_THRESHOLD_ORDINAL is None:
        return False
    ordinal = _rank_ordinal(rank_name)
    return ordinal is not None and ordinal >= _PENDING_THRESHOLD_ORDINAL


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


async def _grant_verified_only(member: discord.Member, *, reason: str) -> None:
    """Grant Verified role and strip any rank roles. Used during Pending
    Verification — the user is "in" the server but no rank role is granted
    until an organizer confirms the high-rank claim."""
    guild = member.guild
    verified = await _ensure_role(guild, VERIFIED_ROLE_NAME, reason=reason)
    managed = _bot_managed_rank_names()
    to_remove = [r for r in member.roles if r.name in managed]
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason=reason)
        except discord.Forbidden:
            pass
    await member.add_roles(verified, reason=reason)


async def _start_pending_verification(
    *,
    guild: discord.Guild,
    member: discord.Member,
    tekken_id: str,
    rank_tier: str,
    rank_source: str,
) -> None:
    """Put a player into Pending Verification (spec §5.3).

    Side effects, in order:
      1. Strips any old rank role and grants Verified-only.
      2. Inserts/replaces the pending_verifications row (clearing any prior
         message_id — the old audit message becomes orphaned).
      3. Posts a Confirm/Reject embed to #verification-log and backfills
         message_id so the buttons can find the row on click.

    Best-effort on failures: an unreachable verification-log channel doesn't
    block the user from being Verified.
    """
    try:
        await _grant_verified_only(member, reason=f"Pending verification ({rank_tier})")
    except discord.Forbidden:
        log.warning(
            "Couldn't grant Verified to %s in pending flow (role hierarchy)", member.id
        )

    await db.upsert_pending_verification(
        discord_id=member.id,
        guild_id=guild.id,
        tekken_id=tekken_id,
        rank_tier=rank_tier,
        rank_source=rank_source,
        now_iso=_now_iso(),
    )

    channel = discord.utils.get(guild.text_channels, name=audit.VERIFICATION_LOG_CHANNEL)
    if channel is None:
        log.warning(
            "Pending verification for %s in guild %s has no audit channel "
            "to post to — request stored but no UI",
            member.id, guild.id,
        )
        return

    embed = discord.Embed(
        title="⏳ Pending high-rank verification",
        description=(
            f"{member.mention} has claimed **{rank_tier}** "
            f"(source: *{rank_source}*) — needs an organizer to confirm "
            "before the rank role is granted.\n\n"
            "Click **Confirm** if this is the real player. **Reject** if it's "
            "an over-claim or impersonation. After **72 hours** with no action, "
            "the request is marked stale (their profile shows the claimed rank "
            "without the role) but Confirm/Reject still work.\n\n"
            "Verification level: **Verified** ✓ — they're in the server, "
            "just no rank role until you act."
        ),
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Discord", value=f"{member.mention} (`{member.id}`)", inline=True)
    embed.add_field(name="Tekken ID", value=f"`{tekken_id}`", inline=True)
    embed.add_field(name="Claimed rank", value=rank_tier, inline=True)
    embed.set_footer(text=f"Source: {rank_source}")

    try:
        msg = await channel.send(embed=embed, view=PendingVerificationView())
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("Couldn't post pending verification in guild %s: %s", guild.id, e)
        return

    await db.set_pending_message(member.id, channel.id, msg.id)


async def _cancel_pending_if_any(guild: discord.Guild | None, discord_id: int) -> None:
    """Drop a pending row and edit its audit message to "cancelled".
    Called when a player unlinks (their pending claim is no longer relevant)."""
    row = await db.get_pending_by_discord(discord_id)
    if row is None:
        return
    await db.delete_pending_verification(discord_id)
    if guild is None or row["channel_id"] is None or row["message_id"] is None:
        return
    channel = guild.get_channel(row["channel_id"])
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(row["message_id"])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    embed = msg.embeds[0] if msg.embeds else None
    if embed is None:
        return
    embed.title = "🚫 Pending verification — cancelled"
    embed.color = discord.Color.dark_grey()
    embed.add_field(
        name="Status",
        value="Player unlinked before this could be resolved.",
        inline=False,
    )
    try:
        await msg.edit(embed=embed, view=None)
    except (discord.Forbidden, discord.HTTPException):
        pass


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
                ephemeral=True,
            )
            return

        # Relink cooldown (spec §5.2). Same-ID re-link is allowed immediately;
        # different-ID re-link waits 7 days. Admins can clear with
        # /admin-clear-cooldown or override with /admin-link.
        last_unlink = await db.get_last_unlink(interaction.user.id)
        if last_unlink is not None:
            remaining = _cooldown_remaining(last_unlink["unlinked_at"])
            if remaining is not None and _normalize_id(last_unlink["tekken_id"]) != _normalize_id(entered):
                prev_id_str = (
                    f"`{last_unlink['tekken_id']}`"
                    if last_unlink["tekken_id"] else "your previous Tekken ID"
                )
                embed = discord.Embed(
                    title="⏳ Relink cooldown active",
                    description=(
                        f"You recently unlinked from {prev_id_str}.\n\n"
                        f"To prevent identity rotation, you can't link a **different** "
                        f"Tekken ID for **{_format_duration(remaining)}**.\n\n"
                        f"• Re-link {prev_id_str} → **allowed immediately**.\n"
                        f"• Need a different ID sooner? Ask an admin to run "
                        f"`/admin-clear-cooldown` on you, or `/admin-link` directly."
                    ),
                    color=discord.Color.orange(),
                )
                try:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                except discord.HTTPException as e:
                    # Defensive: if Discord drops the followup the user is left
                    # staring at a "thinking…" indicator. At least surface the
                    # failure server-side so we know to look.
                    log.warning(
                        "Cooldown followup failed for user=%s: %s",
                        interaction.user.id, e,
                    )
                return

        try:
            profile = await wavu.lookup_player(entered)
        except wavu.PlayerNotFound as e:
            await interaction.followup.send(f"{e}", ephemeral=True)
            return
        except wavu.WavuError as e:
            await interaction.followup.send(
                f"Data source error: {e}\nTry again in a minute.",
                ephemeral=True,
            )
            return

        # Auto-detect rank: wavu replays first, then ewgf as fallback.
        profile.rank_tier = await _resolve_rank(entered)

        if profile.rank_tier:
            view = ConfirmProfileView(
                self.bot, interaction.user.id, profile, rank_source="auto-detect",
            )
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
        confirm = ConfirmProfileView(
            self.parent_view.bot, self.parent_view.user_id, profile,
            rank_source="self-report",
        )
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
    def __init__(
        self, bot: commands.Bot, user_id: int, profile: wavu.PlayerProfile,
        *, rank_source: str = "auto-detect",
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.profile = profile
        self.rank_source = rank_source

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

        # Spec §5.3: a high-rank claim withholds the rank role until an
        # organizer confirms. We persist the player row with rank_tier=NULL
        # so My Profile shows "Pending" instead of the unverified claim.
        needs_pending = _requires_pending(self.profile.rank_tier)
        stored_rank = None if needs_pending else self.profile.rank_tier

        try:
            await db.upsert_player(
                discord_id=member.id,
                tekken_id=self.profile.tekken_id,
                display_name=self.profile.display_name,
                main_char=self.profile.main_char,
                rating_mu=self.profile.rating_mu,
                rank_tier=stored_rank,
                linked_by=None,
                now_iso=_now_iso(),
            )
        except sqlite3.IntegrityError:
            await interaction.response.send_message(
                "That Tekken ID was just claimed by someone else. Ask an admin for help.",
                ephemeral=True, delete_after=15,
            )
            return

        if needs_pending:
            await _start_pending_verification(
                guild=guild, member=member,
                tekken_id=self.profile.tekken_id,
                rank_tier=self.profile.rank_tier,  # type: ignore[arg-type]
                rank_source=self.rank_source,
            )
            await interaction.response.edit_message(
                content=(
                    f"Verified, {self.profile.display_name}.\n\n"
                    f"Your **{self.profile.rank_tier}** claim has been sent to "
                    "organizers — they'll confirm and grant the rank role within ~72h. "
                    "(High-rank claims get a sanity check; this prevents impersonation.)"
                ),
                embed=None, view=None,
            )
            _schedule_delete(interaction, delay=20)
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
                ("Source", self.rank_source, True),
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
        pending = await db.get_pending_by_discord(interaction.user.id)
        suffix = ""
        if pending is not None:
            suffix = (
                f"\n*A **{pending['rank_tier']}** claim is pending organizer "
                "confirmation — they'll grant the rank role when they action it.*"
            )
        await interaction.response.send_message(
            f"You're already verified as **{existing['display_name']}** "
            f"(`{existing['tekken_id']}`). Use **Refresh Rank** to update your rank, "
            f"or ask an admin to change your link.{suffix}",
            ephemeral=True, delete_after=20,
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
        await interaction.followup.send(f"{e}", ephemeral=True)
        return

    rank_name = await _resolve_rank(row["tekken_id"], force_refresh=True)

    member = interaction.guild.get_member(interaction.user.id)
    stored = row["rank_tier"]
    stored_is_valid = stored in wavu.ALL_RANK_NAMES

    async def _save_and_apply(rank_tier: str | None) -> bool:
        """Returns True if the rank went to Pending Verification, False if
        it was granted directly."""
        # Spec §5.3: only NEW high-rank claims trigger Pending. If the rank
        # didn't change, skip — the user already had this rank.
        needs_pending = (
            rank_tier is not None
            and rank_tier != stored
            and _requires_pending(rank_tier)
        )
        profile.rank_tier = None if needs_pending else rank_tier

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

        if needs_pending:
            await _start_pending_verification(
                guild=interaction.guild, member=member,
                tekken_id=profile.tekken_id,
                rank_tier=rank_tier,  # type: ignore[arg-type]
                rank_source="auto-detect (refresh)",
            )
            return True

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
        return False

    if rank_name is not None:
        went_pending = await _save_and_apply(rank_name)
        if went_pending:
            await interaction.followup.send(
                content=(
                    f"Your **{rank_name}** claim was sent to organizers for "
                    "confirmation — they'll grant the rank role within ~72h."
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                content="Updated.", embed=_profile_embed(profile),
                ephemeral=True,
            )
    elif stored_is_valid:
        # rank_tier == stored, so needs_pending is False; safe.
        await _save_and_apply(stored)
        await interaction.followup.send(
            content="Updated. *(Couldn't auto-detect rank — kept your existing one.)*",
            embed=_profile_embed(profile), ephemeral=True,
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
    pending = await db.get_pending_by_discord(interaction.user.id)
    extra = None
    if pending is not None:
        if pending["expired_at"] is None:
            extra = (
                f"⏳ **{pending['rank_tier']}** claim is pending organizer "
                "confirmation. The rank role hasn't been granted yet."
            )
        else:
            extra = (
                f"⌛ **{pending['rank_tier']}** claim is stale (>72h). "
                "Profile shows the claimed rank as self-reported until an "
                "organizer confirms — no rank role granted."
            )
    embed = _profile_embed(profile)
    if extra is not None:
        embed.add_field(name="Verification status", value=extra, inline=False)
    await interaction.response.send_message(
        embed=embed, ephemeral=True, delete_after=25,
    )


# --------------------------------------------------------------------------- #
# Pending verification UI (spec §5.3) — persistent View                        #
# --------------------------------------------------------------------------- #

class PendingVerificationView(discord.ui.View):
    """Persistent View attached to every #verification-log Pending message.

    Custom_ids are static; we look up the pending row by interaction.message.id
    so a single View instance can serve every pending message in every guild,
    even after a bot restart.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _resolver_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only action.", ephemeral=True, delete_after=8,
            )
            return False
        if not any(r.name in _RESOLVER_ROLES for r in member.roles):
            await interaction.response.send_message(
                "Only **Admins**, **Moderators**, or **Organizers** can resolve "
                "pending verifications.",
                ephemeral=True, delete_after=10,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success,
                       custom_id="pending:confirm", row=0)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if not await self._resolver_check(interaction):
            return
        await self._resolve(interaction, action="confirm")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger,
                       custom_id="pending:reject", row=0)
    async def reject(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if not await self._resolver_check(interaction):
            return
        await self._resolve(interaction, action="reject")

    async def _resolve(self, interaction: discord.Interaction, *, action: str) -> None:
        msg = interaction.message
        if msg is None:
            await interaction.response.send_message(
                "Couldn't read this message — try again.", ephemeral=True, delete_after=8,
            )
            return

        row = await db.get_pending_by_message(msg.id)
        if row is None:
            # Stale message — pending was already resolved/cancelled, or we
            # lost the row somehow. Strip the buttons so it can't be clicked
            # again, and tell the clicker.
            try:
                await msg.edit(view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass
            await interaction.response.send_message(
                "This pending request is no longer active (already resolved, "
                "the player unlinked, or the row was cleared).",
                ephemeral=True, delete_after=10,
            )
            return

        guild = interaction.guild
        target_member = None
        if guild is not None:
            target_member = guild.get_member(row["discord_id"])
            if target_member is None:
                try:
                    target_member = await guild.fetch_member(row["discord_id"])
                except (discord.NotFound, discord.HTTPException):
                    target_member = None

        granted = False
        if action == "confirm" and target_member is not None:
            # Re-fetch the player row so we don't clobber other fields.
            player_row = await db.get_player_by_discord(row["discord_id"])
            if player_row is not None:
                await db.upsert_player(
                    discord_id=row["discord_id"],
                    tekken_id=player_row["tekken_id"],
                    display_name=player_row["display_name"],
                    main_char=player_row["main_char"],
                    rating_mu=player_row["rating_mu"],
                    rank_tier=row["rank_tier"],
                    linked_by=player_row["linked_by"],
                    now_iso=_now_iso(),
                )
            confirmed_profile = wavu.PlayerProfile(
                tekken_id=row["tekken_id"],
                display_name=player_row["display_name"] if player_row else "(unknown)",
                main_char=player_row["main_char"] if player_row else None,
                rating_mu=player_row["rating_mu"] if player_row else None,
                rank_tier=row["rank_tier"],
            )
            try:
                await _apply_rank_and_verified(target_member, confirmed_profile)
                granted = True
            except discord.Forbidden:
                granted = False

        # Always delete the pending row whether granted or not — Reject simply
        # leaves the player with no rank role; Confirm's role grant is best-effort.
        await db.delete_pending_verification(row["discord_id"])

        # Update the audit message in place to lock in the resolution.
        embed = msg.embeds[0] if msg.embeds else None
        if embed is not None:
            if action == "confirm":
                embed.title = (
                    "✅ Verification confirmed" if granted
                    else "✅ Verification confirmed (role grant failed)"
                )
                embed.color = discord.Color.green() if granted else discord.Color.orange()
            else:
                embed.title = "❌ Verification rejected"
                embed.color = discord.Color.red()
            ts = int(datetime.now(timezone.utc).timestamp())
            embed.add_field(
                name="Resolved by",
                value=f"{interaction.user.mention} <t:{ts}:R>",
                inline=False,
            )
            if action == "confirm" and not granted:
                embed.add_field(
                    name="⚠ Role grant failed",
                    value="Likely a role-hierarchy issue — the bot's role needs "
                          "to be above the rank role. Player is Verified but has no rank role.",
                    inline=False,
                )

        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException as e:
            log.warning("Couldn't edit pending message after resolve: %s", e)


# --------------------------------------------------------------------------- #
# 72h Pending sweeper (spec §5.3 — auto-stale after expiry)                    #
# --------------------------------------------------------------------------- #

class _PendingSweeper:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="pending-sweeper")

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        # Wait for the bot to be fully connected before the first sweep so
        # guild/channel lookups are populated.
        await self.bot.wait_until_ready()
        await asyncio.sleep(15)
        while True:
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Pending sweeper iteration failed")
            await asyncio.sleep(PENDING_SWEEP_INTERVAL.total_seconds())

    async def _sweep_once(self) -> None:
        await self._sweep_pending()
        await self._purge_unlinks()

    async def _sweep_pending(self) -> None:
        cutoff = (datetime.now(timezone.utc) - PENDING_EXPIRY).isoformat()
        rows = await db.list_stale_pending(cutoff)
        if not rows:
            return
        log.info("Pending sweeper: marking %d row(s) stale", len(rows))
        for row in rows:
            await self._mark_one_stale(row)

    async def _purge_unlinks(self) -> None:
        """Privacy hygiene: once the cooldown has elapsed, drop the unlinks
        row entirely so we no longer hold the historical
        discord_id↔tekken_id pairing."""
        cutoff = (datetime.now(timezone.utc) - RELINK_COOLDOWN).isoformat()
        n = await db.purge_unlinks_before(cutoff)
        if n:
            log.info("Purged %d expired unlinks row(s)", n)

    async def _mark_one_stale(self, row) -> None:
        # Spec §5.3: after 72h, "auto-downgrade to a self-reported rank".
        # Implementation: stamp expired_at; copy claimed rank into players
        # so My Profile shows it (without a role); leave the buttons live
        # so an organizer can still confirm and grant the role later.
        await db.mark_pending_expired(row["discord_id"], _now_iso())
        player_row = await db.get_player_by_discord(row["discord_id"])
        if player_row is not None and player_row["rank_tier"] is None:
            await db.upsert_player(
                discord_id=row["discord_id"],
                tekken_id=player_row["tekken_id"],
                display_name=player_row["display_name"],
                main_char=player_row["main_char"],
                rating_mu=player_row["rating_mu"],
                rank_tier=row["rank_tier"],
                linked_by=player_row["linked_by"],
                now_iso=_now_iso(),
            )
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None or row["channel_id"] is None or row["message_id"] is None:
            return
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            msg = await channel.fetch_message(row["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        embed = msg.embeds[0] if msg.embeds else None
        if embed is None or (embed.title and embed.title.startswith("⌛")):
            return
        embed.title = "⌛ Pending — stale (72h elapsed)"
        embed.color = discord.Color.dark_gold()
        embed.add_field(
            name="Status",
            value="No organizer has acted within 72h. The player's profile now "
                  "shows the claimed rank as **self-reported** (no rank role). "
                  "Confirm/Reject still work to resolve the claim.",
            inline=False,
        )
        try:
            await msg.edit(embed=embed)  # keep the View live
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("Couldn't edit stale pending message: %s", e)


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
        await _cancel_pending_if_any(interaction.guild, interaction.user.id)
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
        self._sweeper = _PendingSweeper(bot)

    async def cog_load(self) -> None:
        # Persistent views: custom_ids let Discord route button clicks back to
        # these View instances even after a bot restart.
        self.bot.add_view(PlayerHubView(self.bot))
        self.bot.add_view(PendingVerificationView())
        self._sweeper.start()

    async def cog_unload(self) -> None:
        self._sweeper.stop()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        """Catch-all for slash-command exceptions in this cog.

        Without this, an uncaught error after `interaction.response.defer()`
        leaves the user staring at a permanent "thinking…" — the followup
        never lands and Discord shows no error. We log the traceback to the
        bot console (so it's visible in the PowerShell window the bot is
        running in) and send a short error embed back to the operator.
        """
        cmd_name = interaction.command.name if interaction.command else "<unknown>"
        log.exception("Slash command /%s raised: %s", cmd_name, error)
        msg = f"⚠ `/{cmd_name}` failed: `{type(error).__name__}: {error}`\n*Check the bot console for the full traceback.*"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            # Interaction is gone (>15min after defer) — nothing we can do.
            pass

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
            await interaction.followup.send(f"{e}", ephemeral=True)
            return

        if rank is not None:
            if rank not in wavu.ALL_RANK_NAMES:
                await interaction.followup.send(
                    f"`{rank}` is not a known T8 rank. Valid ranks:\n"
                    + ", ".join(wavu.ALL_RANK_NAMES),
                    ephemeral=True,
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
        # Spec §5.4: admin override clears any pending relink cooldown
        # and any in-flight pending verification (admins know their server).
        await db.clear_unlink(member.id)
        await _cancel_pending_if_any(interaction.guild, member.id)
        try:
            await _apply_rank_and_verified(member, profile)
        except discord.Forbidden:
            await interaction.followup.send(
                "Linked in DB but couldn't assign roles (role hierarchy).",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            content=f"Linked {member.mention}.", embed=_profile_embed(profile),
            ephemeral=True,
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

    @app_commands.command(
        name="admin-clear-cooldown",
        description="Admin: clear a user's relink cooldown so they can verify a different Tekken ID.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def admin_clear_cooldown(
        self, interaction: discord.Interaction, member: discord.Member,
    ):
        existing = await db.get_last_unlink(member.id)
        if existing is None:
            await interaction.response.send_message(
                f"{member.mention} has no active cooldown.",
                ephemeral=True, delete_after=10,
            )
            return
        await db.clear_unlink(member.id)
        await interaction.response.send_message(
            f"Cleared cooldown for {member.mention}. They can now link any Tekken ID.",
            ephemeral=True, delete_after=12,
        )
        await audit.post_event(
            interaction.guild,
            title="Cooldown cleared (admin)",
            color=discord.Color.purple(),
            fields=[
                ("Target", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Previous Tekken ID", f"`{existing['tekken_id']}`" if existing["tekken_id"] else "—", True),
                ("Unlinked at", existing["unlinked_at"], True),
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
        await _cancel_pending_if_any(interaction.guild, member.id)
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
