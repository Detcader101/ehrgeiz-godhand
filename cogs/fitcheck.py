"""Fit Check — community-voted character customisation showcase.

A verified-only `#📸-fit-check` channel where members post a screenshot
of their character customisation, tagged with the Tekken character it's
for. Other verified members upvote / downvote each post; the totals
flow into a rolling leaderboard for a low-stakes "best fit of the week"
competition.

Architecture mirrors the rest of the bot:
- One persistent `FitcheckVoteView` registered at cog-load; every entry's
  message routes its button clicks back to the same view instance, which
  resolves the entry via `interaction.message.id`.
- Voting state lives in `db.fitcheck_votes` with a (entry_id, user_id)
  primary key, so re-clicks toggle (Reddit-style) without juggling state.
- Character pick uses a slash-command autocomplete against
  `wavu.T8_CHARACTERS`, the same canonical roster used for main_char on
  player profiles.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import audit
import channel_util
import db
import tournament_render
import wavu
from cogs.onboarding import VERIFIED_ROLE_NAME
from view_util import ErrorHandledView, handle_app_command_error

log = logging.getLogger(__name__)

FITCHECK_CHANNEL_NAME = "fit-check"
FITCHECK_PANEL_KIND = "fitcheck"
ANNOUNCEMENTS_CHANNEL = "announcements"
# Cap the attachment size we accept — Pillow isn't loading these (Discord
# hosts the URL itself), but a 50 MB clip uploaded with `image/png`
# content-type would still take a slot in the channel and clutter the
# leaderboard. 8 MB matches Discord's free-tier upload ceiling and is
# generous for a screenshot.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Default leaderboard window — a week of fit checks fits the "best fit
# of the week" framing without making the all-time leaderboard meaningful
# yet (which would punish early posters).
DEFAULT_WINDOW = timedelta(days=7)

# Drip Lord rotation cadence + state-store key. Rotator polls hourly and
# only acts when at least DRIP_LORD_INTERVAL has elapsed since the last
# rotation, so the actual cadence is "weekly, rounded to the next hour
# after the prior rotation."
DRIP_LORD_ROLE_NAME = "Drip Lord"
DRIP_LORD_INTERVAL = timedelta(days=7)
DRIP_LORD_POLL_INTERVAL = timedelta(hours=1)
DRIP_LORD_STATE_KEY = "fitcheck:last_drip_lord_rotation"
DRIP_LORD_STARTUP_DELAY = timedelta(seconds=60)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_verified(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(r.name == VERIFIED_ROLE_NAME for r in member.roles)


# --------------------------------------------------------------------------- #
# Persistent vote view                                                          #
# --------------------------------------------------------------------------- #

class FitcheckVoteView(ErrorHandledView):
    """Persistent 👍/👎 view attached to every fit-check post.

    A single instance handles every entry across the guild — the per-row
    state lookup is `db.get_fitcheck_by_message(interaction.message.id)`.
    Vote counts appear in the button labels so members can see the
    tally without opening any extra UI.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="0", emoji="👍",
        style=discord.ButtonStyle.success,
        custom_id="fc:up", row=0,
    )
    async def upvote(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _handle_vote(interaction, "up")

    @discord.ui.button(
        label="0", emoji="👎",
        style=discord.ButtonStyle.danger,
        custom_id="fc:down", row=0,
    )
    async def downvote(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await _handle_vote(interaction, "down")


async def _handle_vote(interaction: discord.Interaction, vote_type: str) -> None:
    msg = interaction.message
    if msg is None:
        await interaction.response.send_message(
            "Couldn't read this post — try again in a moment.",
            ephemeral=True, delete_after=8,
        )
        return

    if not _is_verified(interaction.user):
        await interaction.response.send_message(
            f"🔒 Only **{VERIFIED_ROLE_NAME}** members can vote on fit checks. "
            "Head to **#player-hub** and click **Verify** first.",
            ephemeral=True, delete_after=12,
        )
        return

    entry = await db.get_fitcheck_by_message(msg.id)
    if entry is None:
        # The entry row was deleted (mod purge, post author retract) but
        # the message survived — defang the buttons rather than crash on
        # the next click.
        await interaction.response.send_message(
            "This fit check is no longer tracked. Voting is closed on it.",
            ephemeral=True, delete_after=10,
        )
        return

    # Self-votes feel cringe and pad the leaderboard. Cheap to refuse.
    if interaction.user.id == entry["poster_id"]:
        await interaction.response.send_message(
            "You can't vote on your own fit check.",
            ephemeral=True, delete_after=8,
        )
        return

    result = await db.set_fitcheck_vote(
        entry_id=entry["id"], user_id=interaction.user.id,
        vote_type=vote_type, now_iso=_now_iso(),
    )
    ups, downs = await db.get_fitcheck_vote_counts(entry["id"])

    # Refresh the buttons in-place so everyone sees the new tally.
    new_view = FitcheckVoteView()
    # Walk children and rewrite labels on the matching custom_ids — using
    # set on a fresh view keeps Discord's persistent-view contract intact.
    for child in new_view.children:
        if isinstance(child, discord.ui.Button):
            if child.custom_id == "fc:up":
                child.label = str(ups)
            elif child.custom_id == "fc:down":
                child.label = str(downs)

    try:
        await interaction.response.edit_message(view=new_view)
    except discord.HTTPException:
        # Edit failed (rate limit, message deleted) — recover by sending
        # an ephemeral confirmation so the user knows the click worked.
        log.warning("[fitcheck] vote edit failed for entry=%s", entry["id"])
        await interaction.followup.send(
            f"Vote recorded ({result}).", ephemeral=True, delete_after=6,
        )


def _build_post_embed(
    *,
    poster: discord.Member | discord.User,
    character: str,
    attachment_filename: str,
    note: str | None = None,
) -> discord.Embed:
    """Embed wraps the composited Pillow card.

    The card itself carries the kicker / character / poster / rank, so
    the embed body stays minimal — title for jump-link readability, the
    poster mention so push-notifications fire, and an optional caption.
    `attachment_filename` must match the name passed to discord.File so
    the `attachment://` URI resolves.
    """
    embed = discord.Embed(
        title=f"Fit Check · {character}",
        description=(
            f"By {poster.mention}"
            + (f"\n\n*{note}*" if note else "")
        ),
        color=discord.Color.from_rgb(200, 30, 40),
    )
    embed.set_image(url=f"attachment://{attachment_filename}")
    embed.set_footer(text="👍 / 👎 · verified members only")
    return embed


# --------------------------------------------------------------------------- #
# Cog                                                                          #
# --------------------------------------------------------------------------- #

class Fitcheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rotator = _DripLordRotator(bot)

    async def cog_load(self) -> None:
        # One persistent view instance covers every post in the guild.
        self.bot.add_view(FitcheckVoteView())
        self._rotator.start()

    async def cog_unload(self) -> None:
        self._rotator.stop()

    # --- /fitcheck-post ------------------------------------------------- #

    @app_commands.command(
        name="fitcheck-post",
        description="Post a Tekken 8 character fit check to the showcase channel.",
    )
    @app_commands.describe(
        character="The Tekken 8 character this fit is for.",
        image="A screenshot of your customisation (PNG/JPG/WEBP).",
        note="Optional note shown beside your post.",
    )
    async def fitcheck_post(
        self,
        interaction: discord.Interaction,
        character: str,
        image: discord.Attachment,
        note: str | None = None,
    ) -> None:
        await self._post(interaction, character=character, image=image, note=note)

    @fitcheck_post.autocomplete("character")
    async def _character_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        # Discord caps the response at 25 entries; T8 has 41 characters,
        # so a contains-match filter beats a raw slice.
        cur = (current or "").lower()
        matches = sorted(
            c for c in wavu.T8_CHARACTERS if cur in c.lower()
        )
        return [
            app_commands.Choice(name=c, value=c) for c in matches[:25]
        ]

    async def _post(
        self,
        interaction: discord.Interaction,
        *,
        character: str,
        image: discord.Attachment,
        note: str | None,
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return

        if not _is_verified(interaction.user):
            await interaction.response.send_message(
                f"🔒 You need the **{VERIFIED_ROLE_NAME}** role to post a fit "
                "check. Head to **#player-hub** and click **Verify** first.",
                ephemeral=True, delete_after=15,
            )
            return

        if character not in wavu.T8_CHARACTERS:
            await interaction.response.send_message(
                f"**{character}** isn't a Tekken 8 character. Use the "
                "autocomplete dropdown to pick from the roster.",
                ephemeral=True, delete_after=12,
            )
            return

        # Discord guarantees content_type for attachments uploaded from a
        # client; reject obvious non-images so the channel doesn't fill
        # with stray clips.
        ctype = (image.content_type or "").lower()
        if not ctype.startswith("image/"):
            await interaction.response.send_message(
                "Attach a PNG, JPG, or WEBP screenshot — videos and other "
                "files don't fit the leaderboard.",
                ephemeral=True, delete_after=12,
            )
            return
        if image.size and image.size > MAX_IMAGE_BYTES:
            await interaction.response.send_message(
                f"Image is too large ({image.size / 1024 / 1024:.1f} MB). "
                f"Limit is {MAX_IMAGE_BYTES // 1024 // 1024} MB.",
                ephemeral=True, delete_after=12,
            )
            return

        if note and len(note) > 240:
            note = note[:240].rstrip() + "…"

        channel = channel_util.find_text_channel(
            interaction.guild, FITCHECK_CHANNEL_NAME,
        )
        if channel is None:
            await interaction.response.send_message(
                f"Couldn't find a `#{FITCHECK_CHANNEL_NAME}` channel. An admin "
                "needs to run **/setup-server** to create it.",
                ephemeral=True, delete_after=15,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Pull the source bytes off the interaction attachment, then
        # composite into the branded Ehrgeiz fit-check card. Card render
        # off-loops Pillow work; only the bytes read is awaited inline.
        try:
            source_bytes = await image.read()
        except discord.HTTPException as e:
            log.warning("[fitcheck] attachment read failed: %s", e)
            await interaction.followup.send(
                "Couldn't read your attachment from Discord. Try uploading "
                "again in a moment.",
                ephemeral=True,
            )
            return

        # Pull the poster's stored rank so the card footer can flair it.
        # Optional — render_fitcheck_card handles rank_tier=None cleanly.
        player_row = await db.get_player_by_discord(interaction.user.id)
        rank_tier = player_row["rank_tier"] if player_row else None

        try:
            card_buf = await tournament_render.render_fitcheck_card(
                source_bytes=source_bytes,
                character=character,
                poster_name=interaction.user.display_name,
                rank_tier=rank_tier,
            )
        except Exception as e:
            log.exception("[fitcheck] card render failed: %s", e)
            await interaction.followup.send(
                "Couldn't render your fit-check card — likely a corrupt "
                "image. Try a different upload.",
                ephemeral=True,
            )
            return

        attachment_filename = "fitcheck.png"
        card_file = discord.File(card_buf, filename=attachment_filename)
        embed = _build_post_embed(
            poster=interaction.user,
            character=character,
            attachment_filename=attachment_filename,
            note=note,
        )
        provisional_view = FitcheckVoteView()
        try:
            posted = await channel.send(
                content=interaction.user.mention,
                embed=embed,
                file=card_file,
                view=provisional_view,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"I don't have permission to post in `#{channel.name}`. "
                "Flag an admin.",
                ephemeral=True,
            )
            return

        # The CDN URL of the re-uploaded card is what we archive — handy
        # if the leaderboard later wants to pull a thumbnail without
        # re-rendering, and survives the original interaction attachment
        # expiring.
        archived_url = (
            posted.attachments[0].url if posted.attachments else ""
        )

        await db.create_fitcheck_entry(
            guild_id=interaction.guild.id,
            poster_id=interaction.user.id,
            character=character,
            channel_id=channel.id,
            message_id=posted.id,
            image_url=archived_url,
            now_iso=_now_iso(),
        )

        # Audit dump — every post lands in mod-log-dump for staff visibility
        # and as a paper trail if a moderator later removes the entry.
        await audit.post_dump_event(
            interaction.guild,
            title="Fit Check posted",
            color=discord.Color.from_rgb(200, 30, 40),
            fields=[
                ("Poster", f"{interaction.user.mention} (`{interaction.user.id}`)", True),
                ("Character", character, True),
                ("Channel", channel.mention, True),
                ("Jump", f"[Open post]({posted.jump_url})", False),
            ],
        )

        await interaction.followup.send(
            f"Posted in {channel.mention}. Good luck on the leaderboard!",
            ephemeral=True,
        )

    # --- /fitcheck-leaderboard ------------------------------------------ #

    @app_commands.command(
        name="fitcheck-leaderboard",
        description="Show the top fit checks of the past week.",
    )
    @app_commands.describe(
        days="How many days back to consider (default 7, max 30).",
    )
    async def fitcheck_leaderboard(
        self,
        interaction: discord.Interaction,
        days: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return

        window_days = max(1, min(30, days or 7))
        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        rows = await db.top_fitchecks_in_window(
            guild_id=interaction.guild.id,
            since_iso=since.isoformat(),
            limit=5,
        )

        if not rows:
            await interaction.response.send_message(
                f"No fit checks posted in the last **{window_days}** day(s) yet. "
                "Be the first — `/fitcheck-post`.",
                ephemeral=True, delete_after=15,
            )
            return

        embed = discord.Embed(
            title=f"Fit Check Leaderboard · last {window_days} day(s)",
            color=discord.Color.from_rgb(200, 30, 40),
        )
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, row in enumerate(rows):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            jump = (
                f"https://discord.com/channels/"
                f"{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
            )
            poster = interaction.guild.get_member(row["poster_id"])
            poster_label = (
                poster.display_name if poster else f"<@{row['poster_id']}>"
            )
            net = int(row["ups"]) - int(row["downs"])
            embed.add_field(
                name=f"{medal} {poster_label} · {row['character']}",
                value=(
                    f"**{net:+d}** net "
                    f"(👍 {int(row['ups'])} · 👎 {int(row['downs'])})\n"
                    f"[Jump to post]({jump})"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    # --- /fitcheck-delete ----------------------------------------------- #

    @app_commands.command(
        name="fitcheck-delete",
        description="Delete a fit-check post (poster or moderators).",
    )
    @app_commands.describe(
        message_id="The message ID of the fit-check post to delete.",
    )
    async def fitcheck_delete(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        try:
            mid = int(message_id)
        except ValueError:
            await interaction.response.send_message(
                "Message ID should be a number — right-click the post → "
                "Copy Message ID (Developer Mode required).",
                ephemeral=True, delete_after=15,
            )
            return

        entry = await db.get_fitcheck_by_message(mid)
        if entry is None:
            await interaction.response.send_message(
                "No tracked fit-check post for that message ID.",
                ephemeral=True, delete_after=10,
            )
            return

        is_poster = interaction.user.id == entry["poster_id"]
        is_staff = any(
            r.name in {"Admin", "Moderator"} for r in interaction.user.roles
        )
        if not (is_poster or is_staff):
            await interaction.response.send_message(
                "Only the original poster or a moderator can delete a fit check.",
                ephemeral=True, delete_after=10,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Best-effort: scrub the Discord message + the DB row. Either can
        # fail independently (mod might have already deleted the message)
        # and we want to report partial success rather than crash.
        ch = interaction.guild.get_channel(entry["channel_id"])
        if isinstance(ch, discord.TextChannel):
            try:
                msg = await ch.fetch_message(mid)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.info("[fitcheck] delete msg fetch/delete: %s", e)

        await db.delete_fitcheck_entry(entry["id"])

        await audit.post_dump_event(
            interaction.guild,
            title="Fit Check deleted",
            color=discord.Color.dark_red(),
            fields=[
                ("Entry ID", str(entry["id"]), True),
                ("Original poster", f"<@{entry['poster_id']}>", True),
                ("Character", entry["character"], True),
                ("Removed by",
                 f"{interaction.user.mention} "
                 f"({'self' if is_poster else 'mod'})", True),
                ("Original message", f"`{mid}`", True),
            ],
        )
        await interaction.followup.send("Deleted.", ephemeral=True)

    # --- /fitcheck-rotate-now ------------------------------------------- #

    @app_commands.command(
        name="fitcheck-rotate-now",
        description="(Admin) Force the weekly Drip Lord rotation to fire now.",
    )
    @app_commands.default_permissions(administrator=True)
    async def fitcheck_rotate_now(
        self, interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._rotator.rotate_one_guild(
            interaction.guild, force=True,
        )
        await interaction.followup.send(result, ephemeral=True)

    # --- /fitcheck-set-drip-lord ---------------------------------------- #

    @app_commands.command(
        name="fitcheck-set-drip-lord",
        description="(Admin) Manually crown a specific user as Drip Lord.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member="The user to crown",
        reason="Why — included in the announcement and audit log",
        announce="Post a celebration in #announcements (default true)",
    )
    async def fitcheck_set_drip_lord(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
        announce: bool = True,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Server-only command.", ephemeral=True, delete_after=8,
            )
            return

        role = discord.utils.get(guild.roles, name=DRIP_LORD_ROLE_NAME)
        if role is None:
            await interaction.response.send_message(
                f"`{DRIP_LORD_ROLE_NAME}` role doesn't exist yet — run "
                "/setup-server first.",
                ephemeral=True, delete_after=12,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self._rotator._rotate_role(guild, role, member)
        except discord.Forbidden:
            await interaction.followup.send(
                f"Can't move the `{role.name}` role — bot's role needs to be "
                "above it. Drag the bot's role up in Server Settings → Roles.",
                ephemeral=True,
            )
            return

        # Stamp the rotation timestamp so the weekly auto-rotator doesn't
        # immediately overwrite this manual crown with the leaderboard pick.
        # The manual crown effectively resets the cycle clock.
        now_iso = _now_iso()
        await db.set_bot_state(
            guild.id, DRIP_LORD_STATE_KEY, now_iso, now_iso,
        )

        announce_msg: discord.Message | None = None
        if announce:
            player = await db.get_player_by_discord(member.id)
            rank_tier = player["rank_tier"] if player else None
            character = (player["main_char"] if player else None) or "—"
            try:
                card_buf = await tournament_render.render_drip_lord_card(
                    winner_name=member.display_name,
                    character=character,
                    rank_tier=rank_tier,
                    fit_image_bytes=None,
                    net_score=0,
                )
            except Exception:
                log.exception(
                    "[drip-lord] guild=%s manual-crown card render failed",
                    guild.id,
                )
                card_buf = None

            announcements = channel_util.find_text_channel(
                guild, ANNOUNCEMENTS_CHANNEL,
            )
            if announcements is not None:
                embed = discord.Embed(
                    title=f"👑 Drip Lord · {member.display_name}",
                    description=(
                        f"{member.mention} has been crowned Drip Lord "
                        f"by {interaction.user.mention}."
                        + (f"\n\n*{reason}*" if reason else "")
                    ),
                    color=discord.Color.from_rgb(212, 175, 55),
                )
                files: list[discord.File] = []
                if card_buf is not None:
                    files.append(discord.File(card_buf, filename="drip-lord.png"))
                    embed.set_image(url="attachment://drip-lord.png")
                try:
                    announce_msg = await announcements.send(
                        content=member.mention, embed=embed, files=files,
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(
                        "[drip-lord] guild=%s manual announce failed: %s",
                        guild.id, e,
                    )

        await audit.post_dump_event(
            guild,
            title="Drip Lord rotated (manual crown)",
            color=discord.Color.from_rgb(212, 175, 55),
            fields=[
                ("Crowned", f"{member.mention} (`{member.id}`)", True),
                ("Acted by", f"{interaction.user.mention}", True),
                ("Reason", reason or "—", False),
                (
                    "Announcement",
                    (f"[Open]({announce_msg.jump_url})"
                     if announce_msg else "skipped" if not announce else "failed"),
                    False,
                ),
            ],
        )

        await interaction.followup.send(
            f"👑 Crowned {member.mention} as Drip Lord."
            + (f" Announcement: {announce_msg.jump_url}" if announce_msg else
               " (no announcement posted)"),
            ephemeral=True,
        )

    # --- error routing -------------------------------------------------- #

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        await handle_app_command_error(interaction, error, log)


# --------------------------------------------------------------------------- #
# Drip Lord weekly rotator                                                     #
# --------------------------------------------------------------------------- #

class _DripLordRotator:
    """Weekly background task: pick last week's #1 fit check, rotate the
    Drip Lord role onto the winner, post a celebration card.

    Mirrors the _PendingSweeper / _RankSweeper conventions in onboarding:
      * `start()` / `stop()` manage the asyncio task lifecycle, called
        from cog_load / cog_unload.
      * The loop polls hourly and consults DB-backed last-rotation state
        per guild, so a bot restart doesn't reset the schedule and
        running multiple guilds doesn't fight over a single global timer.
      * Failures inside `_rotate_one_guild` are logged and never break
        the outer loop — a misconfigured guild shouldn't stall others.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._loop(), name="drip-lord-rotator",
            )

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        # Stagger from the other startup tasks (rank/pending sweepers)
        # so an early-startup spike doesn't pile API hits.
        await asyncio.sleep(DRIP_LORD_STARTUP_DELAY.total_seconds())
        while True:
            try:
                await self._rotate_all_guilds()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Drip Lord rotator iteration failed")
            await asyncio.sleep(DRIP_LORD_POLL_INTERVAL.total_seconds())

    async def _rotate_all_guilds(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self.rotate_one_guild(guild, force=False)
            except Exception:
                log.exception(
                    "[drip-lord] guild=%s rotation failed", guild.id,
                )

    async def rotate_one_guild(
        self, guild: discord.Guild, *, force: bool,
    ) -> str:
        """Returns a human-readable status line. `force=True` skips the
        last-rotation check (used by /fitcheck-rotate-now).

        Crash-safety contract:
          1. The cooldown check skips this call if a recent rotation
             is on file. So a process restart inside the polling
             interval can't double-fire.
          2. The last-rotation timestamp is stamped BEFORE we send the
             announcement. If we crash between role-rotation and post,
             the next loop iteration sees a recent stamp and won't
             retry until the next 7-day boundary — no double crown.
          3. The announcement post is dedup'd via posted_messages by a
             per-day identity key. If the bot was restarted *just*
             before stamping (rare race), the identity check still
             prevents a duplicate post.
        """
        now = datetime.now(timezone.utc)
        last_iso = await db.get_bot_state(guild.id, DRIP_LORD_STATE_KEY)
        if not force and last_iso:
            try:
                last = datetime.fromisoformat(last_iso)
            except ValueError:
                last = None
            if last is not None and (now - last) < DRIP_LORD_INTERVAL:
                # Not yet due. Quiet skip.
                return f"Skipped — next rotation due {last + DRIP_LORD_INTERVAL:%Y-%m-%d %H:%M UTC}."

        if not force and not last_iso:
            # First-ever observation in this guild: just stamp "now" and
            # wait a full week before crowning anyone. Avoids a
            # mid-week rotation right after the bot's first deploy.
            await db.set_bot_state(
                guild.id, DRIP_LORD_STATE_KEY, now.isoformat(), now.isoformat(),
            )
            log.info("[drip-lord] guild=%s seeded last-rotation, next in 7d",
                     guild.id)
            return "Seeded — first rotation in 7 days."

        # Helper: stamp last-rotation and bail with a status string. Used
        # for every "we're not actually crowning anyone" exit so the next
        # iteration won't re-evaluate until the cooldown elapses again.
        async def _stamp_skip(reason: str) -> str:
            await db.set_bot_state(
                guild.id, DRIP_LORD_STATE_KEY, now.isoformat(), now.isoformat(),
            )
            log.info("[drip-lord] guild=%s %s", guild.id, reason.lower())
            return reason

        since = now - DRIP_LORD_INTERVAL
        rows = await db.top_fitchecks_in_window(
            guild_id=guild.id, since_iso=since.isoformat(), limit=1,
        )
        if not rows:
            return await _stamp_skip(
                "No fit checks posted in the past week — no Drip Lord crowned."
            )
        winner = rows[0]
        net = int(winner["ups"]) - int(winner["downs"])
        if net <= 0:
            return await _stamp_skip(
                f"Top entry's net score was {net} — skipped, no Drip Lord this week."
            )

        # Resolve the winner member; bail (gracefully) if they've left.
        member = guild.get_member(winner["poster_id"])
        if member is None:
            try:
                member = await guild.fetch_member(winner["poster_id"])
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None:
            return await _stamp_skip("Winner left the server — skipped this week.")

        role = discord.utils.get(guild.roles, name=DRIP_LORD_ROLE_NAME)
        if role is None:
            log.warning(
                "[drip-lord] guild=%s role %r missing — run /setup-server",
                guild.id, DRIP_LORD_ROLE_NAME,
            )
            return (
                f"`{DRIP_LORD_ROLE_NAME}` role doesn't exist yet. "
                "Run /setup-server to create it."
            )

        # Per-day identity key for the announcement. Two rotations on the
        # same calendar day are forbidden — used both as the dedup key
        # for posted_messages and as a sanity rail against any future
        # poll-cadence change firing twice in a day.
        identity = f"{now:%Y-%m-%d}"
        already_posted = await db.find_posted_message(
            "drip_lord", identity, guild.id,
        )

        # **Stamp eagerly** before any side effects — if the rotation
        # crashes mid-flow, the next loop iteration will see a recent
        # stamp and skip until next week. We accept "lost rotation" over
        # "double-crowned rotation."
        await db.set_bot_state(
            guild.id, DRIP_LORD_STATE_KEY, now.isoformat(), now.isoformat(),
        )

        try:
            await self._rotate_role(guild, role, member)
        except discord.Forbidden:
            return (
                f"Couldn't rotate the `{DRIP_LORD_ROLE_NAME}` role — "
                "the bot's role needs to be above it. Flag an admin."
            )

        if already_posted is not None:
            # We crashed last time after the announcement but before the
            # bot_state stamp. Role is now in sync with this week's pick;
            # we don't need to re-announce.
            log.info(
                "[drip-lord] guild=%s identity=%s already announced "
                "(message_id=%s) — role re-synced, no double-post",
                guild.id, identity, already_posted["message_id"],
            )
            return (
                f"Crowned {member.display_name} as Drip Lord (net {net:+d}); "
                "announcement was already on record from a prior run."
            )

        # Pull the winning fit's image bytes back off the Discord CDN so
        # we can compose a celebration card. Best-effort — falls back to
        # a brand panel if the URL 404s (post deleted, CDN blip).
        fit_bytes: bytes | None = None
        try:
            fit_bytes = await self._fetch_image(winner["image_url"])
        except Exception:
            log.exception(
                "[drip-lord] guild=%s fetch winner image failed",
                guild.id,
            )

        # Pull rank from players for the celebration footer flair.
        player = await db.get_player_by_discord(member.id)
        rank_tier = player["rank_tier"] if player else None

        try:
            card_buf = await tournament_render.render_drip_lord_card(
                winner_name=member.display_name,
                character=winner["character"],
                rank_tier=rank_tier,
                fit_image_bytes=fit_bytes,
                net_score=net,
            )
        except Exception:
            log.exception(
                "[drip-lord] guild=%s celebration card render failed",
                guild.id,
            )
            card_buf = None

        announcements = channel_util.find_text_channel(
            guild, ANNOUNCEMENTS_CHANNEL,
        )
        jump_url = (
            f"https://discord.com/channels/{guild.id}/"
            f"{winner['channel_id']}/{winner['message_id']}"
        )

        announce_msg: discord.Message | None = None
        if announcements is not None:
            embed = discord.Embed(
                title=f"👑 Drip Lord of the Week · {member.display_name}",
                description=(
                    f"{member.mention} took this week's fashion crown with "
                    f"a **{winner['character']}** fit — net **{net:+d}**.\n"
                    f"[Jump to the winning post]({jump_url})"
                ),
                color=discord.Color.from_rgb(212, 175, 55),
            )
            files: list[discord.File] = []
            if card_buf is not None:
                files.append(discord.File(card_buf, filename="drip-lord.png"))
                embed.set_image(url="attachment://drip-lord.png")
            try:
                announce_msg = await announcements.send(
                    content=member.mention, embed=embed, files=files,
                )
                # Record the post immediately — narrow window between send
                # and record means crash-here is rare, and the duplicate
                # guard above catches it next time.
                await db.record_posted_message(
                    kind="drip_lord", identity=identity,
                    guild_id=guild.id,
                    channel_id=announcements.id,
                    message_id=announce_msg.id,
                    now_iso=now.isoformat(),
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(
                    "[drip-lord] guild=%s announce failed: %s", guild.id, e,
                )
        else:
            log.warning(
                "[drip-lord] guild=%s no #%s channel found",
                guild.id, ANNOUNCEMENTS_CHANNEL,
            )

        # Audit dump so staff can see the rotation happened even if the
        # announcement channel post failed.
        await audit.post_dump_event(
            guild,
            title="Drip Lord rotated",
            color=discord.Color.from_rgb(212, 175, 55),
            fields=[
                ("Winner", f"{member.mention} (`{member.id}`)", True),
                ("Character", winner["character"], True),
                ("Net score", f"{net:+d}", True),
                ("Original post", f"[Jump]({jump_url})", False),
                (
                    "Announcement",
                    (f"[Open]({announce_msg.jump_url})"
                     if announce_msg else "—"),
                    False,
                ),
            ],
        )

        log.info(
            "[drip-lord] guild=%s crowned member=%s net=%+d",
            guild.id, member.id, net,
        )
        return (
            f"Crowned {member.display_name} as Drip Lord (net {net:+d}). "
            f"Announcement: {'sent' if announce_msg else 'failed'}."
        )

    async def _rotate_role(
        self,
        guild: discord.Guild,
        role: discord.Role,
        new_holder: discord.Member,
    ) -> None:
        # Strip from existing holders first so we never have two simultaneous
        # crowns (transient is fine; durable would feel sloppy).
        for existing in list(role.members):
            if existing.id == new_holder.id:
                continue
            try:
                await existing.remove_roles(
                    role, reason="Drip Lord weekly rotation",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(
                    "[drip-lord] couldn't strip %s from %s: %s",
                    role.name, existing.id, e,
                )
        if role not in new_holder.roles:
            await new_holder.add_roles(
                role, reason="Drip Lord weekly rotation",
            )

    async def _fetch_image(self, url: str) -> bytes | None:
        if not url:
            return None
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fitcheck(bot))
