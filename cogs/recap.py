"""Weekly recap composite — auto-posts a Pillow digest of the past 7
days every Monday at 18:00 UTC (or whenever the timer next ticks past
the threshold). Mirrors the Drip Lord rotator's crash-safety pattern:
last-run timestamp in `bot_state` plus a `posted_messages` idempotency
key so a process restart can't double-post the same week's recap.

Surfaces:
  * Background `_RecapPoster` task — checks hourly, fires when it's been
    at least RECAP_INTERVAL (~7 days) since the last post.
  * `/recap-now` — admin-only force-trigger for testing; respects the
    same idempotency guards so two clicks in the same week won't
    double-post.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import audit
import channel_util
import db
import tournament_render
from cogs.fitcheck import DRIP_LORD_ROLE_NAME
from view_util import handle_app_command_error

log = logging.getLogger(__name__)

ANNOUNCEMENTS_CHANNEL = "announcements"
RECAP_STATE_KEY = "recap:last_post"
RECAP_INTERVAL = timedelta(days=7)
RECAP_POLL_INTERVAL = timedelta(hours=1)
RECAP_STARTUP_DELAY = timedelta(seconds=90)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _RecapPoster:
    """Lifecycle for the weekly recap background task. Convention matches
    `_DripLordRotator` so the two scheduled jobs feel uniform — same
    start/stop interface, same hourly poll cadence with a per-guild
    last-run timestamp gate."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._loop(), name="weekly-recap-poster",
            )

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(RECAP_STARTUP_DELAY.total_seconds())
        while True:
            try:
                for guild in list(self.bot.guilds):
                    try:
                        await self.post_for_guild(guild, force=False)
                    except Exception:
                        log.exception(
                            "[recap] guild=%s post failed", guild.id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Recap poster iteration failed")
            await asyncio.sleep(RECAP_POLL_INTERVAL.total_seconds())

    async def post_for_guild(
        self, guild: discord.Guild, *, force: bool,
    ) -> str:
        now = datetime.now(timezone.utc)
        last_iso = await db.get_bot_state(guild.id, RECAP_STATE_KEY)
        if not force and last_iso:
            try:
                last = datetime.fromisoformat(last_iso)
            except ValueError:
                last = None
            if last is not None and (now - last) < RECAP_INTERVAL:
                return f"Skipped — next recap due {last + RECAP_INTERVAL:%Y-%m-%d %H:%M UTC}."

        if not force and not last_iso:
            # First-ever observation — seed and wait a full week.
            await db.set_bot_state(
                guild.id, RECAP_STATE_KEY, now.isoformat(), now.isoformat(),
            )
            return "Seeded — first recap in 7 days."

        # Identity key: ISO week year+number ("2026-W17"). Keeps two
        # rotations from posting in the same calendar week even if a
        # force-trigger fires twice.
        iso_year, iso_week, _ = now.isocalendar()
        identity = f"{iso_year}-W{iso_week:02d}"
        already = await db.find_posted_message("weekly_recap", identity, guild.id)

        # Stamp BEFORE side effects (same pattern as drip lord). If we
        # crash mid-flow the timer won't refire until the next interval.
        await db.set_bot_state(
            guild.id, RECAP_STATE_KEY, now.isoformat(), now.isoformat(),
        )

        if already is not None:
            log.info(
                "[recap] guild=%s identity=%s already posted (msg=%s)",
                guild.id, identity, already["message_id"],
            )
            return f"Already posted for {identity}; skipped duplicate."

        # Gather the stats. Each query is small + indexed; total cost is
        # under a hundred ms even on a busy guild.
        since = now - RECAP_INTERVAL
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

        # Drip Lord — ask Discord directly rather than the DB; the role
        # is the source of truth (it's what the rotator manipulates).
        drip_role = discord.utils.get(guild.roles, name=DRIP_LORD_ROLE_NAME)
        drip_holder = drip_role.members[0] if drip_role and drip_role.members else None
        drip_player = (
            await db.get_player_by_discord(drip_holder.id)
            if drip_holder else None
        )

        top_fit_poster = None
        top_fit_character = None
        top_fit_net: int | None = None
        if top_fit is not None:
            poster_member = guild.get_member(top_fit["poster_id"])
            top_fit_poster = (
                poster_member.display_name if poster_member
                else f"<@{top_fit['poster_id']}>"
            )
            top_fit_character = top_fit["character"]
            top_fit_net = int(top_fit["ups"]) - int(top_fit["downs"])

        week_label = f"{since:%Y-%m-%d} → {now:%Y-%m-%d}"

        try:
            card_buf = await tournament_render.render_weekly_recap_card(
                week_label=week_label,
                drip_lord_name=(
                    drip_holder.display_name if drip_holder else None
                ),
                drip_lord_character=(
                    drip_player["main_char"] if drip_player else None
                ),
                top_fit_poster=top_fit_poster,
                top_fit_character=top_fit_character,
                top_fit_net=top_fit_net,
                new_members=new_members,
                fitchecks_posted=fitchecks_posted,
                tournaments_completed=tournaments_completed,
            )
        except Exception:
            log.exception("[recap] guild=%s render failed", guild.id)
            return "Render failed — check logs."

        announcements = channel_util.find_text_channel(guild, ANNOUNCEMENTS_CHANNEL)
        if announcements is None:
            log.warning(
                "[recap] guild=%s no announcements channel — skipped post",
                guild.id,
            )
            return f"No #{ANNOUNCEMENTS_CHANNEL} channel; recap not posted."

        embed = discord.Embed(
            title=f"📊 Week in Review · {week_label}",
            description=(
                "Last 7 days at a glance — Drip Lord, top fit check, "
                "new members, and tournaments completed."
            ),
            color=discord.Color.from_rgb(212, 175, 55),
        )
        embed.set_image(url="attachment://weekly-recap.png")
        try:
            posted = await announcements.send(
                embed=embed,
                file=discord.File(card_buf, filename="weekly-recap.png"),
            )
            await db.record_posted_message(
                kind="weekly_recap", identity=identity,
                guild_id=guild.id,
                channel_id=announcements.id,
                message_id=posted.id,
                now_iso=now.isoformat(),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[recap] guild=%s post failed: %s", guild.id, e)
            return f"Send failed: {e}"

        await audit.post_dump_event(
            guild,
            title="Weekly recap posted",
            color=discord.Color.from_rgb(212, 175, 55),
            fields=[
                ("Window", week_label, False),
                ("Top fit", top_fit_poster or "—", True),
                ("Drip Lord",
                 drip_holder.display_name if drip_holder else "—", True),
                ("New members", str(new_members), True),
                ("Tournaments", str(tournaments_completed), True),
                ("Fit checks posted", str(fitchecks_posted), True),
            ],
        )

        return (
            f"Posted recap for {identity} — top fit: {top_fit_poster or 'none'}, "
            f"new members: {new_members}, tournaments: {tournaments_completed}."
        )


class Recap(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._poster = _RecapPoster(bot)

    async def cog_load(self) -> None:
        self._poster.start()

    async def cog_unload(self) -> None:
        self._poster.stop()

    @app_commands.command(
        name="recap-now",
        description="(Admin) Force-post the weekly recap for this guild now.",
    )
    @app_commands.default_permissions(administrator=True)
    async def recap_now(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Server-only.", ephemeral=True, delete_after=8,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._poster.post_for_guild(
            interaction.guild, force=True,
        )
        await interaction.followup.send(result, ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        await handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Recap(bot))
