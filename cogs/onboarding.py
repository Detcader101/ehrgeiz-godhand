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
import channel_util
import db
import ewgf
import media
import tournament_render
import wavu
from view_util import ErrorHandledView

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

# Rank sweeper cadence. The sweeper wakes up every RANK_SWEEP_INTERVAL
# and walks every linked player; per-player API hits are skipped if the
# row was already refreshed within RANK_SWEEP_SKIP_IF_SYNCED_WITHIN so
# we don't hammer wavu/ewgf. Both are overridable via env var so an
# admin can tune API load without a code change.
RANK_SWEEP_INTERVAL = timedelta(
    seconds=int(os.environ.get("RANK_SWEEP_INTERVAL_SECONDS", 12 * 3600)),
)
RANK_SWEEP_SKIP_IF_SYNCED_WITHIN = timedelta(
    seconds=int(os.environ.get("RANK_SWEEP_SKIP_IF_SYNCED_WITHIN_SECONDS", 6 * 3600)),
)
# First sweep fires this many seconds after the bot reports ready — pushed
# out so the Pending sweeper (15s offset) and slash-command sync settle
# first. Env-overridable for dev: set to e.g. 5s when iterating on the
# sweeper locally.
RANK_SWEEP_STARTUP_DELAY = timedelta(
    seconds=int(os.environ.get("RANK_SWEEP_STARTUP_DELAY_SECONDS", 45)),
)

# Per-member pacing: small sleep between Discord role edits so a resync
# pass over 100+ members doesn't trip the guild-wide role-edit rate
# limiter (the same one that whinged at us on /reset-server).
_RESYNC_PER_MEMBER_DELAY = 0.25

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


async def _upgrade_display_name(profile: wavu.PlayerProfile) -> None:
    """If wavu fell back to the tekken_id as display_name (meaning it had no
    real name — usually because the player has no ranked-match data on
    wavu), try to fill in a better name from ewgf in place. Silently keeps
    the fallback if ewgf also has nothing."""
    if profile.display_name != profile.tekken_id:
        return
    try:
        better = await ewgf.lookup_display_name(profile.tekken_id)
    except ewgf.EwgfError as e:
        log.warning("ewgf name lookup failed for %s: %s", profile.tekken_id, e)
        return
    if better:
        profile.display_name = better


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
        # Newly-created roles default to position 1 (just above @everyone).
        # Push them up to just above Verified so the rank-role band sits
        # together in the hierarchy — otherwise the picker would show
        # fresh tiers dangling at the bottom.
        await _tuck_rank_role_above_verified(guild, role)
    return role


async def _tuck_rank_role_above_verified(
    guild: discord.Guild, role: discord.Role,
) -> None:
    """Move a newly-minted rank role to just above the Verified role so
    rank tiers cluster together in the server's role list. Best-effort:
    if the bot can't reposition (missing Manage Roles or its own role is
    too low), the role just stays at position 1 and the admin can drag
    it later."""
    verified = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
    if verified is None:
        return
    bot_me = guild.me
    if bot_me is None:
        return
    target_pos = verified.position + 1
    # Can't place a role at or above the bot's own top role.
    max_allowed = bot_me.top_role.position - 1
    if max_allowed < 1:
        return
    if target_pos > max_allowed:
        target_pos = max_allowed
    try:
        await guild.edit_role_positions(
            positions={role: target_pos},
            reason="Ehrgeiz Godhand: keep rank roles grouped",
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        log.debug("couldn't tuck rank role %s: %s", role.name, e)


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
    had_verified = any(r.id == verified.id for r in member.roles)
    if profile.rank_tier:
        rank_role = await _ensure_role(guild, profile.rank_tier, reason="Tekken rank sync")
        to_remove = [r for r in member.roles
                     if r.name in managed and r.id != rank_role.id]
        removed_names = [r.name for r in to_remove]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank re-sync")
        await member.add_roles(verified, rank_role, reason="Onboarding verified")
        log.info(
            "[roles] member=%s guild=%s action=apply rank=%r verified_was=%s "
            "removed=%s",
            member.id, guild.id, profile.rank_tier, had_verified, removed_names or "-",
        )
    else:
        # No rank resolved — grant Verified only, strip any stale rank role.
        to_remove = [r for r in member.roles if r.name in managed]
        removed_names = [r.name for r in to_remove]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Rank cleared")
        await member.add_roles(verified, reason="Onboarding verified (no rank)")
        log.info(
            "[roles] member=%s guild=%s action=apply rank=- verified_was=%s "
            "removed=%s",
            member.id, guild.id, had_verified, removed_names or "-",
        )


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
    embed.add_field(name="🪪 Discord", value=f"{member.mention} (`{member.id}`)", inline=True)
    embed.add_field(name="🎮 Tekken ID", value=f"`{tekken_id}`", inline=True)
    embed.add_field(name="🏆 Claimed rank", value=rank_tier, inline=True)
    rank_icon = media.rank_icon_url(rank_tier)
    if rank_icon is not None:
        embed.set_thumbnail(url=rank_icon)
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

        # If wavu had no display name for this player (usually because they
        # haven't played ranked), upgrade from ewgf before showing the embed.
        await _upgrade_display_name(profile)

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
    embed.add_field(name="🪪 Tekken ID", value=f"`{p.tekken_id}`", inline=False)
    embed.add_field(name="🥊 Main", value=p.main_char or "—", inline=True)
    embed.add_field(name="🏅 Rank", value=p.rank_tier or "—", inline=True)
    if p.rating_mu is not None:
        embed.add_field(name="📊 Rating (μ)", value=f"{p.rating_mu:.0f}", inline=True)
    # Character portrait (right side) when we know the main; rank tier icon
    # as the small author icon (left of name) when we know the rank.
    char_icon = media.character_icon_url(p.main_char)
    if char_icon is not None:
        embed.set_thumbnail(url=char_icon)
    rank_icon = media.rank_icon_url(p.rank_tier)
    if rank_icon is not None:
        embed.set_author(name=p.rank_tier or "", icon_url=rank_icon)
    embed.set_footer(text="Sources: wank.wavu.wiki + ewgf.gg")
    return embed


async def _profile_card_payload(
    p: wavu.PlayerProfile,
) -> tuple[discord.Embed, discord.File] | tuple[discord.Embed, None]:
    """Render the broadcast-style player card and pair it with a slim
    embed that hosts the title + tekken ID + rating-mu line. The image
    carries name / rank / character; the embed carries the metadata
    bits that don't fit cleanly inside the card.

    Returns (embed_with_attachment_image, file) on success or
    (text_only_embed, None) if rendering blew up — callers can splat
    the tuple into followup.send and the fallback is harmless.
    """
    embed = discord.Embed(title=p.display_name, color=discord.Color.blurple())
    embed.add_field(name="🪪 Tekken ID", value=f"`{p.tekken_id}`", inline=False)
    if p.rating_mu is not None:
        embed.add_field(
            name="📊 Rating (μ)", value=f"{p.rating_mu:.0f}", inline=True,
        )
    embed.set_footer(text="Sources: wank.wavu.wiki + ewgf.gg")
    try:
        card_buf = await tournament_render.render_player_card(
            display_name=p.display_name,
            rank_tier=p.rank_tier,
            main_char=p.main_char,
            tekken_id=p.tekken_id,
        )
    except Exception:
        log.exception("[profile-card] render failed for %s", p.tekken_id)
        # Degrade to the text-heavy embed so the user still sees their data.
        return _profile_embed(p), None
    file = discord.File(card_buf, filename="player-card.png")
    embed.set_image(url="attachment://player-card.png")
    return embed, file


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


class RankGroupSelectView(ErrorHandledView):
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


class ConfirmProfileView(ErrorHandledView):
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

        # Acknowledge on the original ephemeral message (clears the confirm
        # view), then send a follow-up with the broadcast-style player card
        # so the success state actually feels celebratory rather than just
        # a one-liner. edit_message + followup is the supported pattern for
        # adding fresh attachments alongside an interaction response.
        await interaction.response.edit_message(
            content=f"✅ Verified. Welcome, {self.profile.display_name}.",
            embed=None, view=None,
        )
        embed, file = await _profile_card_payload(self.profile)
        try:
            if file is not None:
                await interaction.followup.send(
                    embed=embed, file=file, ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=embed, ephemeral=True,
                )
        except discord.HTTPException:
            # Card follow-up is decoration; the verify itself already landed.
            pass

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
    log.info("[refresh/user] user=%s guild=%s", interaction.user.id,
             interaction.guild.id if interaction.guild else "-")
    row = await db.get_player_by_discord(interaction.user.id)
    if row is None:
        log.info("[refresh/user] user=%s result=not-linked", interaction.user.id)
        await interaction.response.send_message(
            "You're not linked yet. Click **Verify** first.",
            ephemeral=True, delete_after=10,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        profile = await wavu.lookup_player(row["tekken_id"], force_refresh=True)
    except (wavu.PlayerNotFound, wavu.WavuError) as e:
        log.warning("[refresh/user] user=%s tekken_id=%s wavu_err=%s",
                    interaction.user.id, row["tekken_id"], e)
        await interaction.followup.send(f"{e}", ephemeral=True)
        return

    await _upgrade_display_name(profile)
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
            embed, file = await _profile_card_payload(profile)
            kwargs: dict = {"content": "Updated.", "embed": embed, "ephemeral": True}
            if file is not None:
                kwargs["file"] = file
            await interaction.followup.send(**kwargs)
    elif stored_is_valid:
        # rank_tier == stored, so needs_pending is False; safe.
        await _save_and_apply(stored)
        embed, file = await _profile_card_payload(profile)
        kwargs = {
            "content": "Updated. *(Couldn't auto-detect rank — kept your existing one.)*",
            "embed": embed, "ephemeral": True,
        }
        if file is not None:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)
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
    await interaction.response.defer(ephemeral=True, thinking=True)
    embed, file = await _profile_card_payload(profile)
    if extra is not None:
        embed.add_field(name="Verification status", value=extra, inline=False)
    if file is not None:
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Pending verification UI (spec §5.3) — persistent View                        #
# --------------------------------------------------------------------------- #

class PendingVerificationView(ErrorHandledView):
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
# Auto-refresh — Interaction-free entry points                                 #
# --------------------------------------------------------------------------- #
#
# `_flow_refresh` above is the interactive version: it defers an Interaction,
# fetches fresh rank, updates DB + roles, and posts a followup. The functions
# below are the same core logic with the Interaction bits stripped, so the
# rank sweeper, on_member_join, /admin-resync-all, and /reset-server can all
# share a single source of truth for "restore / refresh this player".

async def restore_roles_from_db_cache(
    member: discord.Member, row,
) -> None:
    """Re-grant Verified + the player's cached rank role using ONLY DB values
    — no wavu/ewgf hit. Used when we just need to get someone's roles back
    quickly (rejoin, post-/reset-server, etc.). Fresh rank lookup is the
    rank sweeper's job.

    Pending-verification guard (spec §5.3): if the player has a live
    pending row, `_PendingSweeper._mark_one_stale` may have written their
    *claimed* rank back into `players.rank_tier` for profile-display
    purposes. Applying that rank would silently grant a role the
    organizers never confirmed — a high-rank claim + 72h of organizer
    inaction + a rejoin could net the user a Tekken Emperor role without
    anyone approving. So we drop to Verified-only when a pending row
    exists; the rank role can only be granted once the pending is
    explicitly resolved (Confirm button) and the row deleted.
    """
    pending = await db.get_pending_by_discord(member.id)
    if pending is not None:
        await _grant_verified_only(
            member, reason="Rejoin/resync — pending verification in flight",
        )
        return
    profile = wavu.PlayerProfile(
        tekken_id=row["tekken_id"],
        display_name=row["display_name"],
        main_char=row["main_char"],
        rating_mu=row["rating_mu"],
        rank_tier=row["rank_tier"],
    )
    await _apply_rank_and_verified(member, profile)


async def refresh_player_from_api(
    guild: discord.Guild,
    member: discord.Member,
    row,
    *,
    audit_source: str,
) -> dict:
    """Run the wavu → ewgf lookup, apply pending-verification rules, upsert
    the players row, and re-apply roles. Returns a result dict for logging.

    Status values:
      - "rank-changed": rank tier differs from stored; roles updated, audit posted
      - "pending":      new high-rank claim went to Pending Verification
      - "ok":           no change, roles still re-applied
      - "error":        wavu lookup or role edit failed; `reason` key explains
    """
    try:
        profile = await wavu.lookup_player(row["tekken_id"], force_refresh=True)
    except (wavu.PlayerNotFound, wavu.WavuError) as e:
        return {"status": "error", "reason": f"wavu: {e}"}

    await _upgrade_display_name(profile)
    rank_name = await _resolve_rank(row["tekken_id"], force_refresh=True)

    stored = row["rank_tier"]
    stored_is_valid = stored in wavu.ALL_RANK_NAMES
    # If the auto-detect fails but we have a valid stored rank, keep it —
    # same conservative fallback as _flow_refresh's `elif stored_is_valid`.
    new_tier = rank_name if rank_name is not None else (stored if stored_is_valid else None)

    # Spec §5.3: only *new* high-rank claims trigger Pending Verification.
    needs_pending = (
        new_tier is not None
        and new_tier != stored
        and _requires_pending(new_tier)
    )

    if needs_pending:
        # Post the pending request BEFORE updating the DB. If the audit
        # channel is gone or Discord rejects the post, we want the old
        # rank preserved in the DB — not blanked out. A retry on the
        # next sweep will detect the same high-rank claim and try again
        # against the old stored rank, which is the right behaviour.
        await _start_pending_verification(
            guild=guild, member=member,
            tekken_id=profile.tekken_id,
            rank_tier=new_tier,  # type: ignore[arg-type]
            rank_source=audit_source,
        )
        profile.rank_tier = None  # withheld until organizer confirm
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
        return {"status": "pending", "rank": new_tier}

    # Non-pending path: DB update + role apply happen together; a
    # Forbidden on the role edit still leaves the DB in a sensible
    # state (the rank is correct, the grant just didn't land).
    profile.rank_tier = new_tier
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

    try:
        await _apply_rank_and_verified(member, profile)
    except discord.Forbidden:
        return {"status": "error", "reason": "role hierarchy"}

    if new_tier is not None and new_tier != stored:
        await audit.post_event(
            guild,
            title="Rank changed",
            color=discord.Color.gold(),
            fields=[
                ("Discord", f"{member.mention} (`{member.id}`)", True),
                ("Tekken ID", f"`{profile.tekken_id}`", True),
                ("From", stored or "—", True),
                ("To", new_tier, True),
                ("Trigger", audit_source, True),
            ],
        )
        return {"status": "rank-changed", "from": stored, "to": new_tier}
    return {"status": "ok"}


async def resync_all_players(
    guild: discord.Guild,
    *,
    api_refresh: bool,
    force: bool = False,
    skip_if_synced_within: timedelta | None = None,
    audit_source: str = "auto-resync",
) -> dict:
    """Walk every linked player row and re-apply their roles.

    api_refresh=False — restore cached Verified + rank role only, no API hit.
                        Cheap enough to run over hundreds of members.
    api_refresh=True  — re-run wavu/ewgf lookup per player; picks up rank
                        changes and promotes to Pending for high-rank jumps.

    force=True ignores skip_if_synced_within. Used by /admin-resync-all."""
    rows = await db.list_all_players()
    log.info(
        "[resync] guild=%s starting rows=%d api_refresh=%s force=%s "
        "skip_if_synced_within=%ss source=%r",
        guild.id, len(rows), api_refresh, force,
        int(skip_if_synced_within.total_seconds()) if skip_if_synced_within else "-",
        audit_source,
    )
    results = {
        "total": 0,
        "restored": 0,
        "rank_changed": 0,
        "pending": 0,
        "skipped_not_in_guild": 0,
        "skipped_recent": 0,
        "errors": 0,
    }
    now = datetime.now(timezone.utc)

    for row in rows:
        results["total"] += 1
        member = guild.get_member(row["discord_id"])
        if member is None:
            results["skipped_not_in_guild"] += 1
            continue

        if (api_refresh and not force and skip_if_synced_within is not None
                and row["last_synced"]):
            try:
                last = datetime.fromisoformat(row["last_synced"])
            except ValueError:
                last = None
            if last is not None and (now - last) < skip_if_synced_within:
                # Still re-grant Verified + cached rank role on the cheap path
                # so a user who lost their role between syncs still gets it
                # back without waiting for their per-player sync window.
                try:
                    await restore_roles_from_db_cache(member, row)
                    results["skipped_recent"] += 1
                except discord.Forbidden:
                    results["errors"] += 1
                except Exception:
                    log.exception("resync: cached-restore failed for %s", member.id)
                    results["errors"] += 1
                await asyncio.sleep(_RESYNC_PER_MEMBER_DELAY)
                continue

        try:
            if api_refresh:
                r = await refresh_player_from_api(
                    guild, member, row, audit_source=audit_source,
                )
                status = r.get("status")
                if status == "rank-changed":
                    results["rank_changed"] += 1
                elif status == "pending":
                    results["pending"] += 1
                elif status == "error":
                    log.warning("resync: API refresh error for %s: %s",
                                member.id, r.get("reason"))
                    results["errors"] += 1
                else:
                    results["restored"] += 1
            else:
                await restore_roles_from_db_cache(member, row)
                results["restored"] += 1
        except discord.Forbidden:
            # Almost always role hierarchy — the bot's own role is below a
            # role it's trying to manage. Log and keep going.
            results["errors"] += 1
        except Exception:
            log.exception("resync: per-player failure for %s", member.id)
            results["errors"] += 1

        await asyncio.sleep(_RESYNC_PER_MEMBER_DELAY)

    log.info(
        "[resync] guild=%s done total=%d restored=%d rank_changed=%d "
        "pending=%d skipped_recent=%d skipped_not_in_guild=%d errors=%d "
        "source=%r",
        guild.id, results["total"], results["restored"],
        results["rank_changed"], results["pending"],
        results["skipped_recent"], results["skipped_not_in_guild"],
        results["errors"], audit_source,
    )
    return results


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


# --------------------------------------------------------------------------- #
# Rank auto-refresh sweeper                                                    #
# --------------------------------------------------------------------------- #
#
# Jay Jay's friend didn't realise he had to click Refresh in the Player Hub
# to pick up rank changes. This sweeper runs the same refresh flow
# automatically for every linked player, on a cadence. Per-player API
# calls are skipped if the row was synced within RANK_SWEEP_SKIP_IF_SYNCED_WITHIN
# so we don't hammer wavu/ewgf on every tick.

class _RankSweeper:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="rank-sweeper")

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        # Offset from the pending sweeper's first run so the two aren't
        # contending for the API-call budget on a fresh boot.
        await asyncio.sleep(RANK_SWEEP_STARTUP_DELAY.total_seconds())
        while True:
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Rank sweeper iteration failed")
            await asyncio.sleep(RANK_SWEEP_INTERVAL.total_seconds())

    async def _sweep_once(self) -> None:
        log.info(
            "[rank-sweeper] tick guilds=%d interval=%ss skip_if_synced_within=%ss",
            len(self.bot.guilds),
            int(RANK_SWEEP_INTERVAL.total_seconds()),
            int(RANK_SWEEP_SKIP_IF_SYNCED_WITHIN.total_seconds()),
        )
        for guild in self.bot.guilds:
            # resync_all_players emits its own [resync] start/done lines,
            # so no need to double-log the per-guild summary here.
            await resync_all_players(
                guild,
                api_refresh=True,
                force=False,
                skip_if_synced_within=RANK_SWEEP_SKIP_IF_SYNCED_WITHIN,
                audit_source="auto-refresh (sweeper)",
            )


class _ConfirmUnlinkView(ErrorHandledView):
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


class PlayerHubView(ErrorHandledView):
    """Persistent unified panel. Custom IDs must stay stable across restarts."""

    def __init__(self, bot: commands.Bot | None = None):
        super().__init__(timeout=None)
        # Bot is None when the View is reconstructed on startup from custom_id;
        # we resolve it from interaction.client inside callbacks.
        self._bot = bot

    def _resolve_bot(self, interaction: discord.Interaction) -> commands.Bot:
        return self._bot or interaction.client  # type: ignore[return-value]

    # Row 0 is deliberately ONE button — Verify alone, primary (blurple).
    # This is the mandatory-verification onboarding gate: new arrivals
    # see only this channel, and the only action they can take is the
    # one button that sits visually by itself above the rest. Splitting
    # the post-verify actions onto rows 1–2 trades a little vertical
    # space for a huge clarity win for first-time visitors.
    @discord.ui.button(label="▶  Verify — Click to enter the server",
                       style=discord.ButtonStyle.primary,
                       custom_id="hub:verify", row=0)
    async def verify(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_verify_start(interaction, self._resolve_bot(interaction))

    @discord.ui.button(label="Refresh Rank",
                       style=discord.ButtonStyle.success,
                       custom_id="hub:refresh", row=1)
    async def refresh(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_refresh(interaction, self._resolve_bot(interaction))

    @discord.ui.button(label="My Profile",
                       style=discord.ButtonStyle.secondary,
                       custom_id="hub:profile", row=1)
    async def profile(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_profile(interaction)

    @discord.ui.button(label="Set Rank Manually",
                       style=discord.ButtonStyle.secondary,
                       custom_id="hub:set_rank", row=1)
    async def set_rank(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_set_rank(interaction, self._resolve_bot(interaction))

    @discord.ui.button(label="Unlink Me",
                       style=discord.ButtonStyle.danger,
                       custom_id="hub:unlink", row=2)
    async def unlink(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_unlink(interaction)


PLAYER_HUB_BANNER_FILENAME = "player_hub_banner.png"

# Body text baked into the Player Hub banner PNG. Kept plain-ASCII + a
# couple of safe unicode bits (bullet, em-dash, arrow) so the body font
# renders every glyph — colour-emojis aren't in DejaVu and would tofu.
# The button labels carry their own emoji in Discord's UI, so players
# still see them where it matters.
#
# Copy is deliberately blunt: this channel is the ONLY visible channel
# to unverified members, so the body has to do the work of (a) telling
# them what to do and (b) condensing the rules they'd otherwise read
# in #📜-rules (which is now gated behind Verified).
_PLAYER_HUB_BODY = (
    "## One click to enter\n"
    "This is the only way into the server. Click VERIFY below, paste your\n"
    "Tekken ID, and the rest of the server unlocks instantly.\n"
    "(Find your ID at Main Menu > Community > My Profile.)\n"
    "\n"
    "## By clicking Verify you agree to\n"
    "• Be kind. No harassment, slurs, or doxxing.\n"
    "• Hype and tilt are fine. Personal attacks are not.\n"
    "• Don't cheat. Don't impersonate. Mods' call is final.\n"
    "\n"
    "## Already verified?\n"
    "Use the buttons below to refresh your rank, set it manually,\n"
    "check your profile, or unlink."
)


# Text block shown in the embed next to the banner image, so users who
# read text faster than pictures still land on the same instruction.
# The banner does the visual work, the description does the scannable
# work — redundant on purpose so no-one misses the Verify call to action.
_PLAYER_HUB_DESCRIPTION = (
    "## ▶  Step 1 — Click **Verify**\n"
    "It's the only way into the rest of the server. One click, paste your "
    "Tekken ID, done.\n\n"
    "By clicking Verify you agree to the server rules (summary on the "
    "banner above — full rules unlock in **#📜-rules** the moment you're in)."
)


def _player_hub_embed() -> discord.Embed:
    """Container embed for the Player Hub banner attachment. The banner
    PNG carries the full body (so the image alone is self-sufficient
    when Discord lazy-loads embeds), and the embed description mirrors
    the Verify CTA in text — redundant on purpose so skimmers and
    deep-readers both land on the same instruction."""
    embed = discord.Embed(
        description=_PLAYER_HUB_DESCRIPTION,
        color=discord.Color.red(),
    )
    embed.set_image(url=f"attachment://{PLAYER_HUB_BANNER_FILENAME}")
    embed.set_footer(
        text="One Tekken ID per Discord account  •  Admins can override  "
             "•  Verify is the only entry to the server",
    )
    return embed


async def _delete_player_hub_pin_notification(
    channel: discord.abc.Messageable,
) -> None:
    """Delete the transient 'X pinned a message' system post that
    appears right after we pin the Player Hub. Silent on failure."""
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        async for m in channel.history(limit=5):
            if m.type == discord.MessageType.pins_add:
                await m.delete()
                return
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _player_hub_banner_file() -> discord.File:
    """Render the Player Hub hero image with the onboarding instructions
    baked into the body band — same visual family as the channel
    banners provisioned by /setup-server."""
    buf = await tournament_render.render_banner(
        kicker="Your Identity",
        title="Player Hub",
        subtitle="Verify · profile · rank · unlink",
        body=_PLAYER_HUB_BODY,
    )
    return discord.File(buf, filename=PLAYER_HUB_BANNER_FILENAME)


# --------------------------------------------------------------------------- #
# Cog                                                                          #
# --------------------------------------------------------------------------- #

class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sweeper = _PendingSweeper(bot)
        self._rank_sweeper = _RankSweeper(bot)

    async def cog_load(self) -> None:
        # Persistent views: custom_ids let Discord route button clicks back to
        # these View instances even after a bot restart.
        self.bot.add_view(PlayerHubView(self.bot))
        self.bot.add_view(PendingVerificationView())
        self._sweeper.start()
        self._rank_sweeper.start()

    async def cog_unload(self) -> None:
        self._sweeper.stop()
        self._rank_sweeper.stop()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Auto-restore roles for returning members + DM the verify nudge.

        If the member was previously linked (row in `players`), re-grant their
        Verified + cached rank role immediately. This covers rejoin-after-leave
        and the post-/reset-server case where the Verified role was deleted
        and needs to come back without asking the user to click anything.
        A fresh rank lookup is left to the rank sweeper so on_member_join
        doesn't block on a wavu/ewgf round-trip.
        """
        if member.bot:
            log.info("[join] member=%s guild=%s bot=yes ignored",
                     member.id, member.guild.id)
            return

        row = await db.get_player_by_discord(member.id)
        linked = row is not None
        log.info(
            "[join] member=%s name=%r guild=%s linked=%s tekken_id=%s "
            "stored_rank=%r",
            member.id, str(member), member.guild.id,
            "yes" if linked else "no",
            row["tekken_id"] if linked else "-",
            row["rank_tier"] if linked else None,
        )
        if linked:
            try:
                await restore_roles_from_db_cache(member, row)
                log.info("[join] member=%s action=restore_cache result=ok", member.id)
            except discord.Forbidden:
                log.warning(
                    "[join] member=%s action=restore_cache result=forbidden "
                    "(bot role below target — drag bot role up)",
                    member.id,
                )
            except Exception:
                log.exception("[join] member=%s action=restore_cache result=exception",
                              member.id)

        try:
            await self._send_join_dm(member, already_linked=linked)
            log.info("[join] member=%s action=send_dm already_linked=%s",
                     member.id, "yes" if linked else "no")
        except Exception:
            log.exception("[join] member=%s action=send_dm result=exception", member.id)

    async def _send_join_dm(
        self, member: discord.Member, *, already_linked: bool,
    ) -> None:
        """Friendly DM pointing new (or returning) members at #🎴-player-hub.
        Silently ignored if the member blocks DMs from the server."""
        hub_channel = channel_util.find_text_channel(member.guild, "player-hub")
        # Channel mentions (<#id>) only hyperlink inside a guild — in DMs
        # they render as raw text. Use a discord.com deep-link instead so
        # clicking in the DM jumps straight to #player-hub.
        if hub_channel is not None:
            hub_url = (
                f"https://discord.com/channels/{member.guild.id}/{hub_channel.id}"
            )
            hub_jump = f"[#🎴-player-hub]({hub_url})"
        else:
            hub_jump = "**#🎴-player-hub**"

        if already_linked:
            description = (
                f"Welcome back to **{member.guild.name}**, {member.mention}!\n\n"
                "Your Tekken link is already on file — I've re-applied your "
                f"Verified role and rank so you can jump straight back in.\n\n"
                f"If anything looks off, pop into {hub_jump} and click "
                "**Refresh Rank**."
            )
        else:
            description = (
                f"Welcome to **{member.guild.name}**, {member.mention}!\n\n"
                f"**{hub_jump} is the only channel you can see right now** "
                "— and that's on purpose. Click the **▶  Verify** button "
                "there, paste your Tekken ID, and the whole server unlocks "
                "instantly.\n\n"
                "No verify, no access — no exceptions. One click is all "
                "it takes, and we'll auto-assign your current rank role "
                "from your recent matches."
            )

        embed = discord.Embed(
            title="🎴 Welcome to the server",
            description=description,
            color=discord.Color.green(),
        )
        embed.set_footer(text="Ehrgeiz Godhand • auto-sent on join")

        try:
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            # User has DMs disabled, or Discord blocked us. No retry —
            # the #👋-welcome channel's pinned banner is the fallback.
            pass

    @app_commands.command(
        name="admin-resync-all",
        description="[Admin] Re-check every linked player's rank + re-grant Verified if missing.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def admin_resync_all(self, interaction: discord.Interaction):
        """Manual trigger for the same sweep the _RankSweeper runs.
        Bypasses the skip-if-recent threshold (force=True) so an admin
        can demand a fresh pass right now — e.g. after a bulk role edit
        or when chasing a suspected drift bug."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        results = await resync_all_players(
            guild,
            api_refresh=True,
            force=True,
            audit_source="admin-resync-all",
        )
        embed = discord.Embed(
            title="🔄 Resync complete",
            color=(discord.Color.green() if results["errors"] == 0
                   else discord.Color.orange()),
            description=(
                f"Checked **{results['total']}** linked player(s).\n\n"
                f"• ✅ **{results['restored']}** roles re-applied\n"
                f"• 📈 **{results['rank_changed']}** rank changes detected\n"
                f"• ⏳ **{results['pending']}** new pending verification(s)\n"
                f"• 👋 **{results['skipped_not_in_guild']}** no longer in this server\n"
                f"• ⚠ **{results['errors']}** error(s) — see bot console"
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
        banner = await _player_hub_banner_file()
        channel = interaction.channel
        msg = await channel.send(
            embed=_player_hub_embed(),
            view=PlayerHubView(self.bot),
            file=banner,
        )
        # Pin + tidy the "X pinned a message" system notification so the
        # hub stays reachable at the top of #player-hub even when chat
        # pushes it up.
        try:
            await msg.pin()
            await _delete_player_hub_pin_notification(channel)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("player hub pin failed: %s", e)
        await db.set_panel(guild.id, PANEL_KIND_PLAYER_HUB, channel.id, msg.id)
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

        await _upgrade_display_name(profile)

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
        embed, file = await _profile_card_payload(profile)
        kwargs: dict = {
            "content": f"Linked {member.mention}.",
            "embed": embed, "ephemeral": True,
        }
        if file is not None:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)

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

    @app_commands.command(
        name="admin-pending-resolve",
        description="Admin: confirm or reject a player's pending high-rank claim "
                    "without using the audit-log buttons.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        member="The user whose pending claim you want to resolve",
        action="Confirm grants the rank role; Reject leaves them as Verified-only",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Confirm — grant the claimed rank", value="confirm"),
        app_commands.Choice(name="Reject — strip the pending claim",   value="reject"),
    ])
    async def admin_pending_resolve(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        action: app_commands.Choice[str],
    ):
        """Escape hatch for when the audit-log Confirm/Reject message is gone
        (deleted, channel purged, etc.) but the pending row is still live.
        Mirrors the resolution path the buttons take."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return

        pending = await db.get_pending_by_discord(member.id)
        if pending is None:
            await interaction.response.send_message(
                f"{member.mention} has no pending verification on file.",
                ephemeral=True, delete_after=10,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        granted = False
        if action.value == "confirm":
            player_row = await db.get_player_by_discord(member.id)
            if player_row is None:
                await interaction.followup.send(
                    f"Pending row exists for {member.mention} but no players "
                    "row — clear it manually with `/admin-unlink` then "
                    "re-run `/admin-link`.",
                    ephemeral=True,
                )
                return
            await db.upsert_player(
                discord_id=member.id,
                tekken_id=player_row["tekken_id"],
                display_name=player_row["display_name"],
                main_char=player_row["main_char"],
                rating_mu=player_row["rating_mu"],
                rank_tier=pending["rank_tier"],
                linked_by=player_row["linked_by"],
                now_iso=_now_iso(),
            )
            confirmed_profile = wavu.PlayerProfile(
                tekken_id=pending["tekken_id"],
                display_name=player_row["display_name"],
                main_char=player_row["main_char"],
                rating_mu=player_row["rating_mu"],
                rank_tier=pending["rank_tier"],
            )
            try:
                await _apply_rank_and_verified(member, confirmed_profile)
                granted = True
            except discord.Forbidden:
                granted = False

        # Drop the pending row in both branches — Reject leaves the player
        # with no rank role; Confirm's grant is best-effort.
        await db.delete_pending_verification(member.id)

        # Strip the buttons from the audit message if it still exists.
        if pending["channel_id"] and pending["message_id"]:
            channel = guild.get_channel(pending["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(pending["message_id"])
                    await msg.edit(view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        if action.value == "confirm":
            summary = (
                f"✅ Confirmed **{pending['rank_tier']}** for {member.mention}"
                + ("." if granted else " — DB updated, role grant failed (hierarchy?).")
            )
        else:
            summary = (
                f"❌ Rejected the **{pending['rank_tier']}** claim for "
                f"{member.mention}. They keep Verified, no rank role."
            )
        await interaction.followup.send(summary, ephemeral=True)

        await audit.post_event(
            guild,
            title=f"Pending verification {action.value.upper()} (admin)",
            color=(
                discord.Color.green() if (action.value == "confirm" and granted)
                else discord.Color.orange() if action.value == "confirm"
                else discord.Color.dark_red()
            ),
            fields=[
                ("Target", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{interaction.user.mention}", True),
                ("Claim", pending["rank_tier"], True),
                ("Source", pending["rank_source"], True),
                ("Role granted", "yes" if granted else "no", True),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
