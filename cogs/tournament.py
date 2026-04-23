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
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
import media
import tournament_render
import wavu
from cogs.onboarding import VERIFIED_ROLE_NAME

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

class SignupView(discord.ui.View):
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
) -> None:
    """Post a separate, non-editable message in the signup channel that
    mentions every participant — this is the notification vehicle,
    separate from the persistent panel. kind is 'started' or 'cancelled'.
    If attachment is provided it ships in the same message (so Discord's
    image preview hangs directly under the hype text)."""
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
            "Round 1 pairings below. Run your matches and report results to "
            "the organizer for now — `/report-win` lands next slice."
        )
    else:
        headline = f"❌ **{t['name']} — CANCELLED.**"
        body = "This tournament has been called off. Apologies for the shuffle."

    kwargs: dict = {"allowed_mentions": discord.AllowedMentions(users=True)}
    if attachment is not None:
        kwargs["file"] = attachment
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

class Tournament(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # One SignupView serves every signup message in every guild — Discord
        # routes clicks by custom_id, and the callbacks resolve the tournament
        # from interaction.message.id.
        self.bot.add_view(SignupView())

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        cmd_name = interaction.command.name if interaction.command else "<unknown>"
        log.exception("Slash command /%s raised: %s", cmd_name, error)
        msg = (f"⚠ `/{cmd_name}` failed: `{type(error).__name__}: {error}`\n"
               "*Check the bot console for the traceback.*")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

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

        signup_channel = discord.utils.get(
            guild.text_channels, name=SIGNUPS_CHANNEL_NAME)
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
            self.bot, t_after, "started", attachment=bracket_file,
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tournament(bot))
