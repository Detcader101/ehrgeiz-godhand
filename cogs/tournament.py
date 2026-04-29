"""Tournament cog (spec §8, issue #1). Slice 1 — signup flow + state.

Swiss format tournaments with rank-weighted seeding, run entirely by the
bot. This slice handles the entry points only:

  - /tournament-create posts a signup embed to #tournaments.
  - Players click ⚔️ JOIN / Leave buttons (persistent view).
  - /tournament-start closes signups and flips state to IN_PROGRESS.
  - /tournament-cancel kills a tournament at any state.

Pairing, match reporting, auto-provisioned VCs, bracket-PNG rendering,
and the end-of-tournament archive flow are deferred to slices 2-5.
"""
from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import audit
import channel_util
import db
import media
import tournament_render
import wavu
from cogs.onboarding import VERIFIED_ROLE_NAME
import view_util
from view_util import ErrorHandledView

# Name → ordinal lookup for seeding. Built once at import time.
_RANK_ORDINAL_BY_NAME = {v: k for k, v in wavu.TEKKEN_RANKS.items()}

log = logging.getLogger(__name__)

ORGANIZER_ROLE_NAME = "Organizer"
SIGNUPS_CHANNEL_NAME = "tournaments"
MIN_PLAYERS_TO_START = 4

FORMAT_CHOICES = [
    app_commands.Choice(name="First to 2 (FT2 / Bo3)", value="FT2"),
    app_commands.Choice(name="First to 3 (FT3 / Bo5)", value="FT3"),
]

# ---- Test-mode fixtures -------------------------------------------------- #
# Fake Discord IDs live below 10_000 so they can't collide with real
# snowflake IDs (which are ~17-20 digits). Synthetic Tekken IDs all begin
# with 'TEST' so /tournament-dev-cleanup can find and wipe them.
DEV_FAKE_UID_MIN = 1000
DEV_FAKE_UID_MAX = 9999
DEV_FAKE_TEKKEN_PREFIX = "TEST"
DEV_FAKE_NAMES = [
    "Kazuya Clone", "Jin Replica", "Heihachi Bot", "Nina Stand-In",
    "Paul Decoy", "King Shadow", "Law Duplicate", "Bryan Copy",
    "Lars Proxy", "Steve Echo", "Reina Model", "Asuka Sim",
    "Dragunov Sub", "Xiaoyu Mirror", "Hwoarang Alt", "Yoshi Phantom",
    "Clive Puppet", "Devil Jin Lite", "Jack Prototype", "Lee Duplicate",
    "Feng Shadow", "Leroy Sub", "Azucena Clone", "Victor Proxy",
]
DEV_FAKE_CHARS = [
    "Kazuya", "Jin", "King", "Paul", "Law", "Yoshimitsu", "Hwoarang",
    "Lars", "Steve", "Xiaoyu", "Bryan", "Asuka", "Nina", "Dragunov",
    "Reina", "Clive", "Lee", "Feng", "Leroy", "Azucena", "Victor",
    "Alisa", "Jack-8", "Lili",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_organizer(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name == ORGANIZER_ROLE_NAME for r in member.roles)


def _is_verified(member: discord.Member) -> bool:
    return any(r.name == VERIFIED_ROLE_NAME for r in member.roles)


# --------------------------------------------------------------------------- #
# Signup embed + roster attachment                                             #
# --------------------------------------------------------------------------- #

ROSTER_FILENAME = "roster.png"


def _build_signup_embed(
    *,
    tournament_row,
    participant_rows,
    organizer_mention: str,
) -> discord.Embed:
    name = tournament_row["name"]
    fmt = tournament_row["match_format"]
    max_p = tournament_row["max_players"]
    state = tournament_row["state"]
    count = len(participant_rows)

    if state == "SIGNUPS_OPEN":
        color = discord.Color.red()
        status_line = "🟢 **SIGNUPS OPEN** — click ⚔️ JOIN below to enter."
    elif state == "IN_PROGRESS":
        color = discord.Color.dark_red()
        status_line = "🔴 **IN PROGRESS** — signups are closed."
    elif state == "COMPLETED":
        color = discord.Color.gold()
        status_line = "🏆 **COMPLETED** — see #tournament-history for the final bracket."
    else:  # CANCELLED
        color = discord.Color.dark_gray()
        status_line = "⚫ **CANCELLED.**"

    embed = discord.Embed(
        title=name,
        description=(
            f"{status_line}\n\n"
            "*Swiss format · rank-weighted seeding · match winners only.*"
        ),
        color=color,
    )

    embed.set_author(name="Ehrgeiz Swiss Tournament", icon_url=media.LOGO_URL)
    embed.set_thumbnail(url=media.LOGO_URL)

    slots = f"{count} / {max_p}" if max_p else f"{count}"
    embed.add_field(name="🎮 Format", value=f"**{fmt}**", inline=True)
    embed.add_field(name="👥 Contenders", value=f"**{slots}**", inline=True)
    embed.add_field(name="🎤 Organizer", value=organizer_mention, inline=True)

    if participant_rows:
        # Plain mention list — the roster PNG carries the visual detail
        # (rank icons, character icons, ranked name). Text is here so
        # mentions are clickable and Discord mobile renders names inline.
        lines = [
            f"**{r['rank_tier'] or 'Unranked'}** · <@{r['user_id']}>"
            for r in participant_rows
        ]
        roster = "\n".join(lines)
        # Discord field value cap is 1024 chars; split if we overflow.
        if len(roster) <= 1024:
            embed.add_field(
                name=f"🗡️ Entrants ({count})",
                value=roster, inline=False,
            )
        else:
            mid = len(lines) // 2
            embed.add_field(
                name=f"🗡️ Entrants ({count})",
                value="\n".join(lines[:mid]), inline=False,
            )
            embed.add_field(
                name="⋯",
                value="\n".join(lines[mid:]), inline=False,
            )
    else:
        embed.add_field(
            name="🗡️ Entrants",
            value="*No one yet — be the first to step up.*",
            inline=False,
        )

    # The PNG renders immediately below the embed as an attachment preview.
    embed.set_image(url=f"attachment://{ROSTER_FILENAME}")

    return embed


async def _enrich_participants(participant_rows) -> list[dict]:
    """Turn participant rows into renderer-friendly dicts. display_name
    and rank_tier are snapshotted at join time; main_char is looked up
    live from the players table (falls back to None if the user has
    since unlinked)."""
    enriched: list[dict] = []
    for r in participant_rows:
        player = await db.get_player_by_discord(r["user_id"])
        enriched.append({
            "user_id": r["user_id"],
            "display_name": r["display_name"],
            "rank_tier": r["rank_tier"],
            "main_char": player["main_char"] if player else None,
        })
    return enriched


async def _build_roster_file(participant_rows) -> discord.File:
    buf = await tournament_render.render_roster(
        await _enrich_participants(participant_rows)
    )
    return discord.File(buf, filename=ROSTER_FILENAME)


# --------------------------------------------------------------------------- #
# Round-1 pairings (rank-weighted Dutch Swiss)                                 #
# --------------------------------------------------------------------------- #

def _compute_round1_pairings(
    participants: list[dict],
) -> list[tuple[int | None, int | None, int | None]]:
    """Return a list of (player_a_id, player_b_id, winner_id) per match.

    Dutch Swiss round-1 with rank-weighted seeding: sort by rank ordinal
    descending (higher rank = better seed); split into top/bottom halves;
    pair top[0]-vs-bottom[0], top[1]-vs-bottom[1], etc. With 8 seeds this
    gives the classic 1v5, 2v6, 3v7, 4v8. Odd counts: the lowest seed
    gets a bye and is pre-winnered.

    Players missing a rank_tier fall to the bottom of the seeding order
    (treated as ordinal -1). user_id breaks ties deterministically.
    """
    def seed_key(p: dict) -> tuple[int, int]:
        ord_ = p.get("rank_ordinal")
        ord_ = ord_ if ord_ is not None else -1
        return (-ord_, p["user_id"])

    ordered = sorted(participants, key=seed_key)

    pairings: list[tuple[int | None, int | None, int | None]] = []
    bye: dict | None = None
    if len(ordered) % 2 == 1:
        bye = ordered.pop()

    half = len(ordered) // 2
    top = ordered[:half]
    bot = ordered[half:]
    for t, b in zip(top, bot):
        pairings.append((t["user_id"], b["user_id"], None))

    if bye is not None:
        pairings.append((bye["user_id"], None, bye["user_id"]))

    return pairings


async def _participants_for_pairing(tournament_id: int) -> list[dict]:
    rows = await db.list_participants(tournament_id)
    return [
        {
            "user_id": r["user_id"],
            "display_name": r["display_name"],
            "rank_tier": r["rank_tier"],
            "rank_ordinal": _RANK_ORDINAL_BY_NAME.get(r["rank_tier"])
                if r["rank_tier"] else None,
        }
        for r in rows
    ]


async def _player_snapshot_for_render(
    tournament_id: int, user_id: int | None,
) -> dict | None:
    if user_id is None:
        return None
    part = await db.get_participant(tournament_id, user_id)
    if part is None:
        return None
    player = await db.get_player_by_discord(user_id)
    return {
        "user_id": user_id,
        "display_name": part["display_name"],
        "rank_tier": part["rank_tier"],
        "main_char": player["main_char"] if player else None,
    }


async def _matches_for_render(tournament_id: int, round_number: int) -> list[dict]:
    rows = await db.list_matches_for_round(tournament_id, round_number)
    out: list[dict] = []
    for r in rows:
        a = await _player_snapshot_for_render(tournament_id, r["player_a_id"])
        b = await _player_snapshot_for_render(tournament_id, r["player_b_id"])
        out.append({
            "match_number": r["match_number"],
            "player_a": a,
            "player_b": b,
            "is_bye": r["player_b_id"] is None,
            "winner_id": r["winner_id"],
        })
    return out


async def _build_round_bracket_file(
    tournament_name: str, tournament_id: int, round_number: int,
) -> discord.File:
    matches = await _matches_for_render(tournament_id, round_number)
    buf = await tournament_render.render_bracket(
        tournament_name=tournament_name,
        round_number=round_number,
        matches=matches,
    )
    return discord.File(buf, filename=f"round{round_number}.png")


# --------------------------------------------------------------------------- #
# Persistent signup view (Join / Leave)                                        #
# --------------------------------------------------------------------------- #

class SignupView(ErrorHandledView):
    """Persistent Join/Leave view. We resolve the tournament from the
    message_id on each click (via signup_message_id) rather than binding
    it to the view instance — that way a single registered SignupView
    services every tournament signup message in every guild."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="JOIN", emoji="⚔️",
        style=discord.ButtonStyle.success,
        custom_id="tourney:join",
    )
    async def join(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_join(interaction)

    @discord.ui.button(
        label="Leave",
        style=discord.ButtonStyle.secondary,
        custom_id="tourney:leave",
    )
    async def leave(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_leave(interaction)


async def _flow_join(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    msg = interaction.message
    if msg is None:
        await interaction.followup.send(
            "Couldn't resolve this tournament.", ephemeral=True)
        return

    t = await db.get_tournament_by_signup_message(msg.id)
    if t is None:
        await interaction.followup.send(
            "This signup is no longer tracked by the bot.", ephemeral=True)
        return

    if t["state"] != "SIGNUPS_OPEN":
        await interaction.followup.send(
            f"Signups are closed (state: **{t['state']}**).", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.followup.send("Server-only.", ephemeral=True)
        return

    if not _is_verified(member):
        await interaction.followup.send(
            f"🔒 You need the **{VERIFIED_ROLE_NAME}** role to enter tournaments. "
            "Head to **#player-hub**, click **Verify**, and link your Tekken ID — "
            "then come back and click ⚔️ JOIN.",
            ephemeral=True)
        return

    if t["max_players"]:
        current = await db.count_participants(t["id"])
        if current >= t["max_players"]:
            await interaction.followup.send(
                f"Tournament is full ({current}/{t['max_players']}).",
                ephemeral=True)
            return

    # Snapshot display_name + rank_tier so the bracket stays stable even
    # if the player later unlinks or re-ranks. Falls back to member-level
    # defaults if the players row is somehow missing (shouldn't happen
    # given the verified check above, but belt-and-braces).
    player = await db.get_player_by_discord(member.id)
    rank_tier = player["rank_tier"] if player else None
    display_name = player["display_name"] if player else member.display_name

    inserted = await db.add_participant(
        tournament_id=t["id"],
        user_id=member.id,
        display_name=display_name,
        rank_tier=rank_tier,
        now_iso=_now_iso(),
    )
    if not inserted:
        await interaction.followup.send(
            "You're already signed up for this one.", ephemeral=True)
        return

    await _refresh_signup_message(interaction.client, t["id"])

    await interaction.followup.send(
        f"✅ You're in **{t['name']}**. Good luck.", ephemeral=True)


async def _flow_leave(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    msg = interaction.message
    if msg is None:
        await interaction.followup.send(
            "Couldn't resolve this tournament.", ephemeral=True)
        return

    t = await db.get_tournament_by_signup_message(msg.id)
    if t is None:
        await interaction.followup.send(
            "This signup is no longer tracked by the bot.", ephemeral=True)
        return

    if t["state"] != "SIGNUPS_OPEN":
        await interaction.followup.send(
            "You can't leave once the tournament has started. Speak to the "
            "organizer if you need to drop.", ephemeral=True)
        return

    removed = await db.remove_participant(t["id"], interaction.user.id)
    if not removed:
        await interaction.followup.send(
            "You weren't in this tournament.", ephemeral=True)
        return

    await _refresh_signup_message(interaction.client, t["id"])

    await interaction.followup.send(
        f"👋 Left **{t['name']}**.", ephemeral=True)


async def _refresh_signup_message(
    client: discord.Client, tournament_id: int,
) -> None:
    """Re-render embed + roster PNG and edit the signup message in place.
    Strips the view when state is no longer SIGNUPS_OPEN so closed
    tournaments can't be joined."""
    t = await db.get_tournament(tournament_id)
    if t is None or t["signup_channel_id"] is None or t["signup_message_id"] is None:
        return
    participants = await db.list_participants(tournament_id)

    embed = _build_signup_embed(
        tournament_row=t,
        participant_rows=participants,
        organizer_mention=f"<@{t['organizer_id']}>",
    )
    view = SignupView() if t["state"] == "SIGNUPS_OPEN" else None
    roster_file = await _build_roster_file(participants)

    try:
        channel = client.get_channel(t["signup_channel_id"])
        if channel is None:
            return
        msg = await channel.fetch_message(t["signup_message_id"])
        # attachments= replaces the existing file; the embed's
        # attachment:// URL resolves against the fresh one.
        await msg.edit(embed=embed, view=view, attachments=[roster_file])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("refresh_signup_message failed for tournament %s: %s",
                    tournament_id, e)


async def _delete_pin_notification(channel: discord.TextChannel) -> None:
    """After pinning, Discord posts a transient 'X pinned a message'
    system message in the channel. Nuke it so pinning doesn't feel like
    spam — the pin itself is preserved."""
    try:
        async for m in channel.history(limit=5):
            if m.type == discord.MessageType.pins_add:
                await m.delete()
                return
    except (discord.Forbidden, discord.HTTPException) as e:
        log.debug("couldn't tidy pin notification in %s: %s", channel.name, e)


async def _unpin_signup(client: discord.Client, t) -> None:
    """Best-effort unpin when a tournament ends."""
    if t["signup_channel_id"] is None or t["signup_message_id"] is None:
        return
    try:
        channel = client.get_channel(t["signup_channel_id"])
        if channel is None:
            return
        msg = await channel.fetch_message(t["signup_message_id"])
        if msg.pinned:
            await msg.unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.debug("unpin failed for tournament %s: %s", t["id"], e)


async def _announce_state_change(
    client: discord.Client, t, kind: str,
    attachment: discord.File | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Post a separate, non-editable message in the signup channel that
    mentions every participant — this is the notification vehicle,
    separate from the persistent panel. kind is 'started' or 'cancelled'.
    Attachment + view are both optional; view is how the round-start
    announcement gets its Report-a-Win button."""
    if t["signup_channel_id"] is None:
        return
    channel = client.get_channel(t["signup_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    participants = await db.list_participants(t["id"])
    mentions = " ".join(f"<@{p['user_id']}>" for p in participants) or "*(no entrants)*"

    if kind == "started":
        headline = f"⚔️ **{t['name']} — BRACKET LIVE!**"
        body = (
            f"{t['match_format']} · Swiss · rank-weighted seeding.\n"
            "Round 1 pairings below. Play your match, then hit "
            "**⚔️ Report a Win** — the loser confirms, disputes route to "
            "the organizer."
        )
    else:
        headline = f"❌ **{t['name']} — CANCELLED.**"
        body = "This tournament has been called off. Apologies for the shuffle."

    kwargs: dict = {"allowed_mentions": discord.AllowedMentions(users=True)}
    if attachment is not None:
        kwargs["file"] = attachment
    if view is not None:
        kwargs["view"] = view
    try:
        await channel.send(
            f"{headline}\n{body}\n\n{mentions}", **kwargs,
        )
    except discord.HTTPException as e:
        log.warning("failed to post %s announcement for tournament %s: %s",
                    kind, t["id"], e)


# --------------------------------------------------------------------------- #
# Cog                                                                          #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# #tournaments channel panel (pinned by /setup-server)                         #
# --------------------------------------------------------------------------- #

class TournamentsPanelView(ErrorHandledView):
    """Persistent buttons attached to the #tournaments banner.

      - Active Tournaments (anyone): lists live tournaments ephemerally.
      - Create Tournament FT3 (Organizer only): opens a 1-field modal
        that creates an uncapped FT3 tournament. For full control
        (FT2, max-player cap), use /tournament-create.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Active Tournaments", emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="tourney-panel:list",
    )
    async def active(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _flow_active_list(interaction)

    @discord.ui.button(
        label="Create Tournament (FT3)", emoji="🏆",
        style=discord.ButtonStyle.success,
        custom_id="tourney-panel:create",
    )
    async def create(self, interaction: discord.Interaction, _b: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        if not _is_organizer(member):
            await interaction.response.send_message(
                f"Need the **{ORGANIZER_ROLE_NAME}** role (or Administrator) "
                "to create tournaments.",
                ephemeral=True, delete_after=12)
            return
        await interaction.response.send_modal(CreateTournamentModal())


class CreateTournamentModal(discord.ui.Modal, title="Create Swiss Tournament"):
    """Single-field quick-start modal — defaults to FT3, no cap. Power
    users go through /tournament-create for the full option set."""

    tournament_name: discord.ui.TextInput = discord.ui.TextInput(
        label="Tournament name",
        placeholder="e.g. Friday Night Gauntlet",
        min_length=1, max_length=60,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return

        name_value = str(self.tournament_name).strip()

        existing = await db.get_active_tournament_by_name(guild.id, name_value)
        if existing is not None:
            await interaction.response.send_message(
                f"There's already a live tournament called **{existing['name']}** "
                f"({existing['state']}). Pick a different name.",
                ephemeral=True, delete_after=15)
            return

        signup_channel = channel_util.find_text_channel(
            guild, SIGNUPS_CHANNEL_NAME)
        if signup_channel is None:
            await interaction.response.send_message(
                f"Couldn't find a `#{SIGNUPS_CHANNEL_NAME}` channel. "
                "Run `/setup-server` first.",
                ephemeral=True, delete_after=15)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        tournament_id = await db.create_tournament(
            guild_id=guild.id,
            organizer_id=member.id,
            name=name_value,
            match_format="FT3",
            max_players=None,
            now_iso=_now_iso(),
        )

        t = await db.get_tournament(tournament_id)
        embed = _build_signup_embed(
            tournament_row=t,
            participant_rows=[],
            organizer_mention=member.mention,
        )
        roster_file = await _build_roster_file([])

        try:
            posted = await signup_channel.send(
                embed=embed, view=SignupView(), file=roster_file,
            )
        except discord.HTTPException as e:
            await db.update_tournament_state(tournament_id, "CANCELLED", _now_iso())
            await interaction.followup.send(
                f"Failed to post signup embed: {e}.", ephemeral=True)
            return

        await db.set_tournament_signup_message(
            tournament_id, signup_channel.id, posted.id)

        try:
            await posted.pin()
            await _delete_pin_notification(signup_channel)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("failed to pin signup panel (modal path) for %s: %s",
                        tournament_id, e)

        await interaction.followup.send(
            f"✅ Created **{name_value}** (FT3, no cap) in {signup_channel.mention}. "
            f"Run `/tournament-start name:{name_value}` when ready.",
            ephemeral=True,
        )


async def _flow_active_list(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return
    rows = await db.list_tournaments(
        guild.id, states=("SIGNUPS_OPEN", "IN_PROGRESS"),
    )
    if not rows:
        await interaction.response.send_message(
            "No active tournaments. An organizer can start one with the "
            "**Create Tournament** button or `/tournament-create`.",
            ephemeral=True, delete_after=15,
        )
        return

    lines: list[str] = []
    for r in rows:
        count = await db.count_participants(r["id"])
        state_icon = "🟢" if r["state"] == "SIGNUPS_OPEN" else "🔴"
        cap = f"/{r['max_players']}" if r["max_players"] else ""
        lines.append(
            f"{state_icon} **{r['name']}** · {r['match_format']} · "
            f"{count}{cap} players · *{r['state']}*"
        )

    await interaction.response.send_message(
        "**Active tournaments**\n" + "\n".join(lines),
        ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# Cog                                                                          #
# --------------------------------------------------------------------------- #

class Tournament(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # One SignupView serves every signup message in every guild — Discord
        # routes clicks by custom_id, and the callbacks resolve the tournament
        # from interaction.message.id.
        self.bot.add_view(SignupView())
        self.bot.add_view(TournamentsPanelView())
        # Match-reporting persistent views (slice 2).
        self.bot.add_view(ReportWinView())
        self.bot.add_view(MatchReportPublicView())
        self.bot.add_view(DisputeResolveView())

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        await view_util.handle_app_command_error(interaction, error, log)

    @app_commands.command(
        name="tournament-create",
        description="Create a new Swiss tournament with an open signup panel.",
    )
    @app_commands.describe(
        name="Tournament name (shown on bracket and announcements)",
        match_format="FT2 or FT3 — display metadata only, bot tracks match winners",
        max_players="Cap on signups (leave blank for no cap)",
    )
    @app_commands.choices(match_format=FORMAT_CHOICES)
    async def tournament_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
        match_format: app_commands.Choice[str],
        max_players: app_commands.Range[int, 4, 256] | None = None,
    ):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return

        if not _is_organizer(member):
            await interaction.response.send_message(
                f"You need the **{ORGANIZER_ROLE_NAME}** role (or Administrator) "
                "to create tournaments.",
                ephemeral=True, delete_after=12)
            return

        existing = await db.get_active_tournament_by_name(guild.id, name)
        if existing is not None:
            await interaction.response.send_message(
                f"There's already a live tournament called **{existing['name']}** "
                f"(state: {existing['state']}). Pick a different name or finish "
                "that one first.",
                ephemeral=True, delete_after=15)
            return

        signup_channel = channel_util.find_text_channel(
            guild, SIGNUPS_CHANNEL_NAME)
        if signup_channel is None:
            await interaction.response.send_message(
                f"Couldn't find a `#{SIGNUPS_CHANNEL_NAME}` channel. Run "
                "`/setup-server` first, or create one manually.",
                ephemeral=True, delete_after=15)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        tournament_id = await db.create_tournament(
            guild_id=guild.id,
            organizer_id=member.id,
            name=name,
            match_format=match_format.value,
            max_players=max_players,
            now_iso=_now_iso(),
        )

        t = await db.get_tournament(tournament_id)
        embed = _build_signup_embed(
            tournament_row=t,
            participant_rows=[],
            organizer_mention=member.mention,
        )

        roster_file = await _build_roster_file([])
        try:
            posted = await signup_channel.send(
                embed=embed, view=SignupView(), file=roster_file,
            )
        except discord.HTTPException as e:
            # The tournament row is useless without a signup message to
            # anchor the Join/Leave flow to, so burn the row back down.
            await db.update_tournament_state(tournament_id, "CANCELLED", _now_iso())
            await interaction.followup.send(
                f"Failed to post signup embed: {e}.", ephemeral=True)
            return

        await db.set_tournament_signup_message(
            tournament_id, signup_channel.id, posted.id)

        # Pin so the panel stays reachable even once chat pushes it up.
        # Tidy the pin-notification system message right after so it doesn't
        # read as spam.
        try:
            await posted.pin()
            await _delete_pin_notification(signup_channel)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("failed to pin signup panel for tournament %s: %s",
                        tournament_id, e)

        await interaction.followup.send(
            f"✅ Created **{name}** (`#{tournament_id}`). Signup panel is up "
            f"(pinned) in {signup_channel.mention}. Run "
            f"`/tournament-start name:{name}` when ready to close signups.",
            ephemeral=True,
        )

    @app_commands.command(
        name="tournament-start",
        description="Close signups and start a tournament.",
    )
    @app_commands.describe(name="Tournament name (exact)")
    async def tournament_start(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
    ):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return

        if not _is_organizer(member):
            await interaction.response.send_message(
                f"Need the **{ORGANIZER_ROLE_NAME}** role.",
                ephemeral=True, delete_after=12)
            return

        t = await db.get_active_tournament_by_name(guild.id, name)
        if t is None or t["state"] != "SIGNUPS_OPEN":
            await interaction.response.send_message(
                f"No open signups for **{name}**.",
                ephemeral=True, delete_after=12)
            return

        count = await db.count_participants(t["id"])
        if count < MIN_PLAYERS_TO_START:
            await interaction.response.send_message(
                f"Need at least **{MIN_PLAYERS_TO_START}** players to start a "
                f"Swiss tournament. Currently: **{count}**.",
                ephemeral=True, delete_after=15)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await db.update_tournament_state(t["id"], "IN_PROGRESS", _now_iso())
        t_after = await db.get_tournament(t["id"])
        await _refresh_signup_message(self.bot, t["id"])

        # Generate + persist round-1 pairings, render the bracket PNG, and
        # ship it attached to the hype announcement so entrants see the
        # image inline with their ping.
        pairings = _compute_round1_pairings(
            await _participants_for_pairing(t["id"])
        )
        await db.create_matches(t["id"], 1, pairings)
        bracket_file = await _build_round_bracket_file(t["name"], t["id"], 1)

        await _announce_state_change(
            self.bot, t_after, "started",
            attachment=bracket_file,
            view=ReportWinView(),
        )

        await interaction.followup.send(
            f"🔔 **{name}** is now in progress with **{count}** players. "
            "Round-1 pairings are up. `/report-win` and auto-advance arrive "
            "in the next slice — for now results go through you as organizer.",
            ephemeral=True,
        )

    @tournament_start.autocomplete("name")
    async def _tournament_start_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _autocomplete_tournaments(
            interaction, current, states=("SIGNUPS_OPEN",),
        )

    @app_commands.command(
        name="tournament-cancel",
        description="Cancel a tournament at any stage.",
    )
    @app_commands.describe(name="Tournament name (exact)")
    async def tournament_cancel(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
    ):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return

        if not _is_organizer(member):
            await interaction.response.send_message(
                f"Need the **{ORGANIZER_ROLE_NAME}** role.",
                ephemeral=True, delete_after=12)
            return

        t = await db.get_active_tournament_by_name(guild.id, name)
        if t is None:
            await interaction.response.send_message(
                f"No live tournament named **{name}**.",
                ephemeral=True, delete_after=12)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await db.update_tournament_state(t["id"], "CANCELLED", _now_iso())
        t_after = await db.get_tournament(t["id"])
        await _refresh_signup_message(self.bot, t["id"])
        await _unpin_signup(self.bot, t_after)
        await _announce_state_change(self.bot, t_after, "cancelled")

        await interaction.followup.send(
            f"❌ Cancelled **{name}**.", ephemeral=True)

    @tournament_cancel.autocomplete("name")
    async def _tournament_cancel_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _autocomplete_tournaments(
            interaction, current, states=("SIGNUPS_OPEN", "IN_PROGRESS"),
        )

    # ---- Organizer override ---------------------------------------------- #

    @app_commands.command(
        name="tournament-set-result",
        description="[Organizer] Override a match result — escape hatch for mistakes.",
    )
    @app_commands.describe(
        name="Tournament name",
        round_number="Round number (1, 2, …)",
        match_number="Match number within the round",
        winner="The player who actually won",
    )
    async def tournament_set_result(
        self, interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
        round_number: app_commands.Range[int, 1, 16],
        match_number: app_commands.Range[int, 1, 128],
        winner: discord.Member,
    ):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        if not _is_organizer(member):
            await interaction.response.send_message(
                f"Need the **{ORGANIZER_ROLE_NAME}** role (or Administrator).",
                ephemeral=True, delete_after=12)
            return

        t = await db.get_active_tournament_by_name(guild.id, name)
        if t is None:
            await interaction.response.send_message(
                f"No live tournament named **{name}**.",
                ephemeral=True, delete_after=12)
            return

        rows = await db.list_matches_for_round(t["id"], round_number)
        match = next(
            (r for r in rows if r["match_number"] == match_number),
            None,
        )
        if match is None:
            await interaction.response.send_message(
                f"No match **R{round_number} · #{match_number}** in "
                f"**{name}**.",
                ephemeral=True, delete_after=12)
            return

        if winner.id not in (match["player_a_id"], match["player_b_id"]):
            await interaction.response.send_message(
                f"{winner.mention} isn't a player in that match.",
                ephemeral=True, delete_after=12)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await db.override_match_result(match["id"], winner.id, _now_iso())

        # Update the public report message in place if one exists so
        # onlookers see the correction.
        if match["report_message_id"]:
            channel = guild.get_channel(t["signup_channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(
                        match["report_message_id"])
                    await msg.edit(
                        content=(
                            f"🔧 **Match {match_number} overridden** "
                            f"({t['name']} · Round {round_number}).\n"
                            f"Winner set to {winner.mention} by "
                            f"{member.mention}."
                        ),
                        view=None,
                    )
                except (discord.NotFound, discord.Forbidden,
                        discord.HTTPException):
                    pass

        await audit.post_event(
            guild,
            title="Tournament · result OVERRIDDEN",
            color=discord.Color.dark_red(),
            fields=[
                ("Tournament", t["name"], True),
                ("Match", f"R{round_number} · #{match_number}", True),
                ("New winner", winner.mention, True),
                ("Organizer", member.mention, True),
                ("Previous state", str(match["state"]), True),
            ],
        )

        # DM both players so they know their match was overridden.
        loser_id = (
            match["player_b_id"] if winner.id == match["player_a_id"]
            else match["player_a_id"]
        )
        match_label = f"**{t['name']}** · R{round_number} · Match {match_number}"
        winner_dm = await audit.notify_user_dm(
            winner,
            title="🔧 Match result overridden",
            description=(
                f"An organizer set you as the winner of {match_label} in "
                f"**{guild.name}**. Your bracket standing has been updated."
            ),
            color=discord.Color.green(),
        )
        loser_member = guild.get_member(loser_id) if loser_id else None
        loser_dm = False
        if loser_member is not None:
            loser_dm = await audit.notify_user_dm(
                loser_member,
                title="🔧 Match result overridden",
                description=(
                    f"An organizer overrode the result of {match_label} in "
                    f"**{guild.name}** — {winner.mention} is now the winner. "
                    "If you think this is wrong, message the organizer."
                ),
                color=discord.Color.dark_red(),
            )

        dm_summary = []
        dm_summary.append("winner ✓" if winner_dm else "winner ✗")
        if loser_member is not None:
            dm_summary.append("loser ✓" if loser_dm else "loser ✗")
        await interaction.followup.send(
            f"🔧 Overrode **{name} · R{round_number} · Match {match_number}** "
            f"— winner set to {winner.mention}. (DMs: {', '.join(dm_summary)})",
            ephemeral=True,
        )
        # Override might have been the last outstanding match of the round.
        await _on_match_state_change(interaction.client, match["id"])

    @tournament_set_result.autocomplete("name")
    async def _tournament_set_result_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _autocomplete_tournaments(
            interaction, current, states=("IN_PROGRESS",),
        )

    # ---- Admin participant overrides (escape hatch for Join/Leave) ------- #

    @app_commands.command(
        name="tournament-add-player",
        description="[Organizer] Force-add a player to a tournament's signups.",
    )
    @app_commands.describe(
        name="Tournament name (must be in SIGNUPS_OPEN)",
        member="The Discord member to add",
    )
    async def tournament_add_player(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
        member: discord.Member,
    ):
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        if not _is_organizer(actor):
            await interaction.response.send_message(
                f"Need the **{ORGANIZER_ROLE_NAME}** role (or Administrator).",
                ephemeral=True, delete_after=12)
            return

        t = await db.get_active_tournament_by_name(guild.id, name)
        if t is None or t["state"] != "SIGNUPS_OPEN":
            await interaction.response.send_message(
                f"No tournament in **SIGNUPS_OPEN** state called **{name}**.",
                ephemeral=True, delete_after=12)
            return

        if t["max_players"]:
            current = await db.count_participants(t["id"])
            if current >= t["max_players"]:
                await interaction.response.send_message(
                    f"**{name}** is already at the {t['max_players']}-player cap.",
                    ephemeral=True, delete_after=12)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Snapshot identity from the players row when available — same
        # convention as the Join button — so the bracket stays stable
        # if the player later unlinks.
        player = await db.get_player_by_discord(member.id)
        rank_tier = player["rank_tier"] if player else None
        display_name = player["display_name"] if player else member.display_name

        inserted = await db.add_participant(
            tournament_id=t["id"],
            user_id=member.id,
            display_name=display_name,
            rank_tier=rank_tier,
            now_iso=_now_iso(),
        )
        if not inserted:
            await interaction.followup.send(
                f"{member.mention} is already signed up for **{name}**.",
                ephemeral=True,
            )
            return

        await _refresh_signup_message(self.bot, t["id"])

        dm_sent = await audit.notify_user_dm(
            member,
            title="🎟️ Added to a tournament",
            description=(
                f"An organizer signed you up for **{t['name']}** in "
                f"**{guild.name}**. Watch #🏆-tournaments for the bracket "
                "drop."
            ),
            color=discord.Color.green(),
        )
        dm_suffix = " (player DM'd)" if dm_sent else " (DM blocked)"
        await interaction.followup.send(
            f"✅ Added {member.mention} to **{name}**." + dm_suffix,
            ephemeral=True,
        )
        await audit.post_event(
            guild,
            title="Tournament · player ADDED (admin)",
            color=discord.Color.purple(),
            fields=[
                ("Tournament", t["name"], True),
                ("Player", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{actor.mention} (`{actor.id}`)", True),
                ("Display snapshot", display_name, True),
                ("Rank snapshot", rank_tier or "—", True),
            ],
        )

    @tournament_add_player.autocomplete("name")
    async def _tournament_add_player_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _autocomplete_tournaments(
            interaction, current, states=("SIGNUPS_OPEN",),
        )

    @app_commands.command(
        name="tournament-remove-player",
        description="[Organizer] Force-remove a player from a tournament's signups.",
    )
    @app_commands.describe(
        name="Tournament name (must be in SIGNUPS_OPEN)",
        member="The Discord member to remove",
    )
    async def tournament_remove_player(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
        member: discord.Member,
    ):
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        if not _is_organizer(actor):
            await interaction.response.send_message(
                f"Need the **{ORGANIZER_ROLE_NAME}** role (or Administrator).",
                ephemeral=True, delete_after=12)
            return

        t = await db.get_active_tournament_by_name(guild.id, name)
        if t is None or t["state"] != "SIGNUPS_OPEN":
            await interaction.response.send_message(
                f"No tournament in **SIGNUPS_OPEN** state called **{name}**. "
                "Once a tournament is IN_PROGRESS the participant list is "
                "frozen — handle drops with `/tournament-set-result` instead.",
                ephemeral=True, delete_after=15)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await db.remove_participant(t["id"], member.id)
        if not removed:
            await interaction.followup.send(
                f"{member.mention} wasn't on the **{name}** signup list.",
                ephemeral=True,
            )
            return

        await _refresh_signup_message(self.bot, t["id"])

        dm_sent = await audit.notify_user_dm(
            member,
            title="🎟️ Removed from a tournament",
            description=(
                f"An organizer removed you from **{t['name']}** signups in "
                f"**{guild.name}**. If you think this is wrong, message "
                "the organizer."
            ),
            color=discord.Color.dark_red(),
        )
        dm_suffix = " (player DM'd)" if dm_sent else " (DM blocked)"
        await interaction.followup.send(
            f"✅ Removed {member.mention} from **{name}**." + dm_suffix,
            ephemeral=True,
        )
        await audit.post_event(
            guild,
            title="Tournament · player REMOVED (admin)",
            color=discord.Color.dark_red(),
            fields=[
                ("Tournament", t["name"], True),
                ("Player", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{actor.mention} (`{actor.id}`)", True),
            ],
        )

    @tournament_remove_player.autocomplete("name")
    async def _tournament_remove_player_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _autocomplete_tournaments(
            interaction, current, states=("SIGNUPS_OPEN",),
        )

    # ---- Test mode ------------------------------------------------------- #

    @app_commands.command(
        name="tournament-dev-fill",
        description="[Admin] Fill a tournament with fake entrants for testing.",
    )
    @app_commands.describe(
        name="Tournament name (exact)",
        count="How many fake entrants to add",
    )
    @app_commands.default_permissions(administrator=True)
    async def tournament_dev_fill(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
        count: app_commands.Range[int, 1, 64],
    ):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        if not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "Admin only.", ephemeral=True, delete_after=8)
            return

        t = await db.get_active_tournament_by_name(guild.id, name)
        if t is None or t["state"] != "SIGNUPS_OPEN":
            await interaction.response.send_message(
                f"Need a tournament in SIGNUPS_OPEN state. "
                f"Nothing open called **{name}**.",
                ephemeral=True, delete_after=12)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Respect an organizer-set cap — if we can't add the full count,
        # add as many as fit and tell the caller.
        current = await db.count_participants(t["id"])
        remaining = (
            max(0, t["max_players"] - current) if t["max_players"] else count
        )
        to_add = min(count, remaining)
        if to_add <= 0:
            await interaction.followup.send(
                f"Tournament is already at cap ({current}/{t['max_players']}).",
                ephemeral=True)
            return

        existing = await db.list_participants(t["id"])
        used_ids = {p["user_id"] for p in existing}

        now = _now_iso()
        added = 0
        next_uid = DEV_FAKE_UID_MIN
        all_ranks = list(wavu.ALL_RANK_NAMES)

        for i in range(to_add):
            while next_uid in used_ids and next_uid <= DEV_FAKE_UID_MAX:
                next_uid += 1
            if next_uid > DEV_FAKE_UID_MAX:
                log.warning("dev-fill ran out of fake UIDs")
                break
            uid = next_uid
            next_uid += 1

            # Pick fake identity. random.choice makes each fill batch
            # feel a bit different.
            display_name = random.choice(DEV_FAKE_NAMES) + f" #{uid - DEV_FAKE_UID_MIN + 1}"
            rank_tier = random.choice(all_ranks)
            main_char = random.choice(DEV_FAKE_CHARS)
            tekken_id = f"{DEV_FAKE_TEKKEN_PREFIX}{uid:07d}"

            # Populate players table so character + rank icons resolve via
            # the normal render path. /tournament-dev-cleanup wipes these.
            await db.upsert_player(
                discord_id=uid,
                tekken_id=tekken_id,
                display_name=display_name,
                main_char=main_char,
                rating_mu=None,
                rank_tier=rank_tier,
                linked_by=member.id,
                now_iso=now,
            )
            inserted = await db.add_participant(
                tournament_id=t["id"],
                user_id=uid,
                display_name=display_name,
                rank_tier=rank_tier,
                now_iso=now,
            )
            if inserted:
                used_ids.add(uid)
                added += 1

        await _refresh_signup_message(self.bot, t["id"])

        note = ""
        if added < count:
            note = (f" (capped by max_players — {added} of {count} fit)"
                    if t["max_players"] else
                    f" (stopped early, added {added} of {count})")
        await interaction.followup.send(
            f"🧪 Added **{added}** test-bot{'s' if added != 1 else ''} to "
            f"**{name}**{note}. Use `/tournament-dev-cleanup` to wipe "
            "test-bot player rows when you're done testing.",
            ephemeral=True,
        )

    @tournament_dev_fill.autocomplete("name")
    async def _tournament_dev_fill_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _autocomplete_tournaments(
            interaction, current, states=("SIGNUPS_OPEN",),
        )

    @app_commands.command(
        name="tournament-dev-cleanup",
        description="[Admin] Delete synthetic test-bot player rows.",
    )
    @app_commands.default_permissions(administrator=True)
    async def tournament_dev_cleanup(
        self, interaction: discord.Interaction,
    ):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8)
            return
        if not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "Admin only.", ephemeral=True, delete_after=8)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await db.delete_fake_players()
        await interaction.followup.send(
            f"🧹 Removed **{removed}** test-bot row{'s' if removed != 1 else ''} "
            "from the players table. Participant snapshots from past test "
            "tournaments linger harmlessly in `tournament_participants`.",
            ephemeral=True,
        )


async def _autocomplete_tournaments(
    interaction: discord.Interaction,
    current: str,
    states: tuple[str, ...],
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    rows = await db.list_tournaments(interaction.guild.id, states=states)
    needle = current.lower()
    hits = [r for r in rows if needle in r["name"].lower()]
    # Discord caps autocomplete at 25 options.
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in hits[:25]]


# --------------------------------------------------------------------------- #
# Match reporting (slice 2)                                                    #
# --------------------------------------------------------------------------- #

REPORTER_CANCEL_WINDOW_SECONDS = 60


class ReportWinView(ErrorHandledView):
    """Persistent single-button view attached to every round-start
    announcement. The button's callback queries the clicker's PENDING
    matches across the guild — 0 matches bails, 1 match proceeds
    directly, 2+ matches show a select menu picker."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Report a Win", emoji="⚔️",
        style=discord.ButtonStyle.success,
        custom_id="mreport:start",
    )
    async def report_start(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _flow_report_win_start(interaction)


class MatchReportPublicView(ErrorHandledView):
    """Persistent Confirm/Dispute buttons on the public report-request
    message pinged at the loser. Looks up the match by message_id on
    click so one registered view handles every in-flight report."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Confirm", emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="mreport:confirm",
    )
    async def confirm(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _flow_loser_confirm(interaction)

    @discord.ui.button(
        label="Dispute", emoji="⚠️",
        style=discord.ButtonStyle.danger,
        custom_id="mreport:dispute",
    )
    async def dispute(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _flow_loser_dispute(interaction)


class DisputeResolveView(ErrorHandledView):
    """Attached to the same message once it transitions to DISPUTED.
    Organizer/Admin/Mod picks which player won; locks the result in as
    CONFIRMED."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        if any(r.name in (ORGANIZER_ROLE_NAME, "Moderator")
               for r in member.roles):
            return True
        await interaction.response.send_message(
            f"Only **{ORGANIZER_ROLE_NAME}** / **Moderator** / **Admin** "
            "can resolve disputes.",
            ephemeral=True, delete_after=10,
        )
        return False

    @discord.ui.button(
        label="Player A wins", emoji="🅰️",
        style=discord.ButtonStyle.secondary,
        custom_id="mreport:resolve_a",
    )
    async def pick_a(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _flow_organizer_resolve(interaction, side="a")

    @discord.ui.button(
        label="Player B wins", emoji="🅱️",
        style=discord.ButtonStyle.secondary,
        custom_id="mreport:resolve_b",
    )
    async def pick_b(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _flow_organizer_resolve(interaction, side="b")


class MatchPickerView(ErrorHandledView):
    """Ephemeral select menu — shown when the clicker has 2+ pending
    matches and needs to say which one they won."""

    def __init__(self, matches: list):
        super().__init__(timeout=180)
        options = []
        for m in matches[:25]:
            opp_id = (m["player_b_id"] if m["player_a_id"] == m.get("viewer_id")
                      else m["player_a_id"])
            opp_label = m.get("opponent_display") or f"vs <user {opp_id}>"
            options.append(discord.SelectOption(
                label=f"{m['tournament_name']} · Match {m['match_number']}"[:100],
                description=f"Round {m['round_number']} · {opp_label}"[:100],
                value=str(m["id"]),
            ))
        self.select = discord.ui.Select(
            placeholder="Which match did you win?",
            options=options or [discord.SelectOption(label="no matches", value="0")],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        match_id = int(self.select.values[0])
        match = await db.get_match(match_id)
        if match is None or match["state"] != "PENDING":
            await interaction.response.edit_message(
                content="That match isn't pending anymore. Try again.",
                view=None, embed=None,
            )
            return
        await _flow_confirm_opponent(interaction, match)


class ConfirmReportView(ErrorHandledView):
    """Final 'You beat <opponent>? Yes/Cancel' gate before we record
    the claim and ping the loser publicly."""

    def __init__(self, match_id: int):
        super().__init__(timeout=60)
        self.match_id = match_id

    @discord.ui.button(
        label="Yes — I won", emoji="⚔️",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await _flow_execute_report(interaction, self.match_id)

    @discord.ui.button(
        label="Cancel", style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="Cancelled. No claim filed.", view=None, embed=None,
        )


class ReporterCancelView(ErrorHandledView):
    """Ephemeral 60-second 'Oops, cancel my report' undo button — only
    the reporter sees this and only the match is revertable while the
    loser hasn't clicked Confirm/Dispute yet."""

    def __init__(self, match_id: int):
        super().__init__(timeout=REPORTER_CANCEL_WINDOW_SECONDS)
        self.match_id = match_id

    @discord.ui.button(
        label="Cancel my report", style=discord.ButtonStyle.danger,
        emoji="↩️",
    )
    async def undo(
        self, interaction: discord.Interaction, _b: discord.ui.Button,
    ):
        ok = await db.cancel_match_report(self.match_id, interaction.user.id)
        if not ok:
            await interaction.response.edit_message(
                content=("Too late — the loser already confirmed or "
                         "disputed. Ask the organizer if you need a fix."),
                view=None,
            )
            return
        # Delete the public request message if we can find it.
        match = await db.get_match(self.match_id)
        if match and match["report_message_id"]:
            tournament = await db.get_tournament(match["tournament_id"])
            if tournament and tournament["signup_channel_id"]:
                channel = interaction.client.get_channel(
                    tournament["signup_channel_id"])
                if isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(
                            match["report_message_id"])
                        await msg.delete()
                    except (discord.NotFound, discord.Forbidden,
                            discord.HTTPException):
                        pass
        await interaction.response.edit_message(
            content="✅ Report cancelled. Match is back in PENDING.",
            view=None,
        )


# ---- Flow functions ------------------------------------------------------- #

async def _flow_report_win_start(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return

    matches = await db.list_pending_matches_for_user_in_guild(
        guild.id, member.id)
    if not matches:
        await interaction.response.send_message(
            "You don't have any pending matches right now. If you think "
            "this is wrong, ping the organizer.",
            ephemeral=True, delete_after=15,
        )
        return

    if len(matches) == 1:
        await _flow_confirm_opponent(interaction, matches[0])
        return

    # Multiple tournaments in flight: enrich each match option with the
    # opponent's display name for the select-menu description.
    enriched: list[dict] = []
    for m in matches:
        opp_id = (m["player_b_id"] if m["player_a_id"] == member.id
                  else m["player_a_id"])
        opp = await db.get_participant(m["tournament_id"], opp_id) if opp_id else None
        enriched.append({
            "id": m["id"],
            "tournament_name": m["tournament_name"],
            "match_number": m["match_number"],
            "round_number": m["round_number"],
            "player_a_id": m["player_a_id"],
            "player_b_id": m["player_b_id"],
            "viewer_id": member.id,
            "opponent_display": opp["display_name"] if opp else f"user {opp_id}",
        })
    view = MatchPickerView(enriched)
    await interaction.response.send_message(
        "Pick the match you won:", view=view, ephemeral=True,
    )


async def _flow_confirm_opponent(
    interaction: discord.Interaction, match,
) -> None:
    user_id = interaction.user.id
    if user_id not in (match["player_a_id"], match["player_b_id"]):
        await interaction.response.send_message(
            "You're not a participant in that match.", ephemeral=True,
            delete_after=10,
        )
        return
    opp_id = (match["player_b_id"] if match["player_a_id"] == user_id
              else match["player_a_id"])
    opp = await db.get_participant(match["tournament_id"], opp_id) if opp_id else None
    opp_name = opp["display_name"] if opp else f"user {opp_id}"
    tournament = await db.get_tournament(match["tournament_id"])

    embed = discord.Embed(
        title=f"Confirm: you beat {opp_name}?",
        description=(
            f"**{tournament['name']} · Match {match['match_number']}**\n"
            f"Round {match['round_number']}\n\n"
            f"Clicking **Yes** pings <@{opp_id}> to confirm or dispute.\n"
            "You'll have **60 seconds** to cancel if you picked the wrong match."
        ),
        color=discord.Color.red(),
    )

    view = ConfirmReportView(match_id=match["id"])
    # If we got here from a select menu (already has an ephemeral
    # response open), edit it; otherwise send a fresh ephemeral.
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view, content=None)
    else:
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True,
        )


async def _flow_execute_report(
    interaction: discord.Interaction, match_id: int,
) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "Server-only.", ephemeral=True, delete_after=8)
        return

    match = await db.get_match(match_id)
    if match is None:
        await interaction.response.edit_message(
            content="Match not found.", view=None, embed=None)
        return
    if match["state"] != "PENDING":
        await interaction.response.edit_message(
            content="That match was already reported by someone else.",
            view=None, embed=None)
        return
    if member.id not in (match["player_a_id"], match["player_b_id"]):
        await interaction.response.edit_message(
            content="You're not a participant in that match.",
            view=None, embed=None)
        return

    opp_id = (match["player_b_id"] if match["player_a_id"] == member.id
              else match["player_a_id"])
    if opp_id is None:
        # Bye — shouldn't happen since byes are CONFIRMED at pairing,
        # but belt-and-braces.
        await interaction.response.edit_message(
            content="That match is a bye, nothing to report.",
            view=None, embed=None)
        return

    now = _now_iso()
    ok = await db.report_match_win(match_id, member.id, member.id, now)
    if not ok:
        await interaction.response.edit_message(
            content="Someone else already reported that match.",
            view=None, embed=None)
        return

    tournament = await db.get_tournament(match["tournament_id"])
    channel = interaction.client.get_channel(tournament["signup_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.edit_message(
            content="Couldn't find the tournament channel to post in.",
            view=None, embed=None)
        return

    msg_content = (
        f"⚔️ <@{member.id}> reported a **win** over <@{opp_id}> in "
        f"**{tournament['name']} · Match {match['match_number']}** "
        f"(Round {match['round_number']}).\n\n"
        f"<@{opp_id}> — **Confirm** if correct, **Dispute** if not.\n"
        "Dispute routes to the organizer for a manual call."
    )
    try:
        posted = await channel.send(
            msg_content,
            view=MatchReportPublicView(),
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(opp_id)], roles=False, everyone=False,
            ),
        )
    except discord.HTTPException as e:
        # Roll back the state transition — the DB-visible report without
        # a loser-facing message would be a ghost.
        await db.cancel_match_report(match_id, member.id)
        await interaction.response.edit_message(
            content=f"Failed to post the confirm message: {e}. "
                    "Report cancelled — try again.",
            view=None, embed=None)
        return

    await db.set_match_report_message(match_id, posted.id)

    audit_fields = [
        ("Tournament", tournament["name"], True),
        ("Match", f"R{match['round_number']} · #{match['match_number']}", True),
        ("Reporter → Winner", f"<@{member.id}>", True),
        ("Loser (awaiting confirm)", f"<@{opp_id}>", True),
    ]
    await audit.post_event(
        interaction.guild,
        title="Tournament · result reported",
        color=discord.Color.red(),
        fields=audit_fields,
    )

    await interaction.response.edit_message(
        content=(
            f"✅ Report filed. <@{opp_id}> has {REPORTER_CANCEL_WINDOW_SECONDS}s "
            "to confirm or dispute publicly.\n\n"
            "Picked the wrong match? Click **Cancel my report** within "
            f"{REPORTER_CANCEL_WINDOW_SECONDS} seconds."
        ),
        view=ReporterCancelView(match_id),
        embed=None,
    )


async def _flow_loser_confirm(interaction: discord.Interaction) -> None:
    msg = interaction.message
    if msg is None:
        return
    match = await db.get_match_by_report_message(msg.id)
    if match is None or match["state"] != "REPORTED":
        await interaction.response.send_message(
            "This match isn't awaiting confirmation anymore.",
            ephemeral=True, delete_after=10,
        )
        return
    user_id = interaction.user.id
    if user_id == match["reporter_id"]:
        await interaction.response.send_message(
            "You can't confirm your own win — your opponent confirms.",
            ephemeral=True, delete_after=10,
        )
        return
    if user_id not in (match["player_a_id"], match["player_b_id"]):
        await interaction.response.send_message(
            "Only the two players in this match can act.",
            ephemeral=True, delete_after=10,
        )
        return

    ok = await db.confirm_match_report(match["id"])
    if not ok:
        await interaction.response.send_message(
            "State changed — refresh and try again.", ephemeral=True, delete_after=10,
        )
        return

    tournament = await db.get_tournament(match["tournament_id"])
    winner = match["winner_id"]
    loser = user_id
    new_content = (
        f"✅ **Match {match['match_number']} confirmed** "
        f"({tournament['name']} · Round {match['round_number']}).\n"
        f"Winner: <@{winner}>. Confirmed by <@{loser}>."
    )
    try:
        await msg.edit(content=new_content, view=None)
    except discord.HTTPException:
        pass
    await interaction.response.defer()

    await audit.post_event(
        interaction.guild,
        title="Tournament · result confirmed",
        color=discord.Color.green(),
        fields=[
            ("Tournament", tournament["name"], True),
            ("Match", f"R{match['round_number']} · #{match['match_number']}", True),
            ("Winner", f"<@{winner}>", True),
            ("Confirmed by", f"<@{loser}>", True),
        ],
    )
    # Auto-advance if this was the last match of the round.
    await _on_match_state_change(interaction.client, match["id"])


async def _flow_loser_dispute(interaction: discord.Interaction) -> None:
    msg = interaction.message
    if msg is None:
        return
    match = await db.get_match_by_report_message(msg.id)
    if match is None or match["state"] != "REPORTED":
        await interaction.response.send_message(
            "This match isn't awaiting confirmation anymore.",
            ephemeral=True, delete_after=10,
        )
        return
    user_id = interaction.user.id
    if user_id == match["reporter_id"]:
        await interaction.response.send_message(
            "You can't dispute your own claim.",
            ephemeral=True, delete_after=10,
        )
        return
    if user_id not in (match["player_a_id"], match["player_b_id"]):
        await interaction.response.send_message(
            "Only the two players in this match can act.",
            ephemeral=True, delete_after=10,
        )
        return

    ok = await db.dispute_match_report(match["id"])
    if not ok:
        await interaction.response.send_message(
            "State changed — refresh and try again.",
            ephemeral=True, delete_after=10,
        )
        return

    tournament = await db.get_tournament(match["tournament_id"])
    a_id = match["player_a_id"]
    b_id = match["player_b_id"]
    reporter_id = match["reporter_id"]
    disputer_id = user_id
    new_content = (
        f"⚠️ **Match {match['match_number']} disputed** "
        f"({tournament['name']} · Round {match['round_number']}).\n"
        f"<@{reporter_id}> claimed the win; <@{disputer_id}> disagreed.\n\n"
        f"**Organizer call:** who won — <@{a_id}> (A) or <@{b_id}> (B)?"
    )
    try:
        await msg.edit(content=new_content, view=DisputeResolveView())
    except discord.HTTPException:
        pass
    await interaction.response.defer()

    await audit.post_event(
        interaction.guild,
        title="Tournament · result DISPUTED",
        color=discord.Color.orange(),
        fields=[
            ("Tournament", tournament["name"], True),
            ("Match", f"R{match['round_number']} · #{match['match_number']}", True),
            ("Claimed by", f"<@{reporter_id}>", True),
            ("Disputed by", f"<@{disputer_id}>", True),
        ],
    )


async def _on_match_state_change(
    client: discord.Client, match_id: int,
) -> None:
    """Called after any transition to CONFIRMED. If the round is
    complete, either advances to the next round or closes out the
    tournament. Idempotent — re-entry on subsequent match confirms in
    the same already-advanced round bails out early."""
    match = await db.get_match(match_id)
    if match is None or match["state"] != "CONFIRMED":
        return

    tournament_id = match["tournament_id"]
    round_num = match["round_number"]

    if not await db.is_round_complete(tournament_id, round_num):
        return

    # If we've already paired the next round, don't re-enter.
    next_round_rows = await db.list_matches_for_round(
        tournament_id, round_num + 1)
    if next_round_rows:
        return

    tournament = await db.get_tournament(tournament_id)
    if tournament is None or tournament["state"] != "IN_PROGRESS":
        return

    participant_count = await db.count_participants(tournament_id)
    total_rounds = _compute_total_rounds(participant_count)

    if round_num >= total_rounds:
        await _complete_tournament(client, tournament)
    else:
        await _advance_to_next_round(client, tournament, round_num)


def _compute_total_rounds(participant_count: int) -> int:
    """Swiss standard: ceil(log2(N)). Clamped at 1 to handle sub-4
    edge cases that slipped past the start-time min-players check."""
    if participant_count < 2:
        return 1
    return max(1, math.ceil(math.log2(participant_count)))


async def _compute_next_round_pairings(tournament) -> list[tuple]:
    """Simplified Dutch Swiss pairing for round N+1.

    Scoring: 1 point per CONFIRMED win (byes count). Players sort by
    (wins desc, rank-ordinal desc, user_id asc). Then split top half
    vs bottom half and pair across — same shape as round 1 but grouped
    by score. Bye (for odd counts) goes to the lowest-ranked player
    who hasn't already had one.

    Rematches are accepted occasionally for simplicity — full Dutch
    push-up/push-down is overkill for 8-16 player community brackets.
    """
    tournament_id = tournament["id"]
    participants = await db.list_participants(tournament_id)
    all_matches = await db.list_matches_for_tournament(tournament_id)

    wins: dict[int, int] = defaultdict(int)
    for m in all_matches:
        if m["state"] == "CONFIRMED" and m["winner_id"] is not None:
            wins[m["winner_id"]] += 1

    rank_ord: dict[int, int] = {}
    for p in participants:
        ord_ = _RANK_ORDINAL_BY_NAME.get(p["rank_tier"])
        rank_ord[p["user_id"]] = ord_ if ord_ is not None else -1

    had_bye: set[int] = set()
    for m in all_matches:
        if m["player_a_id"] is not None and m["player_b_id"] is None:
            had_bye.add(m["player_a_id"])

    active = [p for p in participants if not p["forfeited"]]
    active.sort(key=lambda p: (
        -wins.get(p["user_id"], 0),
        -rank_ord[p["user_id"]],
        p["user_id"],
    ))

    pairings: list[tuple] = []
    bye_player = None
    if len(active) % 2 == 1:
        # Lowest-ranked player without a prior bye gets this round's bye.
        for i in range(len(active) - 1, -1, -1):
            if active[i]["user_id"] not in had_bye:
                bye_player = active.pop(i)
                break
        else:
            # Everyone already had one — give it to the lowest seed
            # anyway (edge case in very long tournaments).
            bye_player = active.pop()

    half = len(active) // 2
    top = active[:half]
    bot = active[half:]
    for t, b in zip(top, bot):
        pairings.append((t["user_id"], b["user_id"], None))

    if bye_player is not None:
        pairings.append(
            (bye_player["user_id"], None, bye_player["user_id"]))

    return pairings


async def _advance_to_next_round(
    client: discord.Client, tournament, completed_round: int,
) -> None:
    next_round = completed_round + 1
    pairings = await _compute_next_round_pairings(tournament)
    if not pairings:
        return
    await db.create_matches(
        tournament["id"], next_round, pairings, _now_iso(),
    )

    bracket_file = await _build_round_bracket_file(
        tournament["name"], tournament["id"], next_round,
    )
    channel = client.get_channel(tournament["signup_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    participants = await db.list_participants(tournament["id"])
    active_mentions = " ".join(
        f"<@{p['user_id']}>"
        for p in participants if not p["forfeited"]
    ) or "*(no active players)*"

    headline = (
        f"⚔️ **{tournament['name']} — ROUND {next_round} LIVE!**"
    )
    body = (
        f"{tournament['match_format']} · Swiss pairings below. "
        "Play your match, then hit **⚔️ Report a Win**."
    )

    try:
        await channel.send(
            f"{headline}\n{body}\n\n{active_mentions}",
            file=bracket_file,
            view=ReportWinView(),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException as e:
        log.warning("round %d announcement failed for %s: %s",
                    next_round, tournament["id"], e)


async def _complete_tournament(
    client: discord.Client, tournament,
) -> None:
    tournament_id = tournament["id"]
    await db.update_tournament_state(
        tournament_id, "COMPLETED", _now_iso(),
    )

    standings = await _compute_final_standings(tournament_id)
    if standings:
        # Cache the champion on the tournament row so future badge
        # queries don't have to recompute standings to ask "did X win".
        await db.set_tournament_winner(tournament_id, standings[0]["user_id"])
    channel = client.get_channel(tournament["signup_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    podium_icons = ["🥇", "🥈", "🥉"]
    lines: list[str] = []
    for i, s in enumerate(standings):
        icon = podium_icons[i] if i < 3 else f"#{i + 1}"
        rank_bit = f" · {s['rank_tier']}" if s["rank_tier"] else ""
        lines.append(
            f"{icon} <@{s['user_id']}>{rank_bit}  "
            f"— {s['wins']} W · Buchholz {s['buchholz']}"
        )
    body = "\n".join(lines) if lines else "*(no standings computed)*"

    mentions = " ".join(
        f"<@{s['user_id']}>" for s in standings
    ) or ""

    headline = f"🏆 **{tournament['name']} — COMPLETE!**"
    footer = (
        "\n\nFinal bracket will archive into #🗂️-tournament-history "
        "in the next update. Thanks for playing."
    )

    try:
        await channel.send(
            f"{headline}\n\n**FINAL STANDINGS**\n{body}{footer}\n\n{mentions}",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException as e:
        log.warning("completion message failed for %s: %s",
                    tournament_id, e)

    # Champion celebration card — gold trim banner with the winner's
    # portrait + rank flair. Best-effort: a render or send failure is
    # logged but doesn't block the rest of the finalisation flow.
    if standings:
        winner_row = standings[0]
        runner_row = standings[1] if len(standings) > 1 else None
        winner_player = await db.get_player_by_discord(winner_row["user_id"])
        winner_character = (
            winner_player["main_char"] if winner_player else None
        )
        try:
            card_buf = await tournament_render.render_tournament_champion_card(
                tournament_name=tournament["name"],
                winner_name=winner_row["display_name"],
                winner_character=winner_character,
                winner_rank=winner_row["rank_tier"],
                runner_up_name=(
                    runner_row["display_name"] if runner_row else None
                ),
                entrants=len(standings),
                rounds_played=_compute_total_rounds(len(standings)),
            )
            embed = discord.Embed(
                title=f"🏆 Champion · {winner_row['display_name']}",
                description=(
                    f"<@{winner_row['user_id']}> takes **{tournament['name']}** "
                    f"with {winner_row['wins']} wins · Buchholz "
                    f"{winner_row['buchholz']}."
                ),
                color=discord.Color.from_rgb(212, 175, 55),
            )
            embed.set_image(url="attachment://champion.png")
            await channel.send(
                embed=embed,
                file=discord.File(card_buf, filename="champion.png"),
            )
        except Exception:
            log.exception(
                "champion card render/post failed for tournament %s",
                tournament_id,
            )

    await _unpin_signup(client, tournament)

    await audit.post_event(
        client.get_guild(tournament["guild_id"]),
        title="Tournament · COMPLETE",
        color=discord.Color.gold(),
        fields=[
            ("Tournament", tournament["name"], True),
            ("Winner",
             f"<@{standings[0]['user_id']}>" if standings else "—", True),
            ("Total rounds",
             str(_compute_total_rounds(len(standings))), True),
        ],
    )


async def _compute_final_standings(tournament_id: int) -> list[dict]:
    """Rankings by wins (desc), Buchholz (desc), head-to-head (desc),
    user_id (asc) as deterministic tiebreakers."""
    participants = await db.list_participants(tournament_id)
    all_matches = await db.list_matches_for_tournament(tournament_id)

    wins: dict[int, int] = defaultdict(int)
    opponents: dict[int, list[int]] = defaultdict(list)
    h2h_wins: dict[tuple[int, int], int] = defaultdict(int)

    for m in all_matches:
        if m["state"] != "CONFIRMED":
            continue
        winner = m["winner_id"]
        a, b = m["player_a_id"], m["player_b_id"]
        if winner is not None:
            wins[winner] += 1
        if a is not None and b is not None:
            opponents[a].append(b)
            opponents[b].append(a)
            if winner is not None:
                loser = b if winner == a else a
                h2h_wins[(winner, loser)] += 1

    def buchholz_of(uid: int) -> int:
        return sum(wins.get(o, 0) for o in opponents[uid])

    out: list[dict] = []
    for p in participants:
        uid = p["user_id"]
        out.append({
            "user_id": uid,
            "display_name": p["display_name"],
            "rank_tier": p["rank_tier"],
            "wins": wins.get(uid, 0),
            "buchholz": buchholz_of(uid),
        })

    # Primary sort: wins desc, Buchholz desc. H2H applied as a second
    # pass swap where two adjacent tied players have beaten each other.
    out.sort(key=lambda s: (-s["wins"], -s["buchholz"], s["user_id"]))
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if a["wins"] == b["wins"] and a["buchholz"] == b["buchholz"]:
            # Prefer whichever beat the other in head-to-head.
            if h2h_wins.get((b["user_id"], a["user_id"]), 0) > \
               h2h_wins.get((a["user_id"], b["user_id"]), 0):
                out[i], out[i + 1] = b, a
    return out


async def _flow_organizer_resolve(
    interaction: discord.Interaction, side: str,
) -> None:
    msg = interaction.message
    if msg is None:
        return
    match = await db.get_match_by_report_message(msg.id)
    if match is None or match["state"] != "DISPUTED":
        await interaction.response.send_message(
            "This match isn't awaiting resolution.",
            ephemeral=True, delete_after=10,
        )
        return

    winner_id = (match["player_a_id"] if side == "a"
                 else match["player_b_id"])
    if winner_id is None:
        await interaction.response.send_message(
            "Can't resolve a match with no player on that side.",
            ephemeral=True, delete_after=10,
        )
        return

    await db.resolve_disputed_match(match["id"], winner_id, _now_iso())

    tournament = await db.get_tournament(match["tournament_id"])
    new_content = (
        f"✅ **Match {match['match_number']} resolved by organizer.**\n"
        f"{tournament['name']} · Round {match['round_number']}\n"
        f"Winner: <@{winner_id}> · Decided by {interaction.user.mention}."
    )
    try:
        await msg.edit(content=new_content, view=None)
    except discord.HTTPException:
        pass
    await interaction.response.defer()

    await audit.post_event(
        interaction.guild,
        title="Tournament · dispute resolved",
        color=discord.Color.green(),
        fields=[
            ("Tournament", tournament["name"], True),
            ("Match", f"R{match['round_number']} · #{match['match_number']}", True),
            ("Winner", f"<@{winner_id}>", True),
            ("Organizer", interaction.user.mention, True),
        ],
    )
    await _on_match_state_change(interaction.client, match["id"])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tournament(bot))
